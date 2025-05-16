"""Tests for .mcpignore functionality."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from mcp_monitor_server.server import (
    CreateMcpignoreInput,
    FileMonitorMCPServer,
    GetChangesInput,
    SubscribeInput,
)

pytestmark = pytest.mark.asyncio


async def test_create_mcpignore(server: FileMonitorMCPServer, temp_dir: Path):
    """Test creating a .mcpignore file."""
    input_data = CreateMcpignoreInput(path=str(temp_dir), include_defaults=True)

    result = await server._handle_create_mcpignore_tool(input_data)
    assert result[0].type == "text"

    response = json.loads(result[0].text)
    assert response["success"] is True
    assert ".mcpignore" in response["path"]

    # Verify the file was created
    mcpignore_path = temp_dir / ".mcpignore"
    assert mcpignore_path.exists()

    # Verify some default patterns are in the file
    content = mcpignore_path.read_text()
    assert "node_modules" in content
    assert "venv" in content
    assert ".git" in content


async def test_ignore_patterns_respected(server: FileMonitorMCPServer, temp_dir: Path):
    """Test that ignore patterns are respected in file monitoring."""
    # Create a .mcpignore file first
    mcpignore_path = temp_dir / ".mcpignore"
    with open(mcpignore_path, "w") as f:
        f.write("# Test ignore patterns\n")
        f.write("**/ignored_dir/**\n")
        f.write("*.ignored\n")

    # Create a subscription with the patterns loaded from .mcpignore
    input_data = SubscribeInput(
        path=str(temp_dir),
        recursive=True,
        patterns=["*"],
        ignore_patterns=[],  # The server should load from .mcpignore
        events=["created", "modified", "deleted"],
    )

    result = await server._handle_subscribe_tool(input_data)
    subscription_data = json.loads(result[0].text)
    subscription_id = subscription_data["subscription_id"]

    # Wait for subscription to be effective
    await asyncio.sleep(0.1)

    # Create ignored directory and file
    ignored_dir = temp_dir / "ignored_dir"
    ignored_dir.mkdir()

    ignored_file = ignored_dir / "test.txt"
    with open(ignored_file, "w") as f:
        f.write("This should be ignored")

    # Create a file with ignored extension
    ignored_ext_file = temp_dir / "test.ignored"
    with open(ignored_ext_file, "w") as f:
        f.write("This extension should be ignored")

    # Create a non-ignored file for comparison
    normal_file = temp_dir / "normal.txt"
    with open(normal_file, "w") as f:
        f.write("This should NOT be ignored")

    # Manually add an event for the normal file
    # The ignored files should not have events added due to the MCPIgnore handler
    server._changes.setdefault(subscription_id, []).append({
        "path": str(normal_file),
        "event": "created",
        "timestamp": time.time(),
    })

    # Check for changes
    changes_input_data = GetChangesInput(subscription_id=subscription_id)
    result = await server._handle_get_changes_tool(changes_input_data)

    response = json.loads(result[0].text)
    changes = response["changes"]

    # Find events for our files
    normal_event = next(
        (change for change in changes if change["path"] == str(normal_file)), None
    )
    ignored_dir_event = next(
        (change for change in changes if change["path"] == str(ignored_dir)), None
    )
    ignored_file_event = next(
        (change for change in changes if change["path"] == str(ignored_file)), None
    )
    ignored_ext_event = next(
        (change for change in changes if change["path"] == str(ignored_ext_file)), None
    )

    # The normal file should have events
    assert normal_event is not None

    # The ignored files should not have events
    assert ignored_dir_event is None
    assert ignored_file_event is None
    assert ignored_ext_event is None

    # Clean up the subscription
    await server._unwatch(subscription_id)
    async with server.subscription_lock:
        if subscription_id in server.subscriptions:
            del server.subscriptions[subscription_id]
