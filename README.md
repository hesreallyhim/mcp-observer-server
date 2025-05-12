# mcp-monitor-server

`mcp-monitor-server` is an MCP (Model Context Protocol) server that monitors system events, such as file changes, and sends notifications to an MCP client.

# File Change Monitoring MCP Server Specification

## Overview

This document describes the design and specifications for an MCP server that monitors file system changes and notifies subscribed clients. The server focuses purely on file monitoring and notifications, decoupled from specific client actions, making it versatile for various use cases.

## Design Philosophy

The server follows these key principles:

1. **Separation of Concerns**: The server focuses exclusively on file monitoring and notifications, not on actions to take when files change.
2. **Generic Notification System**: Provides a flexible subscription model that can be used by any MCP client.
3. **Scalable Architecture**: Supports monitoring individual files, directories, or entire repositories.
4. **Client-Driven Actions**: Clients decide what to do with change notifications based on their specific needs.

## Resources

The server exposes the following resources:

1. **File Resources**

   - URI Template: `file://{path}`
   - Returns the content of individual files
   - Supports subscription for change notifications

2. **Directory Resources**

   - URI Template: `dir://{path}`
   - Returns directory listings with file metadata
   - Supports recursive listings of subdirectories

3. **Subscription Resources**
   - URI Template: `subscription://{subscription_id}`
   - Returns information about active subscriptions and their recent changes
   - Updated automatically when monitored files change

## Tools

### 1. `subscribe`

Creates a new subscription to monitor file changes.

**Parameters:**

- `path` (string): Path to file or directory
- `recursive` (boolean, optional): Whether to include subdirectories (default: `false`)
- `patterns` (array of strings, optional): Glob patterns to include (e.g., `["*.py", "*.js"]`)
- `ignore_patterns` (array of strings, optional): Glob patterns to exclude
- `events` (array of strings, optional): Events to monitor (`["created", "modified", "deleted"]`, default: all)

**Returns:**

- `subscription_id` (string): Unique identifier for the subscription
- `status` (string): Status of the subscription ("active")

### 2. `unsubscribe`

Cancels an active subscription.

**Parameters:**

- `subscription_id` (string): ID of the subscription to cancel

**Returns:**

- `success` (boolean): Whether the unsubscription was successful
- `message` (string): Status message

### 3. `list_subscriptions`

Lists all active subscriptions.

**Parameters:**

- None

**Returns:**

- `subscriptions` (array): List of active subscriptions with their details:
  - `id` (string): Subscription ID
  - `path` (string): Monitored path
  - `recursive` (boolean): Whether monitoring includes subdirectories
  - `patterns` (array): Included glob patterns
  - `ignore_patterns` (array): Excluded glob patterns
  - `events` (array): Monitored events
  - `created_at` (string): Timestamp when subscription was created

### 4. `get_changes`

Retrieves recent changes for a specific subscription.

**Parameters:**

- `subscription_id` (string): ID of the subscription
- `since` (string, optional): ISO timestamp to get changes after a specific time

**Returns:**

- `changes` (array): List of file changes with:
  - `path` (string): Path to the changed file
  - `event` (string): Type of change event
  - `timestamp` (string): When the change occurred

## Notification System

The server uses the MCP notification mechanism to inform clients of file changes:

1. **Subscription Protocol**:

   - Clients subscribe to changes using the `subscribe` tool
   - Server maintains a registry of active subscriptions

2. **Change Detection**:

   - Server monitors the file system for changes matching subscription criteria
   - Changes are recorded with path, event type, and timestamp

3. **Change Notification**:
   - Server sends `notifications/resources/updated` messages to subscribed clients
   - Notifications include the subscription URI and details of changes

## Client Usage Pattern

Clients interact with the server through this typical workflow:

1. Create a subscription for files or directories of interest
2. Receive notifications when changes occur
3. Retrieve file contents or change details as needed
4. Perform client-specific actions based on the changes
5. Unsubscribe when monitoring is no longer needed

## Use Cases

This generic file monitoring server supports various use cases, including:

1. **Documentation Updates**: A client like Claude Code can monitor source files and update documentation when code changes.
2. **Testing**: Clients can trigger test runs when source files are modified.
3. **Build Systems**: Automatic rebuilding of artifacts when dependencies change.
4. **Live Preview**: Updating previews of documents, websites, or applications during development.
5. **Synchronization**: Keeping multiple systems in sync when files change.

## Implementation Notes

The server implementation will use:

1. Python's MCP SDK for protocol handling
2. Watchdog library for efficient file system monitoring
3. Asyncio for non-blocking operations and notification delivery
4. In-memory storage for subscription management

## Local Development

### Setup

This project uses [uv](https://github.com/astral-sh/uv) for dependency management and virtual environment handling.

```bash
# Install uv if you don't have it already
curl -sSf https://astral.sh/uv/install.sh | bash

# Create a virtual environment and install dependencies
uv venv
uv pip install -e .

# When adding new dependencies
uv pip install <package>
```

### Running the Server

```bash
# Activate the virtual environment (if not using uv's auto-activation)
source .venv/bin/activate  # On Unix/macOS
# or
.venv\Scripts\activate     # On Windows

# Run the server
python main.py
```
