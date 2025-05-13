"""Test configuration and fixtures for mcp-monitor-server."""
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

from mcp_monitor_server.server import FileMonitorMCPServer, SubscribeInput


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

@pytest_asyncio.fixture
async def server() -> AsyncGenerator[FileMonitorMCPServer, None]:
    """Create and initialize an MCP server instance."""
    server = FileMonitorMCPServer()
    await server.start()
    yield server
    await server.stop()

@pytest_asyncio.fixture
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

# We're removing the custom event_loop fixture and will use the one provided by pytest-asyncio instead
# To set the scope, we'll update pyproject.toml
