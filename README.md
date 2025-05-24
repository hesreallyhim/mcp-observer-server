# mcp-monitor-server

`mcp-monitor-server` is an MCP (Model Context Protocol) server that monitors file system events and provides real-time notifications to MCP clients. It acts as a bridge between your local file system and AI assistants like Claude, enabling them to respond to file changes automatically.

## Server Description

The MCP Monitor Server tracks file and directory changes on your system, allowing MCP clients to subscribe to these events and take action when files are created, modified, deleted, or moved. This server implements the full Model Context Protocol specification, providing:

- **Real-time file monitoring**: Using the Watchdog library for efficient file system observation
- **Subscription management**: Create, list, and cancel monitoring subscriptions for any path
- **Change history**: Maintains a log of recent changes for each subscription
- **File and directory access**: Read file contents and directory listings through MCP resources
- **Stateless design**: Clients control what happens in response to file changes

### Key Features

- Subscribe to changes in specific files, directories, or entire repositories
- Filter events by file patterns or event types
- Query recent changes to see what files were affected
- Access file contents via resource endpoints
- Lightweight and efficient implementation with minimal dependencies
- Simple integration with any MCP-compatible client

### Practical Applications

- **Automated documentation updates**: Keep documentation in sync with code changes
- **Live reloading**: Trigger rebuilds or restarts when source files change
- **Collaborative editing**: Track changes made by other editors or team members
- **Content synchronization**: Mirror file system changes to remote systems
- **Testing automation**: Run tests when relevant files are modified
- **AI assistance**: Enable AI tools to respond to file changes automatically

## Current Implementation Design

The server implementation features a streamlined architecture that prioritizes simplicity, reliability, and maintainability.

### Architecture Highlights

1. **Simplified Structure**

   - Focused implementation (~170 lines of code)
   - Consolidated functionality into a small set of core components
   - Clean function-based design that leverages the MCP SDK directly
   - High readability and maintainability

2. **Efficient State Management**

   - Simple dictionary structure maps paths to client sessions
   - Uses a `watched` dictionary for direct path-to-session mapping
   - Minimal state tracking with clear data flow
   - Avoids redundant data structures

3. **MCP Protocol Integration**

   - Direct use of MCP SDK function decorators
   - Clean resource URI handling
   - Simplified server initialization with proper capability configuration
   - Direct notification delivery system

4. **Event Processing**

   - Streamlined Watchdog event handler implementation
   - Direct event-to-notification path
   - Thread-safe communication via `call_soon_threadsafe`
   - Efficient event filtering

5. **Notification System**
   - Direct use of MCP notification primitives
   - Reliable delivery with proper error handling
   - Accurate UTC timestamp handling
   - Clean URI formatting

### Core Components

1. **Data Structure**

   - Single global dictionary `watched` maps Path objects to sets of ServerSession objects
   - Each path entry contains the set of sessions subscribed to that path

2. **Tool API**

   - Two essential tools: `subscribe` and `unsubscribe`
   - Simple path parameter for straightforward subscription management
   - Clean error handling and path validation

3. **Resource Handling**

   - File URIs directly exposed through resource listing
   - Path resolution and validation
   - Text content reading for files

4. **Event Processing**

   - Watcher class extends FileSystemEventHandler
   - Processes modified events directly
   - Thread-safe notification dispatching
   - Path relativity handling for nested paths

5. **Notification Delivery**
   - ServerNotification creation and sending
   - Event metadata with timestamps
   - Clean URI formatting

The implementation achieves a good balance between functionality and simplicity, resulting in a reliable and maintainable codebase.

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

> **Note:** When creating a subscription, the server automatically applies default ignore patterns (node_modules, venv, .git, etc.) and reads custom patterns from `.mcpignore` files if they exist in the monitored directory.

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

### 5. `create_mcpignore`

Creates a `.mcpignore` file in the specified directory with predefined common patterns to ignore.

**Parameters:**

- `path` (string): Directory where to create the `.mcpignore` file
- `include_defaults` (boolean, optional): Whether to include the default ignore patterns (default: `true`)

**Returns:**

- `success` (boolean): Whether the file was created successfully
- `message` (string): Status message
- `path` (string): Path to the created file

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

## Ignoring Files with .mcpignore

The server includes a flexible system for excluding files from monitoring, similar to `.gitignore`:

### Default Ignore Patterns

These patterns are automatically applied to all subscriptions:

- **Virtual environments**: `**/venv/**`, `**/.venv/**`, `**/env/**`, etc.
- **Package directories**: `**/node_modules/**`, `**/.npm/**`, etc.
- **Cache directories**: `**/__pycache__/**`, `**/.pytest_cache/**`, etc.
- **Build directories**: `**/build/**`, `**/dist/**`, `**/target/**`, etc.
- **IDE files**: `**/.idea/**`, `**/.vscode/**`, etc.
- **Version control**: `**/.git/**`, `**/.github/**`, etc.
- **System files**: `**/.DS_Store`, `**/Thumbs.db`, etc.
- **Temporary files**: `**/*.log`, `**/*.tmp`, etc.

### Custom .mcpignore Files

You can create custom `.mcpignore` files in any directory you monitor:

1. Create the file manually or use the `create_mcpignore` tool
2. Add one glob pattern per line (e.g., `**/*.bak`)
3. Use `#` for comments

Example `.mcpignore` file:

```
# Ignore build artifacts
**/myapp/build/**
**/*.min.js

# Ignore large data files
**/*.csv
**/data/*.json
```

### Pattern Priority

When determining which files to ignore, the server applies patterns in this order:

1. User-provided patterns in the `subscribe` tool call (highest priority)
2. Patterns from `.mcpignore` files (medium priority)
3. Default built-in patterns (lowest priority)

This allows fine-grained control of which files trigger notifications.

## Implementation Notes

The implementation will use:

1. Python's MCP SDK for protocol handling
2. Watchdog library for efficient file system monitoring
3. Asyncio for non-blocking operations and notification delivery
4. In-memory storage for subscription management

## System Design

The server implementation follows a carefully designed architecture that separates protocol handling from business logic:

### Lifecycle Management

The lifecycle management isn't passed directly to the Server when instantiating it because the MCP Server architecture follows a different pattern:

1. **Separation of Concerns**: The Server instance (from the MCP library) is focused on protocol handling, while our FileMonitorMCPServer wrapper handles the business logic and resource lifecycle. This separation keeps the MCP protocol handling decoupled from our specific file monitoring functionality.

2. **Different Responsibility Layers**: The MCP Server (self.server) primarily handles protocol-level communication, while our own lifespan context manager manages higher-level application components like observers, notification processors, and resource cleanup.

3. **Composition vs. Inheritance**: The code uses composition (having a Server as a member) rather than inheritance (extending the Server class). This allows us to add our own lifecycle management without modifying the base Server class.

4. **Event Loop Management**: Our lifespan context manager coordinates with asyncio's event loop for proper task management, while the Server itself is more focused on handling requests and responses.

5. **Control Flow Design**: The lifespan is used at the runtime level in `run()`, where it wraps the entire execution to ensure proper initialization before accepting connections and proper cleanup after. The Server instance is used within this context, not the other way around.

Instead of passing the lifespan to the Server directly, the code:

1. Creates a Server instance as a component of FileMonitorMCPServer
2. Implements a lifespan context manager in FileMonitorMCPServer
3. Uses the lifespan at the application level to wrap the Server's run method

This approach allows us to maintain proper lifecycle for all our components while keeping the MCP protocol handling focused on its specific responsibilities.

## Installation and Usage

### Setup

This project uses [uv](https://github.com/astral-sh/uv) for dependency management and virtual environment handling.

```bash
# Clone the repository
git clone https://github.com/yourusername/mcp-monitor-server.git
cd mcp-monitor-server

# Install uv if you don't have it already
curl -sSf https://astral.sh/uv/install.sh | bash

# Create a virtual environment and install dependencies
uv venv
uv pip install -e .
```

### Running the Server

```bash
# Option 1: Run directly with Python
python main.py

# Option 2: Run with command-line options
python main.py --monitor-path /path/to/monitor --verbose

# Option 3: Run in development mode with MCP CLI
mcp dev src/mcp_monitor_server/server.py

# Option 4: Install in Claude Desktop
mcp install src/mcp_monitor_server/server.py --name "File Monitor"
```

### Command Line Options

- `--monitor-path`, `-m`: Path to monitor for changes (default: current directory)
- `--verbose`, `-v`: Increase logging verbosity (can be used multiple times)

### Using with MCP Clients

Here's an example of how a client would interact with the server:

1. Subscribe to a directory:

   ```
   subscription_id = call_tool("subscribe", {
       "path": "/path/to/project",
       "recursive": True,
       "patterns": ["*.py", "*.js"]
   })
   ```

2. Create an .mcpignore file:

   ```
   call_tool("create_mcpignore", {
       "path": "/path/to/project",
       "include_defaults": true
   })
   ```

3. Get recent changes:

   ```
   changes = call_tool("get_changes", {
       "subscription_id": subscription_id
   })
   ```

4. Read file contents:

   ```
   file_content = read_resource(f"file:///path/to/project/file.py")
   ```

5. Clean up when done:
   ```
   call_tool("unsubscribe", {
       "subscription_id": subscription_id
   })
   ```
