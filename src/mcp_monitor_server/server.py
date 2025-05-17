import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, Union

from pydantic import AnyUrl
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import mcp.types as types
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.types import Resource, Tool

# Global state: mapping watched paths -> subscribed request IDs
watched: Dict[Path, Set[Union[str, int]]] = {}
sessions_by_request: Dict[Union[str, int], ServerSession] = {}

# Create MCP server
server: Server = Server(
    name="mcp-watch",
    version="1.0.0",
    instructions="Subscribe/unsubscribe to filesystem events via separate tools"
)

@server.list_tools()
async def list_tools() -> list[Tool]:
    # Declare two distinct tools with JSON Schema for inputs
    return [
        Tool(
            name="subscribe",
            description="Subscribe to changes on a file or directory",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filesystem path to watch"}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="unsubscribe",
            description="Unsubscribe from changes on a file or directory",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filesystem path to unwatch"}
                },
                "required": ["path"]
            }
        ),
    ]

@server.call_tool()
async def call_tool_handler(
    name: str,
    arguments: Dict[str, str] | None
) -> list[types.TextContent]:
    args = arguments or {}
    path_str = args.get("path")
    if not path_str:
        return [types.TextContent(type="text", text="Error: 'path' argument is required")]
    p = Path(path_str).expanduser().resolve()
    # Track the request and session
    rid = server.request_context.request_id
    session = server.request_context.session
    sessions_by_request[rid] = session

    if name == "subscribe":
        watched.setdefault(p, set()).add(rid)
        return [types.TextContent(type="text", text=f"Subscribed to {p}")]
    elif name == "unsubscribe":
        subs = watched.get(p)
        if subs and rid in subs:
            subs.remove(rid)
            if not subs:
                del watched[p]
            sessions_by_request.pop(rid, None)
            return [types.TextContent(type="text", text=f"Unsubscribed from {p}")]
        return [types.TextContent(type="text", text=f"Not subscribed to {p}")]
    # Handle unknown tool names
    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

@server.list_resources()
async def list_resources() -> list[Resource]:
    # List currently watched paths as MCP resources
    return [
        Resource(
            uri=AnyUrl(f"file://{p}"),
            name=p.name or str(p),
            mimeType="text/plain"
        )
        for p in watched
    ]

@server.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    # Ensure URI has a path
    if not uri.path:
        raise Exception("Invalid resource URI: missing path")
    p = Path(uri.path)
    if p.exists():
        if p.is_dir():
            return "".join(child.name for child in p.iterdir())
        return p.read_text()
    raise Exception("Resource not found")

# File-change watcher pushes notifications on events
class Watcher(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop

    def on_modified(self, event):
        ev_path = Path(str(event.src_path)).resolve()
        ts = datetime.utcnow().isoformat() + "Z"
        for p, subs in watched.items():
            if p == ev_path or (p.is_dir() and ev_path.is_relative_to(p)):
                for rid in list(subs):
                    session = sessions_by_request.get(rid)
                    if not session:
                        continue
                    # Build params via Pydantic model_validate to ensure correct typing
                    params = types.ResourceUpdatedNotificationParams.model_validate({
                        "uri": str(AnyUrl(f"file://{p}")),
                        "event_type": event.event_type,
                        "_meta": {"timestamp": ts}
                    })
                    notif = types.ResourceUpdatedNotification(
                        method="notifications/resources/updated",
                        params=params
                    )
                    # Schedule notification send on the correct session
                    self.loop.call_soon_threadsafe(
                        lambda session=session, notif=notif: asyncio.create_task(
                            session.send_notification(types.ServerNotification(root=notif))
                        )
                    )

async def main():
    # Start filesystem observer
    loop = asyncio.get_running_loop()
    observer = Observer()
    observer.schedule(Watcher(loop), path=".", recursive=True)
    observer.start()

    # Advertise tool and resource capabilities
    caps = types.ServerCapabilities(
        prompts=None,
        resources=types.ResourcesCapability(subscribe=True, listChanged=False),
        tools=types.ToolsCapability(listChanged=False),
        logging=None,
        experimental={}
    )
    init_opts = InitializationOptions(
        server_name=server.name,
        server_version=server.version or "0.1.0",
        capabilities=caps,
        instructions=server.instructions
    )

    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, init_opts)
    finally:
        observer.stop()
        observer.join()

if __name__ == "__main__":
    asyncio.run(main())
