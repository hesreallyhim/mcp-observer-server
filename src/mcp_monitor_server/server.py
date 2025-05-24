import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Iterable, Set, Dict, Union

from pydantic import AnyUrl
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import mcp.types as types
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.types import Resource, Tool, TextContent, Prompt, PromptArgument, ResourceContents

# Global state: mapping watched paths -> subscribed ServerSession objects
watched: Dict[Path, Set[ServerSession]] = {}

async def send_resources_list_changed_notification():
    """Send notification that the resource list has changed."""
    # Get all sessions that might be interested in resource list changes
    all_sessions = set()
    for sessions in watched.values():
        all_sessions.update(sessions)
    
    if not all_sessions:
        return
    
    # Create the notification
    notification = types.ResourceListChangedNotification(
        method="notifications/resources/list_changed"
    )
    
    # Send to all sessions
    for session in all_sessions:
        try:
            await session.send_notification(types.ServerNotification(root=notification))
        except Exception:
            # Ignore failed notifications to avoid breaking the operation
            pass

# Create MCP server
server: Server = Server(
    name="mcp-watch",
    version="0.1.0",
    instructions="Subscribe/unsubscribe to filesystem events via separate tools or resource methods",
)

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="subscribe",
            description="Subscribe to changes on a file or directory",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        ),
        Tool(
            name="unsubscribe",
            description="Unsubscribe from changes on a file or directory",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        ),
        Tool(
            name="list_watched",
            description="List all currently monitored paths and their subscriber counts",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="subscribe_default",
            description="Subscribe to the default watched.txt file for development",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
    ]

@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="file_changes",
            description="Get a summary of recent file changes in monitored paths",
            arguments=[
                PromptArgument(
                    name="path",
                    description="Optional specific path to check for changes (default: show all monitored paths)",
                    required=False
                )
            ]
        )
    ]

@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> types.GetPromptResult:
    if name != "file_changes":
        raise ValueError(f"Unknown prompt: {name}")
    
    args = arguments or {}
    specific_path = args.get("path")
    
    if specific_path:
        p = Path(specific_path).expanduser().resolve()
        if p in watched:
            prompt_text = f"You are monitoring file changes for: {p}\n\n"
            prompt_text += f"This path currently has {len(watched[p])} active subscribers.\n"
            prompt_text += "Use the subscribe/unsubscribe tools to manage monitoring of this path."
        else:
            prompt_text = f"Path {p} is not currently being monitored.\n"
            prompt_text += "Use the subscribe tool to start monitoring this path for changes."
    else:
        if watched:
            prompt_text = f"Currently monitoring {len(watched)} paths:\n\n"
            for path, sessions in watched.items():
                prompt_text += f"- {path} ({len(sessions)} subscribers)\n"
            prompt_text += "\nUse the subscribe/unsubscribe tools to manage these monitoring subscriptions."
        else:
            prompt_text = "No paths are currently being monitored.\n"
            prompt_text += "Use the subscribe tool to start monitoring file or directory changes."
    
    return types.GetPromptResult(
        description="File monitoring status and management",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=prompt_text)
            )
        ]
    )

@server.call_tool()
async def call_tool_handler(
    name: str,
    arguments: Dict[str, str] | None,
) -> list[TextContent]:
    args = arguments or {}
    path_str = args.get("path")
    session = server.request_context.session
    
    if not path_str and name not in ["list_watched", "subscribe_default"]:
        return [TextContent(type="text", text="Error: 'path' argument is required")]
    
    if name == "subscribe":
        assert path_str is not None, "path_str should not be None for subscribe tool"
        p = Path(path_str).expanduser().resolve()
        is_new_path = p not in watched
        watched.setdefault(p, set()).add(session)
        
        # Send notification if this is a new path being watched
        if is_new_path:
            asyncio.create_task(send_resources_list_changed_notification())
        
        return [TextContent(type="text", text=f"Subscribed to {p}")]
    elif name == "unsubscribe":
        assert path_str is not None, "path_str should not be None for unsubscribe tool"
        p = Path(path_str).expanduser().resolve()
        subs = watched.get(p)
        if subs and session in subs:
            subs.remove(session)
            if not subs:
                del watched[p]
            return [TextContent(type="text", text=f"Unsubscribed from {p}")]
        return [TextContent(type="text", text=f"Not subscribed to {p}")]
    elif name == "list_watched":
        if not watched:
            return [TextContent(type="text", text="No paths are currently being monitored")]
        
        result_lines = [f"Currently monitoring {len(watched)} paths:"]
        for path, sessions in watched.items():
            result_lines.append(f"- {path} ({len(sessions)} subscribers)")
        
        return [TextContent(type="text", text="\n".join(result_lines))]
    elif name == "subscribe_default":
        default_file = Path("src/mcp_monitor_server/watched.txt").expanduser().resolve()
        is_new_path = default_file not in watched
        watched.setdefault(default_file, set()).add(session)
        
        # Send notification if this is a new path being watched
        if is_new_path:
            asyncio.create_task(send_resources_list_changed_notification())
        
        return [TextContent(type="text", text=f"Subscribed to default file: {default_file}")]
    return [TextContent(type="text", text=f"Unknown tool: {name}")]

@server.subscribe_resource()
async def subscribe_resource_handler(uri: AnyUrl) -> None:
    if not uri.path:
        return
    p = Path(uri.path).resolve()
    session = server.request_context.session
    is_new_path = p not in watched
    watched.setdefault(p, set()).add(session)
    
    # Send notification if this is a new path being watched
    if is_new_path:
        asyncio.create_task(send_resources_list_changed_notification())

@server.unsubscribe_resource()
async def unsubscribe_resource_handler(uri: AnyUrl) -> None:
    if not uri.path:
        return
    p = Path(uri.path).resolve()
    session = server.request_context.session
    subs = watched.get(p)
    if subs and session in subs:
        subs.remove(session)
        if not subs:
            del watched[p]

@server.list_resources()
async def list_resources() -> list[Resource]:
    # Default development resource
    default_file = Path("src/mcp_monitor_server/watched.txt")
    resources = [
        Resource(
            uri=AnyUrl(f"file://{default_file.resolve()}"), 
            name="watched.txt (dev default)", 
            mimeType="text/plain"
        )
    ]
    
    # Add all currently watched paths
    resources.extend([
        Resource(uri=AnyUrl(f"file://{p}"), name=p.name or str(p), mimeType="text/plain")
        for p in watched
    ])
    
    return resources

@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return []

@server.read_resource() # type: ignore
async def read_resource(uri: AnyUrl):
    if not uri.path:
        raise Exception("Invalid resource URI")
    p = Path(uri.path).resolve()
    if not p.exists():
        raise Exception("Resource not found")
    
    # Check if current session is subscribed to this path
    session = server.request_context.session
    is_subscribed = p in watched and session in watched[p]
    
    if p.is_dir():
        content_text = "\n".join(child.name for child in p.iterdir())
    else:
        content_text = p.read_text()
    
    # Add subscription metadata as header
    # metadata_header = f"# Subscription Status: {'✓ Subscribed' if is_subscribed else '✗ Not subscribed'}\n# Path: {p}\n\n"
    # full_content = metadata_header + content_text
    
    resource_content = ResourceContents.model_validate({
        "uri": uri,
        # "mimeType": "text/plain",
        "content": content_text,
        "_meta": {
            "subscribed": is_subscribed
        }
    })
    
    return [resource_content]

class Watcher(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop

    def on_modified(self, event):
        ev_path = Path(str(event.src_path)).resolve()
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for p, subs in watched.items():
            if ev_path == p or (p.is_dir() and ev_path.is_relative_to(p)):
                for session in list(subs):
                    params = types.ResourceUpdatedNotificationParams.model_validate({
                        "uri": str(AnyUrl(f"file://{p}")),
                        "event_type": event.event_type,
                        "_meta": {"timestamp": ts}
                    })
                    notif = types.ResourceUpdatedNotification(
                        method="notifications/resources/updated",
                        params=params
                    )
                    self.loop.call_soon_threadsafe(
                        lambda session=session, notif=notif: asyncio.create_task(
                            session.send_notification(types.ServerNotification(root=notif))
                        )
                    )

async def main():
    loop = asyncio.get_running_loop()
    observer = Observer()
    observer.schedule(Watcher(loop), path=".", recursive=True)
    observer.start()

    caps = types.ServerCapabilities(
        prompts=types.PromptsCapability(listChanged=True),
        resources=types.ResourcesCapability(subscribe=True, listChanged=True),
        tools=types.ToolsCapability(listChanged=True),
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
