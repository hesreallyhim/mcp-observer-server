import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Set, Dict

from pydantic import AnyUrl
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import mcp.types as types
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.types import Resource, Tool, TextContent, Prompt, PromptArgument

# Global state: mapping watched paths -> subscribed ServerSession objects
watched: Dict[Path, Set[ServerSession]] = {}

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
    arguments: Dict[str, str] | None
) -> list[TextContent]:
    args = arguments or {}
    path_str = args.get("path")
    if not path_str:
        return [TextContent(type="text", text="Error: 'path' argument is required")]
    p = Path(path_str).expanduser().resolve()
    session = server.request_context.session
    if name == "subscribe":
        watched.setdefault(p, set()).add(session)
        return [TextContent(type="text", text=f"Subscribed to {p}")]
    elif name == "unsubscribe":
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
    return [TextContent(type="text", text=f"Unknown tool: {name}")]

@server.subscribe_resource()
async def subscribe_resource_handler(uri: AnyUrl) -> None:
    if not uri.path:
        return
    p = Path(uri.path).resolve()
    session = server.request_context.session
    watched.setdefault(p, set()).add(session)

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
    return [
        Resource(uri=AnyUrl(f"file://{p}"), name=p.name or str(p), mimeType="text/plain")
        for p in watched
    ]

@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return []

@server.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    if not uri.path:
        raise Exception("Invalid resource URI")
    p = Path(uri.path)
    if not p.exists():
        raise Exception("Resource not found")
    if p.is_dir():
        return "\n".join(child.name for child in p.iterdir())
    return p.read_text()

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
