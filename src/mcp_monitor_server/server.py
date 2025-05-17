import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Set

from pydantic import AnyUrl
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
import mcp.types as types
from mcp.types import ServerNotification, Resource
from mcp.server.stdio import stdio_server

# Configuration: watch a file next to this script
HERE = Path(__file__).parent.resolve()
FILE_PATH = HERE / "watched.txt"
RESOURCE_URI = f"file://{FILE_PATH}"

# Global state: subscribed sessions
subscriptions: Set[ServerSession] = set()

# Create MCP server
server: Server = Server(
    name="mcp-filewatch",
    version="1.0.0",
    instructions="A minimal file-watch MCP server over stdio"
)

@server.list_resources()
async def list_resources() -> list[Resource]:
    # Advertise the single watched file
    return [Resource(uri=AnyUrl(RESOURCE_URI), name="Watched File", mimeType="text/plain")]

@server.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    if str(uri) == RESOURCE_URI:
        return FILE_PATH.read_text()
    raise Exception("Resource not found")

@server.subscribe_resource()
async def subscribe(uri: AnyUrl) -> None:
    if str(uri) == RESOURCE_URI:
        # Record the subscribing session
        subscriptions.add(server.request_context.session)

@server.unsubscribe_resource()
async def unsubscribe(uri: AnyUrl) -> None:
    if str(uri) == RESOURCE_URI:
        subscriptions.discard(server.request_context.session)

# File-change watcher to push notifications
class FileWatcher(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop

    def on_any_event(self, event):
        # Only notify on changes to our watched file
        if Path(str(event.src_path)).resolve() == FILE_PATH.resolve():
            timestamp = datetime.utcnow().isoformat() + "Z"
            for session in list(subscriptions):
                params = types.ResourceUpdatedNotificationParams.model_validate({
                    "uri": RESOURCE_URI,
                    "_meta": {"timestamp": timestamp, "event_type": event.event_type}
                })
                notif = types.ResourceUpdatedNotification(
                    method="notifications/resources/updated",
                    params=params
                )
                # Schedule the notification to be sent via the session
                self.loop.call_soon_threadsafe(
                    lambda session=session, notif=notif: asyncio.create_task(
                        session.send_notification(ServerNotification(root=notif))
                    )
                )

async def main():
    # Initialization options with subscribe capability
    capabilities = types.ServerCapabilities(
        prompts=None,
        resources=types.ResourcesCapability(subscribe=True, listChanged=False),
        tools=None,
        logging=None,
        experimental={}
    )
    init_opts = InitializationOptions(
        server_name=server.name,
        server_version=(server.version or "0.1.0"),
        capabilities=capabilities,
        instructions=server.instructions,
    )

    # Ensure watched file exists
    FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FILE_PATH.touch(exist_ok=True)

    loop = asyncio.get_running_loop()
    observer = Observer()
    observer.schedule(FileWatcher(loop), path=str(HERE), recursive=False)
    observer.start()

    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, init_opts)
    finally:
        observer.stop()
        observer.join()

if __name__ == "__main__":
    asyncio.run(main())
