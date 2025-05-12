"""
MCP Monitor Server - File monitoring server implementation using Model Context Protocol
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, AsyncIterator

from mcp.server.fastmcp import Context, FastMCP, Image
from pydantic import BaseModel, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# Configure logging
logger = logging.getLogger(__name__)

# MCP server instance
mcp = FastMCP("MCP Monitor Server")


class Subscription(BaseModel):
    """Model for file monitoring subscriptions."""
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    path: str
    recursive: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    last_modified: datetime = Field(default_factory=datetime.now)
    patterns: List[str] = Field(default_factory=list)
    ignore_patterns: List[str] = Field(default_factory=list)
    
    # A set of client IDs subscribed to this path
    clients: Set[str] = Field(default_factory=set)


class FileChange(BaseModel):
    """Model for file change events."""
    
    path: str
    event_type: str  # created, modified, deleted, moved
    is_directory: bool
    timestamp: datetime = Field(default_factory=datetime.now)
    source_path: Optional[str] = None  # Only for moved events


@dataclass
class MonitorServer:
    """File monitoring server that watches for file changes."""
    
    # Path to monitor
    base_path: Path
    
    # Observer for watching file system events
    observer: Observer = None
    
    # Mapping of subscription IDs to subscription objects
    subscriptions: Dict[str, Subscription] = None
    
    # Map of paths to subscription IDs that are monitoring them
    path_subscriptions: Dict[str, Set[str]] = None
    
    # Store recent changes for each subscription
    recent_changes: Dict[str, List[FileChange]] = None
    
    # Maximum number of changes to keep per subscription
    max_changes_per_subscription: int = 100
    
    def __post_init__(self):
        self.observer = Observer()
        self.subscriptions = {}
        self.path_subscriptions = {}
        self.recent_changes = {}


class FileEventHandler(FileSystemEventHandler):
    """Handler for file system events."""
    
    def __init__(self, server: MonitorServer):
        self.server = server
    
    def dispatch(self, event: FileSystemEvent):
        """
        Dispatch events to the appropriate handler method and notify subscribers.
        """
        super().dispatch(event)
        
        # Common attributes for FileChange
        is_directory = event.is_directory
        path = event.src_path
        
        # Determine event type
        if hasattr(event, 'dest_path'):  # Moved event
            event_type = "moved"
            file_change = FileChange(
                path=event.dest_path,
                event_type=event_type,
                is_directory=is_directory,
                source_path=path
            )
        elif event.event_type == 'deleted':
            event_type = "deleted"
            file_change = FileChange(
                path=path,
                event_type=event_type,
                is_directory=is_directory
            )
        elif event.event_type == 'created':
            event_type = "created"
            file_change = FileChange(
                path=path,
                event_type=event_type,
                is_directory=is_directory
            )
        elif event.event_type == 'modified':
            event_type = "modified"
            file_change = FileChange(
                path=path,
                event_type=event_type,
                is_directory=is_directory
            )
        else:
            # Skip other events
            return
        
        # Record the change for relevant subscriptions
        self._record_change(file_change)
    
    def _record_change(self, change: FileChange):
        """Record a file change for all relevant subscriptions."""
        path = Path(change.path)
        rel_path = str(path)
        
        # Find all subscriptions that should be notified about this change
        for sub_id, subscription in self.server.subscriptions.items():
            sub_path = Path(subscription.path)
            
            # Check if this path is within the subscription's monitored path
            try:
                # For relative paths, use resolve to check if it's a child
                if Path(subscription.path).is_absolute():
                    is_child = Path(change.path).is_relative_to(Path(subscription.path))
                else:
                    # For non-absolute paths, do string-based check
                    sub_str = str(sub_path)
                    path_str = str(path)
                    is_child = path_str.startswith(sub_str)
                
                if is_child:
                    # Add to recent changes for this subscription
                    if sub_id not in self.server.recent_changes:
                        self.server.recent_changes[sub_id] = []
                    
                    # Add to front of the list (newest first)
                    self.server.recent_changes[sub_id].insert(0, change)
                    
                    # Limit the number of changes stored
                    if len(self.server.recent_changes[sub_id]) > self.server.max_changes_per_subscription:
                        self.server.recent_changes[sub_id].pop()
                    
                    logger.debug(f"Recorded {change.event_type} event for subscription {sub_id}: {rel_path}")
            except Exception as e:
                logger.error(f"Error handling change notification: {e}")


# Create a global server instance
_server: Optional[MonitorServer] = None


@asynccontextmanager
async def monitor_lifespan(server: FastMCP) -> AsyncIterator[MonitorServer]:
    """Setup and teardown for the monitoring server."""
    global _server

    base_path = server.state.get("base_path", Path.cwd())
    _server = MonitorServer(base_path=base_path)

    # Start file system observer
    event_handler = FileEventHandler(_server)
    _server.observer.schedule(event_handler, str(base_path), recursive=True)
    _server.observer.start()

    logger.info(f"Started file monitoring for {base_path}")

    try:
        yield _server
    finally:
        # Stop observer on shutdown
        if _server.observer.is_alive():
            _server.observer.stop()
            _server.observer.join()
        logger.info("Stopped file monitoring")


# Resources

@mcp.resource("file://{path}")
def get_file_content(path: str) -> str:
    """Get the contents of a file."""
    try:
        file_path = Path(path)
        if not file_path.exists():
            return f"Error: File {path} does not exist"
        
        if file_path.is_dir():
            return f"Error: {path} is a directory, not a file"
        
        # Read file content
        return file_path.read_text()
    except Exception as e:
        logger.error(f"Error reading file {path}: {e}")
        return f"Error reading file: {str(e)}"


@mcp.resource("dir://{path}")
def get_directory_listing(path: str) -> str:
    """Get a directory listing with metadata."""
    try:
        dir_path = Path(path)
        if not dir_path.exists():
            return f"Error: Directory {path} does not exist"
        
        if not dir_path.is_dir():
            return f"Error: {path} is a file, not a directory"
        
        # Get directory contents
        entries = list(dir_path.iterdir())
        
        # Format as a readable listing
        result = [f"Directory listing for {path}:"]
        result.append("Name | Type | Size | Last Modified")
        result.append("----|------|------|---------------")
        
        for entry in sorted(entries, key=lambda e: e.name):
            entry_type = "Dir" if entry.is_dir() else "File"
            size = "" if entry.is_dir() else f"{entry.stat().st_size} bytes"
            last_modified = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            result.append(f"{entry.name} | {entry_type} | {size} | {last_modified}")
        
        return "\n".join(result)
    except Exception as e:
        logger.error(f"Error listing directory {path}: {e}")
        return f"Error listing directory: {str(e)}"


@mcp.resource("subscription://{subscription_id}")
def get_subscription_info(subscription_id: str) -> str:
    """Get information about a subscription."""
    if not _server:
        return "Error: Server not initialized yet"
    
    if subscription_id not in _server.subscriptions:
        return f"Error: Subscription with ID {subscription_id} not found"
    
    subscription = _server.subscriptions[subscription_id]
    return (
        f"Subscription: {subscription_id}\n"
        f"Path: {subscription.path}\n"
        f"Recursive: {subscription.recursive}\n"
        f"Created: {subscription.created_at}\n"
        f"Last modified: {subscription.last_modified}\n"
        f"Include patterns: {', '.join(subscription.patterns) if subscription.patterns else 'All'}\n"
        f"Ignore patterns: {', '.join(subscription.ignore_patterns) if subscription.ignore_patterns else 'None'}\n"
    )


# Tools

@mcp.tool()
def subscribe(path: str, recursive: bool = False, patterns: List[str] = None, ignore_patterns: List[str] = None, ctx: Context = None) -> str:
    """
    Create a subscription to monitor file changes.
    
    Args:
        path: The path to monitor
        recursive: Whether to recursively monitor subdirectories
        patterns: Patterns to include (e.g., ["*.py", "*.txt"])
        ignore_patterns: Patterns to ignore (e.g., ["*.pyc", "*.log"])
        ctx: MCP context
    
    Returns:
        Subscription ID
    """
    if not _server:
        return "Error: Server not initialized yet"
    
    # Create new subscription
    subscription = Subscription(
        path=path,
        recursive=recursive,
        patterns=patterns or [],
        ignore_patterns=ignore_patterns or []
    )
    
    # Add client ID to subscribed clients
    if ctx and ctx.request_context and hasattr(ctx.request_context, "client_id"):
        subscription.clients.add(ctx.request_context.client_id)
    
    # Store subscription
    _server.subscriptions[subscription.id] = subscription
    
    # Update path mapping
    if path not in _server.path_subscriptions:
        _server.path_subscriptions[path] = set()
    _server.path_subscriptions[path].add(subscription.id)
    
    # Initialize recent changes list
    _server.recent_changes[subscription.id] = []
    
    logger.info(f"Created subscription {subscription.id} for path {path}")
    
    return subscription.id


@mcp.tool()
def unsubscribe(subscription_id: str) -> str:
    """
    Cancel a file monitoring subscription.
    
    Args:
        subscription_id: The ID of the subscription to cancel
    
    Returns:
        Confirmation message
    """
    if not _server:
        return "Error: Server not initialized yet"
    
    if subscription_id not in _server.subscriptions:
        return f"Error: Subscription with ID {subscription_id} not found"
    
    # Get the subscription
    subscription = _server.subscriptions[subscription_id]
    
    # Remove from path mapping
    if subscription.path in _server.path_subscriptions:
        _server.path_subscriptions[subscription.path].discard(subscription_id)
        if not _server.path_subscriptions[subscription.path]:
            del _server.path_subscriptions[subscription.path]
    
    # Remove from subscriptions
    del _server.subscriptions[subscription_id]
    
    # Remove from recent changes
    if subscription_id in _server.recent_changes:
        del _server.recent_changes[subscription_id]
    
    logger.info(f"Canceled subscription {subscription_id}")
    
    return f"Subscription {subscription_id} successfully canceled"


@mcp.tool()
def list_subscriptions() -> str:
    """
    List all active subscriptions.
    
    Returns:
        A formatted list of active subscriptions
    """
    if not _server:
        return "Error: Server not initialized yet"
    
    if not _server.subscriptions:
        return "No active subscriptions"
    
    result = ["Active subscriptions:"]
    result.append("ID | Path | Recursive | Created")
    result.append("----|------|----------|--------")
    
    for sub_id, subscription in _server.subscriptions.items():
        result.append(f"{sub_id} | {subscription.path} | {subscription.recursive} | {subscription.created_at}")
    
    return "\n".join(result)


@mcp.tool()
def get_changes(subscription_id: str, limit: int = 10) -> str:
    """
    Get recent file changes for a subscription.
    
    Args:
        subscription_id: The ID of the subscription to query
        limit: Maximum number of changes to return
    
    Returns:
        A formatted list of recent changes
    """
    if not _server:
        return "Error: Server not initialized yet"
    
    if subscription_id not in _server.subscriptions:
        return f"Error: Subscription with ID {subscription_id} not found"
    
    if subscription_id not in _server.recent_changes or not _server.recent_changes[subscription_id]:
        return f"No changes detected for subscription {subscription_id}"
    
    # Get changes (limited)
    changes = _server.recent_changes[subscription_id][:limit]
    
    result = [f"Recent changes for subscription {subscription_id}:"]
    result.append("Time | Type | Path | Is Directory")
    result.append("-----|------|------|-------------")
    
    for change in changes:
        timestamp = change.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        result.append(f"{timestamp} | {change.event_type} | {change.path} | {change.is_directory}")
    
    return "\n".join(result)


async def serve(monitor_path: Path):
    """
    Start the MCP Monitor Server.
    
    Args:
        monitor_path: Base path to monitor for file changes
    """
    # Add the monitor path to the server state
    mcp.state["base_path"] = monitor_path
    
    # Set up the lifespan context for the server
    mcp.lifespan = monitor_lifespan
    
    # Run the server
    await mcp.run_async()