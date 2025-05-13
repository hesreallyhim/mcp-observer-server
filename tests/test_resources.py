"""Tests for resource functionality."""
import json
from pathlib import Path

import pytest

from mcp_monitor_server.server import FileMonitorMCPServer

pytestmark = pytest.mark.asyncio

async def test_read_file_resource(server: FileMonitorMCPServer, test_file: Path):
    """Test reading a file resource."""
    # Read the file resource
    result = await server._read_file_resource(str(test_file))
    assert "Test content" in result

async def test_read_nonexistent_file_resource(server: FileMonitorMCPServer, temp_dir: Path):
    """Test reading a non-existent file resource."""
    nonexistent_file = temp_dir / "nonexistent.txt"
    
    # Read the non-existent file resource
    with pytest.raises(ValueError) as excinfo:
        await server._read_file_resource(str(nonexistent_file))
    
    assert "not found" in str(excinfo.value)

async def test_read_directory_resource(server: FileMonitorMCPServer, temp_dir: Path, test_file: Path):
    """Test reading a directory resource."""
    # Read the directory resource
    result = await server._read_directory_resource(str(temp_dir))
    
    # Parse the JSON response
    dir_data = json.loads(result)
    
    # Verify the directory entries
    assert dir_data["path"] == str(temp_dir)
    assert "entries" in dir_data
    
    # Find the test file in the entries
    test_file_entry = next(
        (entry for entry in dir_data["entries"] if entry["path"] == str(test_file)),
        None
    )
    assert test_file_entry is not None
    assert test_file_entry["type"] == "file"

async def test_read_subscription_resource(server: FileMonitorMCPServer, subscription: str):
    """Test reading a subscription resource."""
    # Read the subscription resource
    result = await server._read_subscription_resource(subscription)
    
    # Parse the JSON response
    subscription_data = json.loads(result)
    
    # Verify the subscription data
    assert subscription_data["id"] == subscription
    assert "path" in subscription_data
    assert "recent_changes" in subscription_data