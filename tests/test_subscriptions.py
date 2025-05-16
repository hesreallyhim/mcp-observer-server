"""Tests for subscription functionality."""

import json
from pathlib import Path

import pytest

from mcp_monitor_server.server import (
    FileMonitorMCPServer,
    SubscribeInput,
    UnsubscribeInput,
)

pytestmark = pytest.mark.asyncio


async def test_create_subscription(server: FileMonitorMCPServer, temp_dir: Path):
    """Test creating a subscription."""
    input_data = SubscribeInput(
        path=str(temp_dir),
        recursive=True,
        patterns=["*"],
        ignore_patterns=[],
        events=["created", "modified", "deleted"],
    )

    result = await server._handle_subscribe_tool(input_data)
    assert result[0].type == "text"

    response = json.loads(result[0].text)
    assert "subscription_id" in response
    assert response["status"] == "active"
    assert response["path"] == str(temp_dir)
    assert response["recursive"] is True

    # Verify subscription was stored
    subscription_id = response["subscription_id"]
    async with server.subscription_lock:
        assert subscription_id in server.subscriptions
        assert server.subscriptions[subscription_id].path == str(temp_dir)


async def test_list_subscriptions(
    server: FileMonitorMCPServer, subscription: str, temp_dir: Path
):
    """Test listing subscriptions."""
    result = await server._handle_list_subscriptions_tool()
    assert result[0].type == "text"

    response = json.loads(result[0].text)
    assert "subscriptions" in response

    # Verify our subscription is in the list
    subscription_data = next(
        (sub for sub in response["subscriptions"] if sub["id"] == subscription), None
    )
    assert subscription_data is not None
    assert subscription_data["path"] == str(temp_dir)


async def test_unsubscribe(server: FileMonitorMCPServer, subscription: str):
    """Test unsubscribing from a subscription."""
    input_data = UnsubscribeInput(subscription_id=subscription)
    result = await server._handle_unsubscribe_tool(input_data)
    assert result[0].type == "text"

    response = json.loads(result[0].text)
    assert response["success"] is True

    # Verify subscription was removed
    async with server.subscription_lock:
        assert subscription not in server.subscriptions


async def test_unsubscribe_nonexistent(server: FileMonitorMCPServer):
    """Test unsubscribing from a non-existent subscription."""
    input_data = UnsubscribeInput(subscription_id="nonexistent-id")
    result = await server._handle_unsubscribe_tool(input_data)
    assert result[0].type == "text"

    response = json.loads(result[0].text)
    assert response["success"] is False
