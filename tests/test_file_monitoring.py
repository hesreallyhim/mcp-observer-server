"""Tests for file monitoring functionality."""
import json
import pytest
import asyncio
import os
from pathlib import Path
import time

from mcp_monitor_server.server import (
    FileMonitorMCPServer,
    GetChangesInput
)
from tests.conftest import wait_for_file_operation

pytestmark = pytest.mark.asyncio

async def test_detect_file_creation(server: FileMonitorMCPServer, subscription: str, temp_dir: Path):
    """Test detecting file creation events."""
    # Create a new file
    new_file = temp_dir / "new_file.txt"
    with open(new_file, "w") as f:
        f.write("Test content")
    
    # Wait for the file system event to be processed
    await asyncio.sleep(0.5)
    
    # Check for changes
    input_data = GetChangesInput(subscription_id=subscription)
    result = await server._handle_get_changes_tool(input_data)
    assert result[0].type == "text"
    
    response = json.loads(result[0].text)
    assert "changes" in response
    
    # Find the creation event for our file
    creation_event = next(
        (change for change in response["changes"] 
         if change["path"] == str(new_file) and change["event"] == "created"),
        None
    )
    assert creation_event is not None

async def test_detect_file_modification(server: FileMonitorMCPServer, subscription: str, test_file: Path):
    """Test detecting file modification events."""
    # Give a moment for the subscription to be effective
    await asyncio.sleep(0.1)
    
    # Modify the test file
    with open(test_file, "a") as f:
        f.write("\nAdditional content")
    
    # Wait for the file system event to be processed
    await asyncio.sleep(0.5)
    
    # Check for changes
    input_data = GetChangesInput(subscription_id=subscription)
    result = await server._handle_get_changes_tool(input_data)
    assert result[0].type == "text"
    
    response = json.loads(result[0].text)
    assert "changes" in response
    
    # Find the modification event for our file
    modification_event = next(
        (change for change in response["changes"] 
         if change["path"] == str(test_file) and change["event"] == "modified"),
        None
    )
    assert modification_event is not None

async def test_detect_file_deletion(server: FileMonitorMCPServer, subscription: str, temp_dir: Path):
    """Test detecting file deletion events."""
    # Create and then delete a file
    delete_file = temp_dir / "to_delete.txt"
    with open(delete_file, "w") as f:
        f.write("This file will be deleted")
    
    # Wait for the creation event to be processed
    await asyncio.sleep(0.1)
    
    # Delete the file
    os.unlink(delete_file)
    
    # Wait for the deletion event to be processed
    await asyncio.sleep(0.5)
    
    # Check for changes
    input_data = GetChangesInput(subscription_id=subscription)
    result = await server._handle_get_changes_tool(input_data)
    assert result[0].type == "text"
    
    response = json.loads(result[0].text)
    assert "changes" in response
    
    # Find the deletion event for our file
    deletion_event = next(
        (change for change in response["changes"] 
         if change["path"] == str(delete_file) and change["event"] == "deleted"),
        None
    )
    assert deletion_event is not None

async def test_recursive_monitoring(server: FileMonitorMCPServer, subscription: str, temp_dir: Path):
    """Test that recursive monitoring works for subdirectories."""
    # Create a subdirectory
    subdir = temp_dir / "subdir"
    subdir.mkdir()
    
    # Create a file in the subdirectory
    subdir_file = subdir / "subdir_file.txt"
    with open(subdir_file, "w") as f:
        f.write("File in subdirectory")
    
    # Wait for the events to be processed
    await asyncio.sleep(0.5)
    
    # Check for changes
    input_data = GetChangesInput(subscription_id=subscription)
    result = await server._handle_get_changes_tool(input_data)
    assert result[0].type == "text"
    
    response = json.loads(result[0].text)
    assert "changes" in response
    
    # Find the creation event for the subdirectory file
    subdirectory_event = next(
        (change for change in response["changes"] 
         if change["path"] == str(subdir_file) and change["event"] == "created"),
        None
    )
    assert subdirectory_event is not None