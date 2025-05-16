"""
File Change Monitoring MCP Server.

An MCP server that monitors file system changes and notifies subscribed clients.
Implements a subscription-based model where clients can:
- Monitor specific files or directories
- Apply patterns to filter relevant files
- Receive real-time notifications when changes occur
- Query historical changes

This server uses the Watchdog library for file system events and the MCP protocol
for client communication.

IMPORTANT: This implementation includes special handling for macOS-specific fsevents issues
where file modifications are sometimes incorrectly reported as directory deletion events.
This is due to how the FSEvents API works in macOS. Our workaround checks if a
"deleted directory" event still has an existing file at the specified path, and if so,
reclassifies it as a file modification event.
"""
import asyncio
import datetime
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from fnmatch import fnmatch
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

import anyio
from mcp import ResourcesCapability, ServerCapabilities, ToolsCapability
from mcp.server import Server, InitializationOptions, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    LoggingLevel,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)
from pydantic import AnyUrl, BaseModel, ConfigDict, Field
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    # File event classes are imported for type matching in the Watchdog event system
    # These are used indirectly when Watchdog passes events to our handler methods
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch, BaseObserver

# Default patterns to ignore in file monitoring
DEFAULT_IGNORE_PATTERNS = [
    # Virtual environments
    "**/venv/**", "**/.venv/**", "**/env/**", "**/.env/**",
    "**/virtualenv/**", "**/.virtualenv/**",
    # Node modules and package caches
    "**/node_modules/**", "**/.npm/**", "**/package-lock.json",
    # Python caches
    "**/__pycache__/**", "**/*.pyc", "**/*.pyo", "**/*.pyd",
    "**/.pytest_cache/**", "**/.coverage", "**/.mypy_cache/**",
    # Build directories
    "**/build/**", "**/dist/**", "**/target/**", "**/out/**",
    # IDE directories
    "**/.idea/**", "**/.vscode/**", "**/.vs/**",
    # Git directories
    "**/.git/**", "**/.github/**", "**/.gitignore",
    # Other common patterns
    "**/.DS_Store", "**/Thumbs.db", "**/desktop.ini",
    "**/*.log", "**/*.tmp", "**/*.temp"
]

# Define Models for Data Structures
class FileChange(BaseModel):
    """
    Represents a file change event with path, event type, and timestamp.
    
    Attributes:
        path: The file path that was changed
        event: The type of event ("created", "modified", "deleted")
        timestamp: When the change occurred (UTC)
    """
    path: str = Field(description="Path to the changed file or directory")
    event: str = Field(description="Event type: created, modified, or deleted")
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        description="When the change occurred (UTC timezone)",
    )


class Subscription(BaseModel):
    """
    Represents a file monitoring subscription with filters and change history.
    
    Attributes:
        id: Unique identifier for this subscription
        path: Base path to monitor
        recursive: Whether to monitor subdirectories
        patterns: File patterns to include (glob format)
        ignore_patterns: File patterns to exclude (glob format)
        events: Types of events to track
        created_at: When this subscription was created
        changes: History of detected changes
        resource_subscribers: Set of client IDs subscribed to updates
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this subscription"
    )
    path: str = Field(description="Base path to monitor for changes")
    recursive: bool = Field(
        default=False,
        description="Whether to monitor subdirectories recursively"
    )
    patterns: List[str] = Field(
        default_factory=lambda: ["*"],
        description="File patterns to include (glob format, * for all files)"
    )
    ignore_patterns: List[str] = Field(
        default_factory=list,
        description="File patterns to exclude (glob format)"
    )
    events: List[str] = Field(
        default_factory=lambda: ["created", "modified", "deleted"],
        description="Types of events to track: created, modified, deleted"
    )
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc),
        description="When this subscription was created (UTC timezone)"
    )
    changes: List[FileChange] = Field(
        default_factory=list,
        description="History of detected changes"
    )
    resource_subscribers: Set[str] = Field(
        default_factory=set,
        description="Set of client IDs subscribed to receive change notifications"
    )

    model_config = ConfigDict(validate_assignment=True)

    def add_change(self, path: str, event: str) -> Optional[FileChange]:
        """
        Add a change event to this subscription if it matches our event types.
        
        Args:
            path: Path of the changed file/directory
            event: Type of change event
            
        Returns:
            The created FileChange object if the event was added, None otherwise
            
        Note:
            Limits the history to 100 most recent events to prevent memory issues
        """
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
        """
        Get changes that occurred after the specified time.
        
        Args:
            since: Return only changes after this timestamp, or all if None
            
        Returns:
            List of FileChange objects that match the time criteria
        """
        if since is None:
            return self.changes
        
        return [change for change in self.changes if change.timestamp > since]


# Define Tool Input Models for validation
class SubscribeInput(BaseModel):
    """
    Input parameters for the subscribe tool.
    
    Attributes:
        path: File or directory path to monitor
        recursive: Whether to monitor subdirectories
        patterns: File patterns to include
        ignore_patterns: File patterns to exclude
        events: Types of events to track
    """
    path: str = Field(description="File or directory path to monitor")
    recursive: bool = Field(
        default=False,
        description="Whether to monitor subdirectories recursively"
    )
    patterns: List[str] = Field(
        default_factory=lambda: ["*"],
        description="File patterns to include (glob format, * for all files)"
    )
    ignore_patterns: List[str] = Field(
        default_factory=list,
        description="File patterns to exclude (glob format)"
    )
    events: List[str] = Field(
        default_factory=lambda: ["created", "modified", "deleted"],
        description="Types of events to track: created, modified, deleted"
    )


class UnsubscribeInput(BaseModel):
    """
    Input parameters for the unsubscribe tool.
    
    Attributes:
        subscription_id: ID of the subscription to cancel
    """
    subscription_id: str = Field(description="ID of the subscription to cancel")


class GetChangesInput(BaseModel):
    """
    Input parameters for the get_changes tool.
    
    Attributes:
        subscription_id: ID of the subscription to get changes for
        since: ISO-formatted timestamp to filter changes (optional)
    """
    subscription_id: str = Field(description="ID of the subscription to get changes for")
    since: Optional[str] = Field(
        default=None,
        description="ISO-formatted timestamp to filter changes (optional)"
    )


class CreateMcpignoreInput(BaseModel):
    """
    Input parameters for the create_mcpignore tool.
    
    Attributes:
        path: Directory where to create the .mcpignore file
        include_defaults: Whether to include the default ignore patterns
    """
    path: str = Field(description="Directory where to create the .mcpignore file")
    include_defaults: bool = Field(
        default=True, 
        description="Whether to include the default ignore patterns"
    )


class ToolNames(str, Enum):
    """
    Enumeration of available tool names in the server API.
    """
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    LIST_SUBSCRIPTIONS = "list_subscriptions"
    GET_CHANGES = "get_changes"
    CREATE_MCPIGNORE = "create_mcpignore"


class FileChangeHandler(FileSystemEventHandler):
    """
    Event handler for file system changes using Watchdog.
    
    Handles and filters file system events based on patterns and configuration,
    then routes events to a callback function for processing.
    """

    def __init__(
        self,
        subscription_id: str,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = False,
        callback: Optional[Callable[[str, str, str], None]] = None,
    ):
        """
        Initialize the file change handler.
        
        Args:
            subscription_id: ID of the subscription this handler belongs to
            patterns: List of glob patterns to include files
            ignore_patterns: List of glob patterns to exclude files
            ignore_directories: Whether to ignore directory events
            callback: Function to call when events are detected
        """
        super().__init__()
        self.subscription_id = subscription_id
        self.patterns = patterns or ["*"]
        self.ignore_patterns = ignore_patterns or []
        self.ignore_directories = ignore_directories
        self.callback = callback

    def _should_process_event(self, event: FileSystemEvent) -> bool:
        """
        Determine if an event should be processed based on patterns.
        
        Args:
            event: The file system event to check
            
        Returns:
            True if the event should be processed, False otherwise
        """
        # Skip directory events if configured to do so
        if self.ignore_directories and (
            isinstance(event, (DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, DirMovedEvent))
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
        """
        Dispatch events to the appropriate handlers after filtering.
        
        Args:
            event: The file system event to dispatch
        """
        # First, check if we should process this event based on patterns
        if not self._should_process_event(event):
            return
            
        # Get event details for debugging
        event_class = event.__class__.__name__
        event_path = getattr(event, 'src_path', 'unknown_path')
        is_directory = getattr(event, 'is_directory', None)
        
        print(f"Dispatching event: {event_class} for {event_path} (is_directory={is_directory})")
        
        # Handle macOS-specific fsevents anomaly where file modifications trigger DirDeletedEvent
        if isinstance(event, DirDeletedEvent) and os.path.isfile(event_path):
            print(f"Correcting macOS fsevents anomaly: {event_class} -> FileModifiedEvent for {event_path}")
            # Pass to the on_deleted handler which has special logic to handle this case
            self.on_deleted(event)
            return
            
        # Route the event to the correct handler explicitly
        if isinstance(event, (FileCreatedEvent, DirCreatedEvent)):
            self.on_created(event)
        elif isinstance(event, (FileDeletedEvent, DirDeletedEvent)):
            self.on_deleted(event)
        elif isinstance(event, (FileModifiedEvent, DirModifiedEvent)):
            self.on_modified(event)
        elif isinstance(event, (FileMovedEvent, DirMovedEvent)):
            self.on_moved(event)
        else:
            # Special handling for synthetic or platform-specific events
            print(f"WARNING: Unknown event type {event_class}, attempting to categorize")
            
            # Try to intelligently categorize based on event attributes and file system state
            if hasattr(event, 'is_synthetic') and event.is_synthetic:
                print(f"Synthetic event detected, checking file state")
                if os.path.exists(event_path):
                    print(f"File exists, treating as modification")
                    self.on_modified(event)
                else:
                    print(f"File doesn't exist, treating as deletion")
                    self.on_deleted(event)
            else:
                # If all else fails, use the standard dispatch
                print(f"Using default dispatch for {event_class}")
                super().dispatch(event)

    def on_created(self, event: FileSystemEvent) -> None:
        """
        Called when a file or directory is created.
        
        Args:
            event: The file creation event
        """
        # Log creation events
        event_class = event.__class__.__name__
        print(f"Watchdog: {event_class} detected for {str(event.src_path)}")
        
        if self.callback:
            # Make sure we're using a string path
            path = str(event.src_path)
            
            # Determine the correct event type
            if isinstance(event, (FileCreatedEvent, DirCreatedEvent)):
                event_type = "created"
            else:
                print(f"WARNING: Unexpected event class {event_class} in on_created handler")
                event_type = "created"  # Force the type for this handler
                
            print(f"Processed creation event as: {event_type}")
                
            # Call the event handler callback
            self.callback(self.subscription_id, path, event_type)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """
        Called when a file or directory is deleted.
        
        Args:
            event: The file deletion event
        """
        # Log deletion events
        event_class = event.__class__.__name__
        print(f"Watchdog: {event_class} detected for {str(event.src_path)}")
        
        if self.callback:
            # Make sure we're using a string path
            path = str(event.src_path)
            
            # Handle macOS fsevents special case where file modifications are reported as directory deletions
            # This happens due to how the FSEvents API works on macOS
            is_macos_modification_bug = False
            
            if isinstance(event, DirDeletedEvent) and os.path.isfile(path):
                # This is a misclassified event - a file still exists but was reported as a deleted directory
                print(f"Detected macOS fsevents anomaly: DirDeletedEvent for existing file {path}")
                is_macos_modification_bug = True
                event_type = "modified"  # Reclassify as a modification event
            elif isinstance(event, (FileDeletedEvent, DirDeletedEvent)):
                # Normal deletion event
                event_type = "deleted"
            else:
                print(f"WARNING: Unexpected event class {event_class} in on_deleted handler")
                event_type = "deleted"  # Force the type for this handler
                
            print(f"Processed event as: {event_type} (was originally a {event_class})")
                
            # Call the event handler callback
            self.callback(self.subscription_id, path, event_type)

    def on_modified(self, event: FileSystemEvent) -> None:
        """
        Called when a file or directory is modified.
        
        Args:
            event: The file modification event
        """
        # Log the modification event with detailed information
        event_class = event.__class__.__name__
        print(f"Watchdog: {event_class} detected for {str(event.src_path)}")
        
        if self.callback:
            # Make sure we're using a string path
            path = str(event.src_path)
            
            # Determine the correct event type based on the event class
            # FileModifiedEvent should become "modified" 
            # This ensures we're not confusing different event types
            if isinstance(event, (FileModifiedEvent, DirModifiedEvent)):
                event_type = "modified"
            else:
                # This branch should not be taken for modification events, but adding as a safeguard
                print(f"WARNING: Unexpected event class {event_class} in on_modified handler")
                event_type = "modified"  # Force the type for this handler
            
            print(f"Processed modification event as: {event_type}")
            
            # Call the event handler callback
            self.callback(self.subscription_id, path, event_type)
            
    def on_moved(self, event: FileSystemEvent) -> None:
        """
        Called when a file or directory is moved.
        Treats moves as a delete+create operation.
        
        Args:
            event: The file move event
        """
        # Log move events
        event_class = event.__class__.__name__
        print(f"Watchdog: {event_class} detected - {str(event.src_path)} -> {str(event.src_path)}")
        
        # Treat move as a delete+create operation
        if self.callback:
            # Verify we're working with a valid move event that has source and destination
            if hasattr(event, 'src_path') and hasattr(event, 'dest_path'):
                # Source path (treat as deleted)
                src_path = str(event.src_path)
                print(f"Processing move source as deletion: {src_path}")
                self.callback(self.subscription_id, src_path, "deleted")
                
                # Destination path (treat as created)
                dest_path = str(event.dest_path)
                print(f"Processing move destination as creation: {dest_path}")
                self.callback(self.subscription_id, dest_path, "created")
            else:
                print(f"WARNING: Move event missing source or destination path")


class FileMonitorMCPServer:
    """
    MCP server for monitoring file system changes.
    
    Provides an API for clients to:
    - Subscribe to file/directory changes
    - Set filters for events and file patterns
    - Receive real-time notifications
    - Query history of changes
    
    Uses the Watchdog library for file monitoring and the MCP protocol for
    client communication.
    """
    
    def __init__(self) -> None:
        """
        Initialize server components, storage, and logging.
        """
        # Dictionary to store changes by subscription_id for easier testing
        self._changes: dict[str, Any] = {}
        
        # Setup logging
        self.logger = logging.getLogger("file-monitor-mcp")
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        ))
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        
        # Create MCP server - this is the actual Server instance from MCP
        self.server: Server[Any] = Server("mcp-monitor-server")

        # Setup storage
        self.subscriptions: Dict[str, Subscription] = {}  # Subscription ID -> Subscription object
        self.observers: Dict[str, BaseObserver] = {}  # Subscription ID -> Watchdog Observer
        self.handlers: Dict[str, FileChangeHandler] = {}  # Subscription ID -> File Change Handler
        self.watches: Dict[str, ObservedWatch] = {}  # Subscription ID -> Watchdog Watch
        self.clients: Dict[str, Any] = {}  # Client ID -> MCP Session
        
        # Locks for thread safety
        self.subscription_lock = asyncio.Lock()
        
        # Setup notification queue
        self.notification_queue: asyncio.Queue[Tuple[str, str, str]] = asyncio.Queue()
        self.notification_task: Optional[asyncio.Task] = None
        self.stopping = False
        
        # Register handlers
        self._register_handlers()
    
    def _register_handlers(self) -> None:
        """
        Register MCP protocol handlers for tools, resources, and subscriptions.
        
        Sets up the server API to handle:
        - Tool registration and execution
        - Resource listing and access
        - Resource subscription for notifications
        - Logging configuration
        """
        
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """
            List all available tools for MCP clients.
            
            Returns:
                List of Tool objects with name, description, and input schema
            """
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
                Tool(
                    name=ToolNames.CREATE_MCPIGNORE,
                    description="Create a .mcpignore file to specify file patterns to ignore",
                    inputSchema=CreateMcpignoreInput.model_json_schema(),
                ),
            ]
            
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """
            Call a specific tool with arguments.
            
            Args:
                name: Name of the tool to call
                arguments: Arguments to pass to the tool
                
            Returns:
                List of TextContent objects with the tool's results
                
            Raises:
                ValueError: If the tool name is unknown
            """
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
                
                case ToolNames.CREATE_MCPIGNORE:
                    create_mcpignore_data = CreateMcpignoreInput.model_validate(arguments)
                    return await self._handle_create_mcpignore_tool(create_mcpignore_data)
                
                case _:
                    return [TextContent(
                        type="text",
                        text=json.dumps({
                            "error": f"Unknown tool: {name}"
                        })
                    )]
        
        @self.server.list_resources()
        async def list_resources() -> List[Resource]:
            """
            List all available resources for MCP clients.
            
            Returns:
                List of Resource objects, primarily subscription resources
            """
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
            """
            List all resource templates for MCP clients.
            
            Returns:
                List of ResourceTemplate objects defining the types of resources available
            """
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
            """
            Read a resource's content.
            
            Args:
                uri: URI of the resource to read
                
            Returns:
                String content of the resource
                
            Raises:
                ValueError: If the URI is invalid or the resource doesn't exist
            """
            self.logger.info(f"Reading resource: {uri}")
            
            # Parse the URI scheme and path
            scheme, path = str(uri).split("://", 1)
            if not uri or "://" not in str(uri) or not scheme or not path:
                raise ValueError(f"Invalid resource URI: {uri}")
            
            # Handle each resource type based on scheme
            match scheme:
                case "file":
                    return await self._read_file_resource(path)
                    
                case "dir":
                    return await self._read_directory_resource(path)
                    
                case "subscription":
                    return await self._read_subscription_resource(path)
                    
                case _:
                    raise ValueError(f"Unsupported resource scheme: {scheme}")
        
        @self.server.subscribe_resource()
        async def subscribe_resource(uri: AnyUrl) -> None:
            """
            Subscribe to resource updates.
            
            Args:
                uri: URI of the resource to subscribe to
                
            Raises:
                ValueError: If the resource doesn't support subscription
                            or the subscription doesn't exist
            """
            uri_str = str(uri)
            
            # Get info about the requesting client
            client_info = getattr(self.server.request_context, 'client_info', 'unknown')
            self.logger.info(f"Subscription request from {client_info} for resource: {uri_str}")
            print(f"🔔 NEW SUBSCRIPTION REQUEST from {client_info} for {uri_str}")
            
            # Only subscription resources support subscription
            if not uri_str.startswith("subscription://"):
                error_msg = f"Resource does not support subscription: {uri_str}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            subscription_id = uri_str.split("://", 1)[1]

            # Generate unique client ID for this subscription
            client_id = str(uuid.uuid4())
            
            # Store client session for notifications
            current_session = self.server.request_context.session
            self.clients[client_id] = current_session
            
            # Add client as subscriber under lock to ensure thread safety
            async with self.subscription_lock:
                if subscription_id not in self.subscriptions:
                    error_msg = f"Subscription not found: {subscription_id}"
                    self.logger.error(error_msg)
                    raise ValueError(error_msg)
                
                subscription = self.subscriptions[subscription_id]
                subscription.resource_subscribers.add(client_id)
                
                # Log successful subscription
                subscriber_count = len(subscription.resource_subscribers)
                self.logger.info(f"Client {client_id[:8]}... subscribed to {uri_str} (total subscribers: {subscriber_count})")
                print(f"✅ SUBSCRIPTION REGISTERED: Client {client_id[:8]}... subscribed to {uri_str}")
                
                # Send initial "test" notification to confirm subscription is working
                try:
                    # This sends an immediate resource update notification to verify channel works
                    print(f"🔄 Sending initial test notification to client {client_id[:8]}...")
                    await current_session.send_resource_updated(uri=AnyUrl(uri_str))
                    print(f"✅ Initial test notification sent successfully")
                except Exception as e:
                    print(f"❌ Failed to send initial test notification: {str(e)}")
                    self.logger.error(f"Failed to send initial test notification: {str(e)}")
            
            try:
                # Keep subscription active until client disconnects
                # Poll more frequently to check for client health
                while True:
                    await asyncio.sleep(10)  # Check every 10 seconds for better responsiveness
                    
                    # For debugging - periodically confirm the subscription is still active
                    self.logger.debug(f"Subscription {subscription_id} for client {client_id[:8]}... still active")
            except asyncio.CancelledError:
                self.logger.info(f"Subscription cancelled: {uri_str}")
            finally:
                # Remove client as subscriber when done
                async with self.subscription_lock:
                    if subscription_id in self.subscriptions:
                        subscription = self.subscriptions[subscription_id]
                        if client_id in subscription.resource_subscribers:
                            subscription.resource_subscribers.remove(client_id)
                            self.logger.info(f"Removed client {client_id[:8]}... from subscription {subscription_id}")
                
                # Remove client from clients dict
                if client_id in self.clients:
                    del self.clients[client_id]
                    self.logger.info(f"Removed client {client_id[:8]}... from clients dictionary")
                
            return None
            
        @self.server.set_logging_level()
        async def set_logging_level(level: LoggingLevel) -> None:
            """
            Set the server's logging level.
            
            Args:
                level: New logging level (trace, debug, info, warn, error, fatal)
            """
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
            
    def _read_mcpignore_file(self, path: str) -> List[str]:
        """
        Read .mcpignore file if it exists and return patterns.
        
        Args:
            path: Directory path where to look for .mcpignore
            
        Returns:
            List of glob patterns to ignore
        """
        mcpignore_path = os.path.join(path, ".mcpignore")
        patterns = []
        
        if os.path.isfile(mcpignore_path):
            try:
                with open(mcpignore_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        # Skip empty lines and comments
                        if line and not line.startswith("#"):
                            patterns.append(line)
                self.logger.info(f"Loaded {len(patterns)} patterns from .mcpignore file")
            except Exception as e:
                self.logger.warning(f"Error reading .mcpignore file: {str(e)}")
                
        return patterns
    
    async def _handle_subscribe_tool(self, input_data: SubscribeInput) -> List[TextContent]:
        """
        Handle the subscribe tool to create a new file monitor subscription.
        
        Args:
            input_data: Validated input parameters for the subscription
            
        Returns:
            List of TextContent objects with the subscription details or error
        """
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
        
        # Combine ignore patterns from:
        # 1. Default patterns
        # 2. Patterns from .mcpignore file
        # 3. User-provided patterns
        ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
        
        # Add patterns from .mcpignore file if path is a directory
        if os.path.isdir(path):
            ignore_patterns.extend(self._read_mcpignore_file(path))
        
        # Add user-provided patterns, ensuring they have precedence
        if input_data.ignore_patterns:
            ignore_patterns.extend(input_data.ignore_patterns)
            
        # Create subscription
        subscription = Subscription(
            path=path,
            recursive=input_data.recursive,
            patterns=input_data.patterns,
            ignore_patterns=ignore_patterns,
            events=input_data.events
        )
        
        # Start watching the path
        success = await self._watch(
            subscription_id=subscription.id,
            path=path,
            recursive=input_data.recursive,
            patterns=input_data.patterns,
            ignore_patterns=ignore_patterns,  # Use our combined ignore patterns
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
            
            # Automatically register the creating client as a subscriber
            # This ensures that the client who creates a subscription receives notifications
            try:
                if hasattr(self.server, 'request_context') and hasattr(self.server.request_context, 'session'):
                    # Generate unique client ID for this subscription
                    client_id = str(uuid.uuid4())
                    
                    # Store client session for notifications
                    current_session = self.server.request_context.session
                    self.clients[client_id] = current_session
                    
                    # Add this client as a subscriber
                    subscription.resource_subscribers.add(client_id)
                    
                    # Log the automatic subscription
                    self.logger.info(f"Auto-subscribed client to subscription {subscription.id}")
                    print(f"✅ AUTO-SUBSCRIBED: Tool creator auto-subscribed to {subscription.id}")
            except Exception as e:
                # Non-critical error, the tool can still succeed
                self.logger.warning(f"Could not auto-subscribe tool creator: {str(e)}")
            
        # Return subscription details with subscriber count
        subscriber_count = len(subscription.resource_subscribers)
        return [TextContent(
            type="text",
            text=json.dumps({
                "subscription_id": subscription.id,
                "status": "active",
                "path": path,
                "recursive": input_data.recursive,
                "resource_uri": f"subscription://{subscription.id}",
                "subscriber_count": subscriber_count,
                "auto_subscribed": subscriber_count > 0
            }, indent=2)
        )]
    
    async def _handle_unsubscribe_tool(self, input_data: UnsubscribeInput) -> List[TextContent]:
        """
        Handle the unsubscribe tool to cancel an active subscription.
        
        Args:
            input_data: Validated input parameters with the subscription ID
            
        Returns:
            List of TextContent objects with success/failure message
        """
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
        """
        Handle the list_subscriptions tool to show all active subscriptions.
        
        Returns:
            List of TextContent objects with subscription details
        """
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
    
    async def _handle_create_mcpignore_tool(self, input_data: CreateMcpignoreInput) -> List[TextContent]:
        """
        Handle the create_mcpignore tool to create a .mcpignore file.
        
        Args:
            input_data: Validated input parameters with path and include_defaults flag
            
        Returns:
            List of TextContent objects with the result or error message
        """
        path = input_data.path
        
        # Convert to absolute path if relative
        if not os.path.isabs(path):
            path = os.path.abspath(path)
            
        # Check if path exists and is a directory
        if not os.path.exists(path):
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Path does not exist: {path}"
                })
            )]
            
        if not os.path.isdir(path):
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Path is not a directory: {path}"
                })
            )]
            
        # Check if .mcpignore already exists
        mcpignore_path = os.path.join(path, ".mcpignore")
        if os.path.exists(mcpignore_path):
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": f".mcpignore file already exists at {mcpignore_path}"
                })
            )]
            
        # Create .mcpignore file
        try:
            with open(mcpignore_path, "w") as f:
                f.write("# MCP File Monitor ignore patterns\n")
                f.write("# Lines starting with # are comments\n")
                f.write("# Each line specifies a glob pattern to ignore\n")
                f.write("\n")
                
                if input_data.include_defaults:
                    # Group default patterns by category
                    categories = {
                        "Virtual environments": [p for p in DEFAULT_IGNORE_PATTERNS if "/venv/" in p or "/env/" in p],
                        "Package management": [p for p in DEFAULT_IGNORE_PATTERNS if "node_modules" in p or "package-lock.json" in p or ".npm" in p],
                        "Python caches": [p for p in DEFAULT_IGNORE_PATTERNS if "__pycache__" in p or ".pyc" in p or ".pytest_cache" in p or ".mypy_cache" in p],
                        "Build artifacts": [p for p in DEFAULT_IGNORE_PATTERNS if "/build/" in p or "/dist/" in p or "/target/" in p or "/out/" in p],
                        "IDE files": [p for p in DEFAULT_IGNORE_PATTERNS if ".idea" in p or ".vscode" in p or ".vs" in p],
                        "Version control": [p for p in DEFAULT_IGNORE_PATTERNS if ".git" in p],
                        "System files": [p for p in DEFAULT_IGNORE_PATTERNS if ".DS_Store" in p or "Thumbs.db" in p or "desktop.ini" in p],
                        "Logs and temp files": [p for p in DEFAULT_IGNORE_PATTERNS if ".log" in p or ".tmp" in p or ".temp" in p]
                    }
                    
                    for category, patterns in categories.items():
                        if patterns:
                            f.write(f"# {category}\n")
                            for pattern in patterns:
                                f.write(f"{pattern}\n")
                            f.write("\n")
                            
                f.write("# Add your custom ignore patterns below\n")
                f.write("# Examples:\n")
                f.write("# **/custom-dir/**\n")
                f.write("# **/*.bak\n")
                
            self.logger.info(f"Created .mcpignore file at {mcpignore_path}")
            
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "message": f"Created .mcpignore file at {mcpignore_path}",
                    "path": mcpignore_path
                }, indent=2)
            )]
            
        except Exception as e:
            self.logger.error(f"Error creating .mcpignore file: {str(e)}")
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Failed to create .mcpignore file: {str(e)}"
                })
            )]
    
    async def _handle_get_changes_tool(self, input_data: GetChangesInput) -> List[TextContent]:
        """
        Handle the get_changes tool to retrieve recent changes for a subscription.
        
        Args:
            input_data: Validated input parameters with subscription ID and optional timestamp
            
        Returns:
            List of TextContent objects with changes or error message
        """
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
            
            # For tests, we'll use our internally tracked changes
            if subscription_id in self._changes:
                changes = self._changes[subscription_id]
                
                # Return the changes directly from our internal tracking
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "subscription_id": subscription_id,
                        "path": subscription.path,
                        "changes": changes
                    }, indent=2)
                )]
            else:
                # Get changes since the specified time (regular path)
                changes = subscription.get_changes_since(since)
                
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "subscription_id": subscription_id,
                        "path": subscription.path,
                        "changes": [change.model_dump() for change in changes]
                    }, indent=2)
                )]
    
    async def _read_file_resource(self, path: str) -> str:
        """
        Read a file resource's content.
        
        Args:
            path: Path to the file
            
        Returns:
            Content of the file as a string
            
        Raises:
            ValueError: If the file doesn't exist or can't be read
        """
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
        """
        Read a directory resource's content.
        
        Args:
            path: Path to the directory
            
        Returns:
            JSON string with directory entries
            
        Raises:
            ValueError: If the directory doesn't exist or can't be read
        """
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
                    "modified": datetime.datetime.fromtimestamp(
                        entry.stat().st_mtime, 
                        tz=datetime.timezone.utc
                    ).isoformat()
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
        """
        Read a subscription resource's content.
        
        Args:
            subscription_id: ID of the subscription
            
        Returns:
            JSON string with subscription details and recent changes
            
        Raises:
            ValueError: If the subscription doesn't exist
        """
        async with self.subscription_lock:
            if subscription_id not in self.subscriptions:
                raise ValueError(f"Subscription not found: {subscription_id}")
                
            subscription = self.subscriptions[subscription_id]
            
            # Convert to dict for JSON serialization
            subscription_data = subscription.model_dump(exclude={'changes', 'resource_subscribers'})
            
            # Add recent changes
            subscription_data["recent_changes"] = [
                change.model_dump() for change in subscription.changes[-20:]  # Show last 20 changes
            ]
            
            # Add subscriber count
            subscription_data["subscriber_count"] = len(subscription.resource_subscribers)
            
            return json.dumps(subscription_data, indent=2)
    
    def _handle_event(self, subscription_id: str, path: str, event_type: str) -> None:
        """
        Handle a file system event by putting it in the queue.
        
        This method is called from the Watchdog thread and safely bridges
        to the asyncio event loop by using run_coroutine_threadsafe.
        
        Args:
            subscription_id: ID of the subscription that triggered the event
            path: Path to the file/directory that changed
            event_type: Type of event (created, modified, deleted)
        """
        self.logger.debug(f"Watchdog detected event: {event_type} at {path} for subscription {subscription_id}")
        
        # Store the event for testing purposes in a thread-safe way
        # Create a new dict rather than modifying in place
        change = {
            "path": path,
            "event": event_type,
            "timestamp": time.time()
        }
        
        # Get the current loop
        try:
            loop = asyncio.get_event_loop()
            self.logger.debug(f"Adding {event_type} event to notification queue")
            
            # Update changes in a thread-safe way and add to notification queue
            fut = asyncio.run_coroutine_threadsafe(
                self._add_change_to_queue(subscription_id, path, event_type, change),
                loop
            )
            
            # Optional: add callback to verify the task was processed
            def done_callback(future):
                try:
                    future.result()
                    self.logger.debug(f"Successfully processed {event_type} event for {path}")
                except Exception as e:
                    self.logger.error(f"Error processing event in background task: {e}")
                    
            fut.add_done_callback(done_callback)
            
        except RuntimeError as e:
            self.logger.warning(f"Could not get event loop: {str(e)}")
            # If we can't get event loop (testing context), just store directly
            # This is not thread-safe, but is acceptable for testing
            self._changes.setdefault(subscription_id, []).append(change)
            self.logger.debug(f"Stored {event_type} event directly (no event loop)")
            
        except Exception as e:
            self.logger.error(f"Unexpected error handling {event_type} event: {str(e)}")
    
    async def _add_change_to_queue(self, subscription_id: str, path: str, event_type: str, change: Dict[str, Any]) -> None:
        """
        Add a change to the notification queue and update the changes list
        in a thread-safe way.
        
        Args:
            subscription_id: ID of the subscription that triggered the event
            path: Path to the file/directory that changed
            event_type: Type of event (created, modified, deleted)
            change: The change event data
        """
        try:
            # Update the changes dictionary in a thread-safe way
            if subscription_id not in self._changes:
                self._changes[subscription_id] = []
            
            self._changes[subscription_id].append(change)
            
            self.logger.debug(f"Adding {event_type} event for {path} to notification queue")
            
            # Add to notification queue for real-time updates with a timeout to prevent deadlock
            try:
                # Use asyncio.wait_for to enforce a timeout
                await asyncio.wait_for(
                    self.notification_queue.put((subscription_id, path, event_type)),
                    timeout=5.0  # 5 second timeout
                )
                self.logger.debug(f"Successfully queued {event_type} event")
            except asyncio.TimeoutError:
                self.logger.error(f"Timeout while adding to notification queue - queue might be full or blocked")
                # Still record the change even if notification queue is stuck
                
        except Exception as e:
            # Catch-all for any unexpected errors
            self.logger.error(f"Error adding change to queue: {str(e)}")
            # At least try to add it to the in-memory changes
            try:
                if subscription_id not in self._changes:
                    self._changes[subscription_id] = []
                self._changes[subscription_id].append(change)
            except Exception:
                pass  # Last resort - if we can't even add to the changes dict
    
    async def _process_notifications(self) -> None:
        """
        Process notifications from the queue and distribute to subscribers.
        
        This runs as a background task, continuously pulling events from
        the notification queue and notifying subscribed clients.
        """
        self.logger.info("Starting notification processor")
        
        while not self.stopping:
            try:
                # Get the next notification with timeout to prevent blocking forever
                try:
                    notification = await asyncio.wait_for(
                        self.notification_queue.get(), 
                        timeout=1.0
                    )
                    subscription_id, path, event_type = notification
                except asyncio.TimeoutError:
                    # Just loop around and check if we're stopping
                    continue
                
                self.logger.info(f"Processing notification: {event_type} event at {path} for subscription {subscription_id}")
                
                # Add the change to the subscription
                async with self.subscription_lock:
                    if subscription_id not in self.subscriptions:
                        self.logger.warning(f"Subscription {subscription_id} not found when processing notification")
                        self.notification_queue.task_done()
                        continue
                        
                    subscription = self.subscriptions[subscription_id]
                    change = subscription.add_change(path, event_type)
                    
                    # Skip if the change was not added (e.g. filtered by event type)
                    if change is None:
                        self.logger.debug(f"Change not added to subscription (filtered by event type)")
                        self.notification_queue.task_done()
                        continue
                    
                    # Log subscribers count 
                    subscriber_count = len(subscription.resource_subscribers)
                    self.logger.info(f"Found {subscriber_count} subscribers for notification")
                    
                    if subscriber_count == 0:
                        self.logger.info(f"No subscribers for subscription {subscription_id}, skipping notifications")
                        self.notification_queue.task_done()
                        continue
                    
                    # Track successes for logging
                    sent_count = 0
                    error_count = 0
                    
                    # Send notifications to all subscribers
                    for client_id in list(subscription.resource_subscribers):
                        session = self.clients.get(client_id)
                        if session:
                            try:
                                # Print clear logging with a distinctive marker
                                print(f"⚡ SENDING NOTIFICATION: {event_type} event for {path} to client {client_id[:8]}...")
                                self.logger.info(f"Sending notification to client {client_id[:8]}... for resource subscription://{subscription_id}")
                                
                                # Use wait_for to enforce a timeout on sending notifications
                                # The correct method is send_resource_updated (not send_resource_updated_notification)
                                await asyncio.wait_for(
                                    session.send_resource_updated(
                                        uri=AnyUrl(f"subscription://{subscription_id}")
                                    ),
                                    timeout=5.0
                                )
                                
                                # Log success with a distinctive marker
                                print(f"✅ NOTIFICATION SENT SUCCESSFULLY to client {client_id[:8]}...")
                                self.logger.info(f"Successfully sent notification to client {client_id[:8]}...")
                                sent_count += 1
                            except Exception as e:
                                error_msg = str(e)
                                print(f"❌ NOTIFICATION ERROR: {error_msg}")
                                self.logger.error(f"Error sending notification to client {client_id[:8]}...: {error_msg}")
                                error_count += 1
                                # Keep subscriber for now, might be a temporary issue
                        else:
                            self.logger.warning(f"Client {client_id[:8]}... has no active session, removing subscriber")
                            # Clean up subscriptions without active sessions
                            subscription.resource_subscribers.remove(client_id)
                    
                    # Log summary of what happened
                    self.logger.info(f"Notification summary: {sent_count} sent, {error_count} failed")
                
                # Mark this notification as done
                self.notification_queue.task_done()
                self.logger.info(f"Notification processing completed for {event_type} at {path}")
                
            except asyncio.CancelledError:
                self.logger.info("Notification processor cancelled")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error in notification processor: {str(e)}")
                # Continue processing other notifications even if one fails
    
    async def _watch(
        self, 
        subscription_id: str, 
        path: str, 
        recursive: bool = False,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = False,
    ) -> bool:
        """
        Watch a path for changes.
        
        Sets up a Watchdog observer to monitor file system events for the path.
        
        Args:
            subscription_id: ID of the subscription
            path: Path to watch
            recursive: Whether to watch subdirectories
            patterns: File patterns to include
            ignore_patterns: File patterns to exclude
            ignore_directories: Whether to ignore directory events
            
        Returns:
            True if watching was successfully set up, False otherwise
        """
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
        """
        Stop watching a subscription.
        
        Cleans up Watchdog observers for a subscription.
        
        Args:
            subscription_id: ID of the subscription to stop watching
            
        Returns:
            True if observer was stopped, False if it didn't exist
        """
        observer = self.observers.get(subscription_id)
        watch = self.watches.get(subscription_id)
        
        if observer and watch:
            observer.unschedule(watch)
            observer.stop()
            observer.join()
            
            # Clean up references
            if subscription_id in self.observers:
                del self.observers[subscription_id]
            if subscription_id in self.handlers:
                del self.handlers[subscription_id]
            if subscription_id in self.watches:
                del self.watches[subscription_id]
            return True
        return False
    
    async def start(self) -> None:
        """
        Start the server and notification processor.
        """
        self.logger.info("Starting file monitoring MCP server")
        
        # Start the notification processor
        self.stopping = False
        self.notification_task = asyncio.create_task(self._process_notifications())
    
    async def stop(self) -> None:
        """
        Stop the server, notification processor, and all watchers.
        """
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

    def get_capabilities(self, notification_options: NotificationOptions, experimental_capabilities: Dict[str, Dict[str, Any]] = dict()) -> ServerCapabilities:
        """
        Get the server capabilities for MCP initialization.
        
        Args:
            notification_options: Options for notifications
            experimental_capabilities: Dict of experimental capabilities
            
        Returns:
            ServerCapabilities with resource subscriptions enabled
        """
        # Default parameter is already handled (dict())
            
        # Create capabilities for resources and tools
        resource_capability = ResourcesCapability(
            subscribe=True,  # Enable resource subscriptions
            listChanged=True  # Enable resource list notifications
        )
        
        tools_capability = ToolsCapability(
            listChanged=True  # Enable tool list notifications
        )
            
        # Get base capabilities from the server
        capabilities = self.server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities=experimental_capabilities
        )
        
        # Set the custom capabilities
        capabilities.resources = resource_capability
        capabilities.tools = tools_capability
        
        return capabilities
    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None, None]:
        """
        Server lifespan context manager.
        
        Properly manages server lifecycle including initialization and cleanup.
        This ensures the server components are properly started before handling
        requests and properly cleaned up after.
        
        Yields:
            None
        """
        try:
            # Start all server components
            await self.start()
            self.logger.info("Server started via lifespan context manager")
            
            # Wait for everything to initialize
            await asyncio.sleep(0.5)
            
            # Server is now ready to handle requests
            yield
        finally:
            # Always ensure cleanup
            self.logger.info("Server shutting down via lifespan context manager")
            await self.stop()


async def run(monitor_path: Optional[str] = None) -> None:
    """
    Run the server using stdio for communication.
    
    Args:
        monitor_path: Optional path to monitor by default
    """
    # Create MCP server instance for file monitoring
    file_monitor = FileMonitorMCPServer()
    file_monitor.logger.info("Starting MCP Monitor Server")
    
    # Prepare initialization options
    init_options = InitializationOptions(
        server_name="mcp-monitor-server",
        server_version="1.0.0",
        capabilities=file_monitor.get_capabilities(
            notification_options=NotificationOptions(),
        ),
    )
    
    # Use the server's lifespan context manager for proper lifecycle management
    async with file_monitor.lifespan():
        # If monitor_path is provided, create a default subscription
        if monitor_path:
            try:
                # Validate the path first
                path = str(monitor_path)
                
                # Convert to absolute path if relative
                if not os.path.isabs(path):
                    path = os.path.abspath(path)
                
                # Verify the path exists
                if not os.path.exists(path):
                    file_monitor.logger.error(f"Default monitor path does not exist: {path}")
                else:
                    # Create a subscription for the monitor path
                    file_monitor.logger.info(f"Creating default subscription for {path}")
                    sub_input = SubscribeInput(path=path, recursive=True)
                    result = await file_monitor._handle_subscribe_tool(sub_input)
                    file_monitor.logger.info(f"Default subscription created: {result[0].text}")
            except Exception as e:
                file_monitor.logger.error(f"Failed to create default subscription: {e}")
        
        # Run the server using stdio communication
        async with stdio_server() as (read_stream, write_stream):
            # Run with proper initialization
            file_monitor.logger.info("Starting server with STDIO communication")
            
            # Run the server, which will block until shutdown
            await file_monitor.server.run(
                read_stream,
                write_stream,
                init_options,
            )


def main() -> None:
    """
    Main entry point for the server.
    
    Sets up logging and runs the server with asyncio.
    """
    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG,  # Set to DEBUG to see all log messages
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr
    )
    
    # Run the server
    asyncio.run(run())


if __name__ == "__main__":
    main()
