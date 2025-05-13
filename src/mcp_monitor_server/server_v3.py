"""
File Change Monitoring MCP Server.

An MCP server that monitors file system changes and notifies subscribed clients.
"""
import asyncio
import datetime
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Set, Tuple, cast

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    EmptyResult,
    LoggingLevel,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)
from pydantic import AnyUrl, BaseModel, Field
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch


# Define Models for Data Structures
class FileChange(BaseModel):
    """Represents a file change event."""
    path: str
    event: str  # "created", "modified", "deleted"
    timestamp: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "path": self.path,
            "event": self.event,
            "timestamp": self.timestamp.isoformat()
        }


class Subscription(BaseModel):
    """Represents a file monitoring subscription."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    path: str
    recursive: bool = False
    patterns: List[str] = Field(default_factory=lambda: ["*"])
    ignore_patterns: List[str] = Field(default_factory=list)
    events: List[str] = Field(default_factory=lambda: ["created", "modified", "deleted"])
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    changes: List[FileChange] = Field(default_factory=list)
    resource_subscribers: Set[str] = Field(default_factory=set)

    class Config:
        validate_assignment = True

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "path": self.path,
            "recursive": self.recursive,
            "patterns": self.patterns,
            "ignore_patterns": self.ignore_patterns,
            "events": self.events,
            "created_at": self.created_at.isoformat()
        }

    def add_change(self, path: str, event: str) -> Optional[FileChange]:
        """Add a change event to this subscription."""
        # Check if the event type is one we're monitoring
        if event not in self.events:
            return None
        
        change = FileChange(path=path, event=event)
        self.changes.append(change)
        # Limit changes list to recent 100 events to prevent memory bloat
        if len(self.changes) > 100:
            self.changes = self.changes[-100:]
        return change

    def get_changes_since(self, since: Optional[datetime.datetime] = None) -> List[FileChange]:
        """Get changes that occurred after the specified time."""
        if since is None:
            return self.changes
        
        return [change for change in self.changes if change.timestamp > since]


# Define Tool Input Models for validation
class SubscribeInput(BaseModel):
    path: str
    recursive: bool = False
    patterns: List[str] = Field(default_factory=lambda: ["*"])
    ignore_patterns: List[str] = Field(default_factory=list)
    events: List[str] = Field(default_factory=lambda: ["created", "modified", "deleted"])


class UnsubscribeInput(BaseModel):
    subscription_id: str


class GetChangesInput(BaseModel):
    subscription_id: str
    since: Optional[str] = None  # ISO formatted timestamp


class ToolNames(str, Enum):
    """Enumeration of tool names."""
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    LIST_SUBSCRIPTIONS = "list_subscriptions"
    GET_CHANGES = "get_changes"


class FileChangeHandler(FileSystemEventHandler):
    """Event handler for file system changes."""

    def __init__(
        self,
        subscription_id: str,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = False,
        callback: Optional[Callable[[str, str, str], None]] = None,
    ):
        """Initialize the handler."""
        super().__init__()
        self.subscription_id = subscription_id
        self.patterns = patterns or ["*"]
        self.ignore_patterns = ignore_patterns or []
        self.ignore_directories = ignore_directories
        self.callback = callback

    def _should_process_event(self, event: FileSystemEvent) -> bool:
        """Determine if an event should be processed based on patterns."""
        # Skip directory events if configured to do so
        if self.ignore_directories and (
            isinstance(event, DirCreatedEvent) or
            isinstance(event, DirDeletedEvent) or
            isinstance(event, DirModifiedEvent) or
            isinstance(event, DirMovedEvent)
        ):
            return False
            
        # Get the path to check against patterns and ensure it's a string
        path = str(event.src_path)
        
        # Check if the path should be ignored
        for pattern in self.ignore_patterns:
            if fnmatch(path, str(pattern)):
                return False
                
        # Check if the path matches any include pattern
        for pattern in self.patterns:
            if fnmatch(path, str(pattern)):
                return True
                
        # If we have specific patterns and none matched, ignore the event
        return len(self.patterns) == 0 or "*" in self.patterns

    def dispatch(self, event: FileSystemEvent) -> None:
        """Dispatch events to the appropriate handlers."""
        if not self._should_process_event(event):
            return
            
        super().dispatch(event)

    def on_created(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is created."""
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "created")

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is deleted."""
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "deleted")

    def on_modified(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is modified."""
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "modified")
            
    def on_moved(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is moved."""
        # Treat move as a delete+create operation
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "deleted")
            self.callback(self.subscription_id, str(event.dest_path), "created")


class FileMonitorMCPServer:
    """MCP server for monitoring file system changes."""
    
    def __init__(self) -> None:
        """Initialize server components."""
        # Setup logging
        self.logger = logging.getLogger("file-monitor-mcp")
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        ))
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
        
        # Create MCP server
        self.server = Server("file-monitor-mcp") # type: ignore
        
        # Setup storage
        self.subscriptions: Dict[str, Subscription] = {}
        self.observers: Dict[str, Any] = {}
        self.handlers: Dict[str, FileChangeHandler] = {}
        self.watches: Dict[str, Any] = {}
        self.clients: Dict[str, Any] = {}
        
        # Locks for thread safety
        self.subscription_lock = asyncio.Lock()
        
        # Setup notification queue
        self.notification_queue: asyncio.Queue[Tuple[str, str, str]] = asyncio.Queue()
        self.notification_task: Optional[asyncio.Task] = None
        self.stopping = False
        
        # Register handlers
        self._register_handlers()
    
    def _register_handlers(self):
        """Register MCP protocol handlers."""
        
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List all available tools."""
            self.logger.info("Listing tools")
            return [
                Tool(
                    name=ToolNames.SUBSCRIBE,
                    description="Create a new subscription to monitor file changes",
                    inputSchema=SubscribeInput.model_json_schema(),
                ),
                Tool(
                    name=ToolNames.UNSUBSCRIBE,
                    description="Cancel an active subscription",
                    inputSchema=UnsubscribeInput.model_json_schema(),
                ),
                Tool(
                    name=ToolNames.LIST_SUBSCRIPTIONS,
                    description="List all active subscriptions",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    },
                ),
                Tool(
                    name=ToolNames.GET_CHANGES,
                    description="Retrieve recent changes for a specific subscription",
                    inputSchema=GetChangesInput.model_json_schema(),
                ),
            ]
            
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """Call a tool."""
            self.logger.info(f"Calling tool: {name} with arguments: {arguments}")
            
            match name:
                case ToolNames.SUBSCRIBE:
                    input_data = SubscribeInput.model_validate(arguments)
                    return await self._handle_subscribe_tool(input_data)
                
                case ToolNames.UNSUBSCRIBE:
                    unsubscribe_data = UnsubscribeInput.model_validate(arguments)
                    return await self._handle_unsubscribe_tool(unsubscribe_data)
                
                case ToolNames.LIST_SUBSCRIPTIONS:
                    return await self._handle_list_subscriptions_tool()
                
                case ToolNames.GET_CHANGES:
                    get_changes_data = GetChangesInput.model_validate(arguments)
                    return await self._handle_get_changes_tool(get_changes_data)
                
                case _:
                    return [TextContent(
                        type="text",
                        text=json.dumps({
                            "error": f"Unknown tool: {name}"
                        })
                    )]
        
        @self.server.list_resources()
        async def list_resources() -> List[Resource]:
            """List all resources."""
            self.logger.info("Listing resources")
            resources = []
            
            # Add subscription resources
            async with self.subscription_lock:
                for subscription_id, subscription in self.subscriptions.items():
                    resources.append(Resource(
                        uri=AnyUrl(f"subscription://{subscription_id}"),
                        name=f"Subscription: {subscription.path}",
                        mimeType="application/json",
                        description=f"File change notifications for {subscription.path}"
                    ))
                    
            return resources
        
        @self.server.list_resource_templates()
        async def list_resource_templates() -> List[ResourceTemplate]:
            """List all resource templates."""
            self.logger.info("Listing resource templates")
            return [
                ResourceTemplate(
                    uriTemplate="file://{path}",
                    name="File Content",
                    description="Content of a file"
                ),
                ResourceTemplate(
                    uriTemplate="dir://{path}",
                    name="Directory Listing",
                    description="List of files in a directory",
                    mimeType="application/json"
                ),
                ResourceTemplate(
                    uriTemplate="subscription://{subscription_id}",
                    name="Subscription Status",
                    description="Status and recent changes for a subscription",
                    mimeType="application/json"
                )
            ]
            
        @self.server.read_resource()
        async def read_resource(uri: AnyUrl) -> str:
            """Read a resource's content."""
            self.logger.info(f"Reading resource: {uri}")
            
            # Parse the URI scheme and path
            if not uri or "://" not in str(uri) or not uri.scheme or not uri.path:
                raise ValueError(f"Invalid resource URI: {uri}")
            
            # scheme, path = uri.split("://", 1)
            
            # Handle each resource type based on scheme
            match uri.scheme:
                case "file":
                    return await self._read_file_resource(uri.path)
                    
                case "dir":
                    return await self._read_directory_resource(uri.path)
                    
                case "subscription":
                    return await self._read_subscription_resource(uri.path)
                    
                case _:
                    raise ValueError(f"Unsupported resource scheme: {uri.scheme}")
        
        @self.server.subscribe_resource()
        async def subscribe_resource(uri: AnyUrl) -> None:
            """Subscribe to resource updates."""
            self.logger.info(f"Subscribing to resource: {uri}")
            
            # Only subscription resources support subscription
            if not str(uri).startswith("subscription://"):
                raise ValueError(f"Resource does not support subscription: {uri}")

            subscription_id = str(uri).split("://", 1)[1]

            # Generate unique client ID for this subscription
            client_id = str(uuid.uuid4())
            
            # Store client session for notifications
            self.clients[client_id] = self.server.request_context.session
            
            # Add client as subscriber
            async with self.subscription_lock:
                if subscription_id not in self.subscriptions:
                    raise ValueError(f"Subscription not found: {subscription_id}")
            
            subscription = self.subscriptions[subscription_id]
            subscription.resource_subscribers.add(client_id)
            
            try:
                # Keep subscription active until client disconnects
                while True:
                    await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                self.logger.info(f"Subscription cancelled: {uri}")
            finally:
                # Remove client as subscriber when done
                async with self.subscription_lock:
                    if subscription_id in self.subscriptions:
                        subscription = self.subscriptions[subscription_id]
                        if client_id in subscription.resource_subscribers:
                            subscription.resource_subscribers.remove(client_id)
                
                # Remove client from clients dict
                if client_id in self.clients:
                    del self.clients[client_id]
                
            return None
            
        @self.server.set_logging_level()
        async def set_logging_level(level: LoggingLevel) -> None:
            """Set the server's logging level."""
            # Convert MCP logging level to Python logging level
            level_map = {
                "trace": logging.DEBUG,
                "debug": logging.DEBUG,
                "info": logging.INFO,
                "warn": logging.WARNING,
                "error": logging.ERROR,
                "fatal": logging.CRITICAL
            }
            python_level = level_map.get(level.lower(), logging.INFO)
            
            # Set the level
            self.logger.setLevel(python_level)
            self.logger.info(f"Logging level set to: {level}")
            
    async def _handle_subscribe_tool(self, input_data: SubscribeInput) -> List[TextContent]:
        """Handle the subscribe tool."""
        path = input_data.path
        
        # Convert to absolute path if relative
        if not os.path.isabs(path):
            path = os.path.abspath(path)
            
        # Check if path exists
        if not os.path.exists(path):
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Path does not exist: {path}"
                })
            )]
            
        # Create subscription
        subscription = Subscription(
            path=path,
            recursive=input_data.recursive,
            patterns=input_data.patterns,
            ignore_patterns=input_data.ignore_patterns,
            events=input_data.events
        )
        
        # Start watching the path
        success = await self._watch(
            subscription_id=subscription.id,
            path=path,
            recursive=input_data.recursive,
            patterns=input_data.patterns,
            ignore_patterns=input_data.ignore_patterns,
            ignore_directories=False
        )
        
        if not success:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Failed to watch path: {path}"
                })
            )]
            
        # Store the subscription
        async with self.subscription_lock:
            self.subscriptions[subscription.id] = subscription
            
        # Return subscription details
        return [TextContent(
            type="text",
            text=json.dumps({
                "subscription_id": subscription.id,
                "status": "active",
                "path": path,
                "recursive": input_data.recursive,
                "resource_uri": f"subscription://{subscription.id}"
            }, indent=2)
        )]
    
    async def _handle_unsubscribe_tool(self, input_data: UnsubscribeInput) -> List[TextContent]:
        """Handle the unsubscribe tool."""
        subscription_id = input_data.subscription_id
            
        # Stop watching
        await self._unwatch(subscription_id)
        
        # Delete subscription
        async with self.subscription_lock:
            if subscription_id in self.subscriptions:
                del self.subscriptions[subscription_id]
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "success": True,
                        "message": "Subscription successfully deleted"
                    }, indent=2)
                )]
            
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "message": "Subscription not found"
                }, indent=2)
            )]
    
    async def _handle_list_subscriptions_tool(self) -> List[TextContent]:
        """Handle the list_subscriptions tool."""
        async with self.subscription_lock:
            subscriptions_data = [
                {
                    "id": subscription.id,
                    "path": subscription.path,
                    "recursive": subscription.recursive,
                    "patterns": subscription.patterns,
                    "ignore_patterns": subscription.ignore_patterns,
                    "events": subscription.events,
                    "created_at": subscription.created_at.isoformat(),
                    "subscriber_count": len(subscription.resource_subscribers)
                }
                for subscription in self.subscriptions.values()
            ]
            
            return [TextContent(
                type="text",
                text=json.dumps({
                    "subscriptions": subscriptions_data
                }, indent=2)
            )]
    
    async def _handle_get_changes_tool(self, input_data: GetChangesInput) -> List[TextContent]:
        """Handle the get_changes tool."""
        subscription_id = input_data.subscription_id
        
        # Parse since timestamp if provided
        since = None
        if input_data.since:
            try:
                since = datetime.datetime.fromisoformat(input_data.since)
                if since.tzinfo is None:
                    # Add UTC timezone if not specified
                    since = since.replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Invalid timestamp format: {input_data.since}. Expected ISO format."
                    })
                )]
        
        # Get the subscription
        async with self.subscription_lock:
            if subscription_id not in self.subscriptions:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Subscription not found: {subscription_id}"
                    })
                )]
                
            subscription = self.subscriptions[subscription_id]
            
            # Get changes since the specified time
            changes = subscription.get_changes_since(since)
            
            return [TextContent(
                type="text",
                text=json.dumps({
                    "subscription_id": subscription_id,
                    "path": subscription.path,
                    "changes": [change.to_dict() for change in changes]
                }, indent=2)
            )]
    
    async def _read_file_resource(self, path: str) -> str:
        """Read a file resource."""
        if not os.path.isfile(path):
            raise ValueError(f"File not found: {path}")
            
        try:
            async with await anyio.open_file(path, "rb") as file:
                content = await file.read()
                
            # For simplicity, we'll assume it's utf-8 text
            # In a production implementation, you would handle binary files differently
            return content.decode("utf-8")
            
        except Exception as e:
            raise ValueError(f"Error reading file {path}: {str(e)}")
    
    async def _read_directory_resource(self, path: str) -> str:
        """Read a directory resource."""
        if not os.path.isdir(path):
            raise ValueError(f"Directory not found: {path}")
            
        try:
            # Get directory contents
            entries = []
            for entry in os.scandir(path):
                entry_type = "file" if entry.is_file() else "directory"
                entries.append({
                    "name": entry.name,
                    "path": entry.path,
                    "type": entry_type,
                    "size": entry.stat().st_size if entry.is_file() else None,
                    "modified": entry.stat().st_mtime
                })
                
            # Sort by name
            entries.sort(key=lambda e: str(e["name"]))
            
            return json.dumps({
                "path": path,
                "entries": entries
            }, indent=2)
            
        except Exception as e:
            raise ValueError(f"Error reading directory {path}: {str(e)}")
    
    async def _read_subscription_resource(self, subscription_id: str) -> str:
        """Read a subscription resource."""
        async with self.subscription_lock:
            if subscription_id not in self.subscriptions:
                raise ValueError(f"Subscription not found: {subscription_id}")
                
            subscription = self.subscriptions[subscription_id]
            
            # Convert to dict for JSON serialization
            subscription_data = subscription.to_dict()
            
            # Add recent changes
            subscription_data["recent_changes"] = [
                change.to_dict() for change in subscription.changes[-20:]  # Show last 20 changes
            ]
            
            # Add subscriber count
            subscription_data["subscriber_count"] = len(subscription.resource_subscribers)
            
            return json.dumps(subscription_data, indent=2)
    
    def _handle_event(self, subscription_id: str, path: str, event_type: str) -> None:
        """Handle a file system event by putting it in the queue."""
        asyncio.run_coroutine_threadsafe(
            self.notification_queue.put((subscription_id, path, event_type)),
            asyncio.get_event_loop()
        )
    
    async def _process_notifications(self) -> None:
        """Process notifications from the queue."""
        self.logger.info("Starting notification processor")
        
        while not self.stopping:
            try:
                # Get the next notification
                subscription_id, path, event_type = await self.notification_queue.get()
                
                # Add the change to the subscription
                async with self.subscription_lock:
                    if subscription_id in self.subscriptions:
                        subscription = self.subscriptions[subscription_id]
                        change = subscription.add_change(path, event_type)
                        
                        # Send notifications to all subscribers
                        for client_id in subscription.resource_subscribers:
                            session = self.clients.get(client_id)
                            if session:
                                try:
                                    await session.send_resource_updated_notification(
                                        uri=f"subscription://{subscription_id}"
                                    )
                                except Exception as e:
                                    self.logger.error(f"Error sending notification: {e}")
                
                self.notification_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error processing notification: {e}")
    
    async def _watch(
        self, 
        subscription_id: str, 
        path: str, 
        recursive: bool = False,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = False,
    ) -> bool:
        """Watch a path for changes."""
        if not os.path.exists(path):
            return False
        
        # Stop existing observer for this subscription if any
        await self._unwatch(subscription_id)
        
        # Create event handler
        handler = FileChangeHandler(
            subscription_id=subscription_id,
            patterns=patterns,
            ignore_patterns=ignore_patterns,
            ignore_directories=ignore_directories,
            callback=self._handle_event,
        )
        
        # Create observer
        observer = Observer()
        watch = observer.schedule(handler, path, recursive=recursive)
        observer.start()
        
        # Store references
        self.observers[subscription_id] = observer
        self.handlers[subscription_id] = handler
        self.watches[subscription_id] = watch
        
        return True
    
    async def _unwatch(self, subscription_id: str) -> bool:
        """Stop watching a subscription."""
        observer = self.observers.get(subscription_id)
        watch = self.watches.get(subscription_id)
        
        if observer and watch:
            observer.unschedule(watch)
            observer.stop()
            observer.join()
            
            del self.observers[subscription_id]
            del self.handlers[subscription_id]
            del self.watches[subscription_id]
            return True
        return False
    
    async def start(self) -> None:
        """Start the server."""
        self.logger.info("Starting file monitoring MCP server")
        
        # Start the notification processor
        self.stopping = False
        self.notification_task = asyncio.create_task(self._process_notifications())
    
    async def stop(self) -> None:
        """Stop the server."""
        self.logger.info("Stopping file monitoring MCP server")
        
        # Stop the notification processor
        self.stopping = True
        if self.notification_task:
            self.notification_task.cancel()
            try:
                await self.notification_task
            except asyncio.CancelledError:
                pass
            
        # Stop all watchers
        for subscription_id in list(self.observers.keys()):
            await self._unwatch(subscription_id)
    
    @asynccontextmanager
    async def run(self) -> AsyncGenerator[None, None]:
        """Run the server as a context manager."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()


async def serve() -> None:
    """Run the server."""
    server = FileMonitorMCPServer()
    
    # Initialize the server
    options = server.server.create_initialization_options()
    
    # Run the server with context manager to ensure cleanup
    async with server.run():
        async with stdio_server() as (read_stream, write_stream):
            await server.server.run(read_stream, write_stream, options)


def main() -> None:
    """Main entry point."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr
    )
    
    # Parse command line arguments
    # import argparse
    # parser = argparse.ArgumentParser(description="File Change Monitoring MCP Server")
    # args = parser.parse_args()
    
    # Run the server
    asyncio.run(serve())


if __name__ == "__main__":
    main()
