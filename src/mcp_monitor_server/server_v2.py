"""
MCP server implementation for file change monitoring.
"""
import asyncio
import datetime
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Resource, ResourceTemplate
from pydantic import BaseModel, Field

from .resources import ResourceManager
from .subscriptions import SubscriptionManager
from .watcher import FileWatcher


# Define Pydantic models for tool inputs and outputs
class SubscribeInput(BaseModel):
    path: str
    recursive: bool = False
    patterns: List[str] = Field(default_factory=lambda: ["*"])
    ignore_patterns: List[str] = Field(default_factory=list)
    events: List[str] = Field(default_factory=lambda: ["created", "modified", "deleted"])


class SubscribeOutput(BaseModel):
    subscription_id: str
    status: str
    path: str
    recursive: bool
    resource_uri: str


class UnsubscribeInput(BaseModel):
    subscription_id: str


class UnsubscribeOutput(BaseModel):
    success: bool
    message: str


class GetChangesInput(BaseModel):
    subscription_id: str
    since: Optional[datetime.datetime] = None


class ChangeOutput(BaseModel):
    path: str
    event: str
    timestamp: str


class GetChangesOutput(BaseModel):
    subscription_id: str
    changes: List[ChangeOutput]


class SubscriptionOutput(BaseModel):
    id: str
    path: str
    recursive: bool
    patterns: List[str]
    ignore_patterns: List[str]
    events: List[str]
    created_at: str


class ListSubscriptionsOutput(BaseModel):
    subscriptions: List[SubscriptionOutput]


@dataclass
class AppContext:
    """Type-safe application context for lifespan management."""
    subscription_manager: SubscriptionManager
    file_watcher: FileWatcher
    resource_manager: ResourceManager
    logger: logging.Logger
    notification_queue: asyncio.Queue
    clients: Dict[str, Any]


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """
    Manage application lifecycle with type-safe context.
    
    Handles initialization of subscription manager, file watcher,
    resource manager, and notification system.
    """
    # Setup logging
    logger = logging.getLogger("file-monitor-mcp")
    handler = logging.StreamHandler(sys.stderr)  # Log to stderr for MCP
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    
    logger.info("Initializing file monitoring MCP server")
    
    # Initialize components
    subscription_manager = SubscriptionManager()
    file_watcher = FileWatcher(subscription_manager)
    resource_manager = ResourceManager(subscription_manager)
    notification_queue = asyncio.Queue()
    clients = {}
    
    # Start the file watcher
    await file_watcher.start()
    
    # Start notification processor
    stopping = False
    
    async def process_notifications():
        while not stopping:
            try:
                # Get the next notification
                subscription_id, path, event_type = await notification_queue.get()
                
                # Get the subscription
                subscription = await subscription_manager.get_subscription(
                    subscription_id
                )
                
                if not subscription:
                    notification_queue.task_done()
                    continue
                
                # Get subscribers
                subscribers = await subscription_manager.get_subscribers(
                    subscription_id
                )
                
                # Send notification to all subscribers
                for client_id in subscribers:
                    session = clients.get(client_id)
                    if session:
                        try:
                            await session.send_resource_updated_notification(
                                uri=f"subscription://{subscription_id}"
                            )
                        except Exception as e:
                            logger.error(f"Error sending notification: {e}")
                
                notification_queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing notification: {e}")
    
    # Start notification task
    notification_task = asyncio.create_task(process_notifications())
    
    try:
        # Yield the application context
        yield AppContext(
            subscription_manager=subscription_manager,
            file_watcher=file_watcher,
            resource_manager=resource_manager,
            logger=logger,
            notification_queue=notification_queue,
            clients=clients
        )
    finally:
        # Cleanup on shutdown
        logger.info("Shutting down file monitoring MCP server")
        
        # Stop notification processing
        stopping = True
        notification_task.cancel()
        try:
            await notification_task
        except asyncio.CancelledError:
            pass
        
        # Stop file watcher
        await file_watcher.stop()


# Create the FastMCP app with lifespan support
app = FastMCP(
    title="File Monitor MCP Server", 
    lifespan=app_lifespan,
    dependencies=["watchdog", "anyio", "pydantic"]
)


# Register resource handlers
@app.list_resources()
async def list_resources(ctx: Context) -> List[Resource]:
    """List all available resources."""
    resource_manager = ctx.request_context.lifespan_context.resource_manager
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info("Listing resources")
    resources_data = await resource_manager.list_resources()
    return [
        Resource(
            uri=r["uri"],
            name=r["name"],
            mimeType=r.get("mimeType"),
            description=r.get("description")
        )
        for r in resources_data
    ]


@app.list_resource_templates()
async def list_resource_templates(ctx: Context) -> List[ResourceTemplate]:
    """List all resource templates."""
    resource_manager = ctx.request_context.lifespan_context.resource_manager
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info("Listing resource templates")
    templates_data = await resource_manager.list_resource_templates()
    return [
        ResourceTemplate(
            uriTemplate=t["uriTemplate"],
            name=t["name"],
            mimeType=t.get("mimeType"),
            description=t.get("description")
        )
        for t in templates_data
    ]


@app.read_resource()
async def read_resource(uri: str, ctx: Context) -> str:
    """Read a resource's content."""
    resource_manager = ctx.request_context.lifespan_context.resource_manager
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info(f"Reading resource: {uri}")
    content, mime_type = await resource_manager.read_resource(uri)
    return content


@app.subscribe_resource()
async def subscribe_resource(uri: str, ctx: Context) -> None:
    """Subscribe to resource updates."""
    resource_manager = ctx.request_context.lifespan_context.resource_manager
    logger = ctx.request_context.lifespan_context.logger
    clients = ctx.request_context.lifespan_context.clients
    
    logger.info(f"Subscribing to resource: {uri}")
    
    # Generate unique client ID for this subscription
    client_id = str(uuid.uuid4())
    
    # Store client session for notifications
    clients[client_id] = ctx.request_context.session
    
    # Subscribe to the resource
    async with resource_manager.subscribe_resource(uri, client_id):
        try:
            # Keep subscription active until client disconnects
            while True:
                await asyncio.sleep(60)  # Check every minute
        except asyncio.CancelledError:
            logger.info(f"Subscription cancelled: {uri}")


# Register tool handlers
@app.tool("subscribe", "Create a new subscription to monitor file changes")
async def subscribe_tool(input_data: SubscribeInput, ctx: Context) -> SubscribeOutput:
    """Create a new subscription to monitor file changes."""
    subscription_manager = ctx.request_context.lifespan_context.subscription_manager
    file_watcher = ctx.request_context.lifespan_context.file_watcher
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info(f"Creating subscription for path: {input_data.path}")
    
    path = input_data.path
    
    # Convert to absolute path if relative
    if not os.path.isabs(path):
        path = os.path.abspath(path)
        
    # Check if path exists
    if not os.path.exists(path):
        raise ValueError(f"Path does not exist: {path}")
        
    # Create subscription
    subscription = await subscription_manager.create_subscription(
        path=path,
        recursive=input_data.recursive,
        patterns=input_data.patterns,
        ignore_patterns=input_data.ignore_patterns,
        events=input_data.events
    )
    
    # Start watching the path
    success = await file_watcher.watch(
        subscription_id=subscription.id,
        path=path,
        recursive=input_data.recursive,
        patterns=input_data.patterns,
        ignore_patterns=input_data.ignore_patterns,
        ignore_directories=False
    )
    
    if not success:
        await subscription_manager.delete_subscription(subscription.id)
        raise ValueError(f"Failed to watch path: {path}")
        
    # Return subscription details
    return SubscribeOutput(
        subscription_id=subscription.id,
        status="active",
        path=path,
        recursive=input_data.recursive,
        resource_uri=f"subscription://{subscription.id}"
    )


@app.tool("unsubscribe", "Cancel an active subscription")
async def unsubscribe_tool(input_data: UnsubscribeInput, ctx: Context) -> UnsubscribeOutput:
    """Cancel an active subscription."""
    subscription_manager = ctx.request_context.lifespan_context.subscription_manager
    file_watcher = ctx.request_context.lifespan_context.file_watcher
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info(f"Unsubscribing from subscription: {input_data.subscription_id}")
    
    # Stop watching
    await file_watcher.unwatch(input_data.subscription_id)
    
    # Delete subscription
    success, message = await subscription_manager.delete_subscription(
        input_data.subscription_id
    )
    
    return UnsubscribeOutput(
        success=success,
        message=message
    )


@app.tool("list_subscriptions", "List all active subscriptions")
async def list_subscriptions_tool(ctx: Context) -> ListSubscriptionsOutput:
    """List all active subscriptions."""
    subscription_manager = ctx.request_context.lifespan_context.subscription_manager
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info("Listing subscriptions")
    
    subscriptions = await subscription_manager.list_subscriptions()
    
    return ListSubscriptionsOutput(
        subscriptions=[
            SubscriptionOutput(
                id=sub.id,
                path=sub.path,
                recursive=sub.recursive,
                patterns=sub.patterns,
                ignore_patterns=sub.ignore_patterns,
                events=sub.events,
                created_at=sub.created_at.isoformat()
            )
            for sub in subscriptions
        ]
    )


@app.tool("get_changes", "Retrieve recent changes for a specific subscription")
async def get_changes_tool(input_data: GetChangesInput, ctx: Context) -> GetChangesOutput:
    """Retrieve recent changes for a specific subscription."""
    subscription_manager = ctx.request_context.lifespan_context.subscription_manager
    logger = ctx.request_context.lifespan_context.logger
    
    logger.info(f"Getting changes for subscription: {input_data.subscription_id}")
    
    # Get the subscription
    subscription = await subscription_manager.get_subscription(
        input_data.subscription_id
    )
    
    if not subscription:
        raise ValueError(f"Subscription not found: {input_data.subscription_id}")
        
    # Get changes since the specified time
    changes = subscription.get_changes_since(input_data.since)
    
    return GetChangesOutput(
        subscription_id=input_data.subscription_id,
        changes=[
            ChangeOutput(
                path=change.path,
                event=change.event,
                timestamp=change.timestamp.isoformat()
            )
            for change in changes
        ]
    )


def main():
    """Run the server using stdio transport."""
    import asyncio

    from mcp.server.stdio import stdio_server
    
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream
            )
    
    asyncio.run(run())


if __name__ == "__main__":
    main()
