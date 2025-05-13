"""Test configuration and fixtures for mcp-monitor-server."""
import os
import tempfile
import asyncio
from pathlib import Path
import pytest
import shutil
import time
from typing import AsyncGenerator, Generator

from mcp_monitor_server.server import (
    FileMonitorMCPServer,
    SubscribeInput,
    FileChange
)

@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for file monitoring tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)
        yield temp_path

@pytest.fixture
def test_file(temp_dir: Path) -> Generator[Path, None, None]:
    """Create a test file in the temporary directory."""
    file_path = temp_dir / "test_file.txt"
    with open(file_path, "w") as f:
        f.write("Test content")
    yield file_path

@pytest.fixture
async def server() -> AsyncGenerator[FileMonitorMCPServer, None]:
    """Create and initialize an MCP server instance."""
    server = FileMonitorMCPServer()
    await server.start()
    yield server
    await server.stop()

@pytest.fixture
async def subscription(server: FileMonitorMCPServer, temp_dir: Path) -> AsyncGenerator[str, None]:
    """Create a test subscription."""
    input_data = SubscribeInput(
        path=str(temp_dir),
        recursive=True,
        patterns=["*"],
        ignore_patterns=[],
        events=["created", "modified", "deleted"]
    )
    
    result = await server._handle_subscribe_tool(input_data)
    subscription_id = result[0].text
    # Extract subscription_id from the JSON response
    import json
    subscription_data = json.loads(subscription_id)
    subscription_id = subscription_data["subscription_id"]
    
    yield subscription_id
    
    # Clean up subscription
    await server._unwatch(subscription_id)
    async with server.subscription_lock:
        if subscription_id in server.subscriptions:
            del server.subscriptions[subscription_id]

# Helper functions for tests
def wait_for_file_operation(path: Path, timeout: float = 1.0) -> None:
    """Wait for a file operation to complete."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if path.exists():
            break
        time.sleep(0.1)

@pytest.fixture
def event_loop():
    """Create an event loop for each test."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()