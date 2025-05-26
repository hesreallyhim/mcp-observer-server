# mcp-observer-server

`mcp-observer-server` is an MCP (Model Context Protocol) server that monitors file system events and provides real-time notifications to MCP clients. It acts as a bridge between your local file system and AI assistants like ~~Claude~~ Inspector, enabling them to respond to file changes automatically.

> **NOTE:** This is a demo/POC of a file monitoring MCP server that I'm working on. I'm seeing a lot of questions/comments/Issues/Discussions about this kind of thing, so I wanted to post this minimal implementation to share my approach.

## Context

The MCP protocol defines the notion of a resource subscription, wherein a client can request to be notified of any changes to a resource, and the server can choose to send notifications. Here is the flow diagram:

![Resource Subscription Flow Diagram](./assets/MCP%20Resource%20Subscription%20Flow%20Diagram.png)

The protocol says the client should then send a read request back to the server to read the changes. (All of this is optional, by the way). But, I find this a bit cumbersome, and involves an extra trip, and I'd rather have my resource-update notification describe the change as well. Fortunately, the SDK offers a `meta`/`_meta` field and you can pretty much send whatever you want. So I might want to send the number of lines changed, a diff of the changes, who knows what. I haven't implemented that in this demo, right now I'm just sending the timestamp. (I basically ripped everything out from the server except the minimum POC.)

> **NOTE!!!** I haven't tested this with any "real" MCP clients yet - my understanding is that very view clients actually support resource subscriptions, since it's optional anyway. However, fortunately **Inspector** is a very good client, and you can use that to test this server.

**DEMO INSTRUCTIONS:**

1. Clone the repository.
2. Install the dependencies using `uv` (or, some other way I suppose).
3. Run the server using `make start` (uses `uv`) or run `npx @modelcontextprotocol/inspector uv run src/mcp_observer_server/server.py`.
4. Open the Inspector client and connect using stdio, no configuration needed.
5. Use the `subscribe` tool to monitor a directory or file, (alternatively, you can run "List Resources", click a resource, and then click "Subscribe" button to subscribe to it).
6. By default, the server will expose a file called `watched.txt` in `src/mcp_observer_server/watched.txt` (the file is .gitignored, so you have to create it), but you can subscribe to other files as well. You can subscribe this file with the `subscribe_default` tool.
7. Modify the `watched.txt` file (or whatever file you subscribed to), and you should see a server notification appear in the bottom-right panel of the Inspector. This is the POC established.

## DEMO VISUALIZATION

1.  Start the server and connect with Inspector:
    ![Start Server and Connect](./assets/01-start-server-and-connect.png)
2.  List the default resources:
    ![List Resources](./assets/02%20-%20list-default-resources.png
3.  List the tools:
    ![List Tools](./assets/03%20-%20list-tools.png)
4.  Subscribe to the default file:
    ![Subscribe to Default File](./assets/04%20-%20invoke-subscribe-default.png
5.  Modify the file:
    ![Modify the File](./assets/05%20-%20editing-watched-file.png)
6.  See the notification appear:
    ![See Notification](./assets/06%20-%20server-notification-received.png)

🎉

## Server Description

The MCP Monitor Server tracks file and directory changes on your system, allowing MCP clients to subscribe to these events and take action when files are created, modified, deleted, or moved (current demo handles modification event). This server implements the full Model Context Protocol specification, providing:

- **Real-time file monitoring**: Using the Watchdog library for efficient file system observation
- **Subscription management**: Create, list, and cancel monitoring subscriptions for any path
- **Change history**: Maintains a log of recent changes for each subscription (omitted in demo)
- **File and directory access**: Read file contents and directory listings through MCP resources
- **Stateless design**: Clients control what happens in response to file changes

### Key Features

- Subscribe to changes in specific files, directories, or entire repositories
- Filter events by file patterns or event types (omitted in demo)
- Query recent changes to see what files were affected (omitted in demo)
- Access file contents via resource endpoints
- Lightweight and efficient implementation with minimal dependencies
- Simple integration with any MCP-compatible client (...that support resource subscriptions)

### Practical Applications

The main pain point I am trying to solve is that unless Claude Code, e.g., touches a file and writes the change to it itself, it has no idea what is going on in your repo/project. (You know those notifications - "File changeed since last read"?) Having a client or coding assistant that is actually monitoring what you're doing in your project and you don't have to delegate every task to Claude just so that it knows that it happens, seems tremendously useful to me. Some practical applications include:

- **Automated documentation updates**: Keep documentation in sync with code changes - you update some code, Claude is notified of the change, and it pro-actively checks or updates the doc-strings, etc.
- **Live code reviews**: Get real-time feedback on code changes as you work, catching spelling errors, type errors, etc., giving advice, true pair programming.
- **Testing automation**: Run tests when relevant files are modified.
- **AI assistance**: Enable AI tools to respond to file changes automatically.
- **Git commit automation**: Do you forget to commit frequently enough? Claude can watch your changes and suggest (or perform) commit actions more frequently.

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
