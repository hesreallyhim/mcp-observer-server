"""
File system watcher for the File Change Monitoring MCP server.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from typing import (
    AsyncGenerator,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)

# Import for runtime
from watchdog.observers import Observer

# Import for type checking
from watchdog.observers.api import BaseObserver, ObservedWatch

# Define type aliases
# ObserverType: TypeAlias = Observer
# WatchType: TypeAlias = ObservedWatch
from .subscriptions import SubscriptionManager


class FileChangeHandler(FileSystemEventHandler):
    """Event handler for file system changes."""

    def __init__(
        self,
        subscription_id: str,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = False,
        callback: Optional[Callable[[str, str, str], None]] = None,
    ):
        """Initialize the handler.
        
        Args:
            subscription_id: ID of the subscription this handler is for
            patterns: List of glob patterns to include
            ignore_patterns: List of glob patterns to exclude
            ignore_directories: Whether to ignore directory events
            callback: Function to call when an event occurs
        """
        super().__init__()
        self.subscription_id = subscription_id
        self.patterns = patterns or ["*"]
        self.ignore_patterns = ignore_patterns or []
        self.ignore_directories = ignore_directories
        self.callback = callback

    def _should_process_event(self, event: FileSystemEvent) -> bool:
        """Determine if an event should be processed based on patterns."""
        # Skip directory events if configured to do so
        if self.ignore_directories and (
            isinstance(event, DirCreatedEvent) or
            isinstance(event, DirDeletedEvent) or
            isinstance(event, DirModifiedEvent) or
            isinstance(event, DirMovedEvent)
        ):
            return False
            
        # Check patterns
        from fnmatch import fnmatch
        
        # Get the path to check against patterns and ensure it's a string
        path = str(event.src_path)
        
        # Check if the path should be ignored
        for pattern in self.ignore_patterns:
            if fnmatch(path, str(pattern)):
                return False
                
        # Check if the path matches any include pattern
        for pattern in self.patterns:
            if fnmatch(path, str(pattern)):
                return True
                
        # If we have specific patterns and none matched, ignore the event
        return len(self.patterns) == 0 or "*" in self.patterns

    def dispatch(self, event: FileSystemEvent) -> None:
        """Dispatch events to the appropriate handlers."""
        if not self._should_process_event(event):
            return
            
        super().dispatch(event)

    def on_created(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is created."""
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "created")

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is deleted."""
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "deleted")

    def on_modified(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is modified."""
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "modified")
            
    def on_moved(self, event: FileSystemEvent) -> None:
        """Called when a file or directory is moved."""
        # Treat move as a delete+create operation
        if self.callback:
            self.callback(self.subscription_id, str(event.src_path), "deleted")
            self.callback(self.subscription_id, str(event.dest_path), "created")


class FileWatcher:
    """Manages file system watchers for subscriptions."""

    def __init__(self, subscription_manager: SubscriptionManager):
        """Initialize the file watcher.
        
        Args:
            subscription_manager: Manager for subscriptions
        """
        self.subscription_manager = subscription_manager
        self._observers: Dict[str, BaseObserver] = {}
        self._handlers: Dict[str, FileChangeHandler] = {}
        self._watches: Dict[str, ObservedWatch] = {}
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[Tuple[str, str, str]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def _handle_event(self, subscription_id: str, path: str, event_type: str) -> None:
        """Handle a file system event by putting it in the queue."""
        asyncio.run_coroutine_threadsafe(
            self._queue.put((subscription_id, path, event_type)),
            asyncio.get_event_loop()
        )

    async def _process_events(self) -> None:
        """Process events from the queue."""
        while not self._stopping:
            try:
                subscription_id, path, event_type = await self._queue.get()
                await self.subscription_manager.add_change(subscription_id, path, event_type)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error processing event: {e}")
                
    async def start(self) -> None:
        """Start the file watcher."""
        if self._task is not None:
            return
        
        self._stopping = False
        self._task = asyncio.create_task(self._process_events())

    async def stop(self) -> None:
        """Stop the file watcher."""
        self._stopping = True
        
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        # Stop all observers
        async with self._lock:
            for observer in self._observers.values():
                observer.stop()
            
            for observer in self._observers.values():
                observer.join()
            
            self._observers.clear()
            self._handlers.clear()
            self._watches.clear()

    @asynccontextmanager
    async def run(self) -> AsyncGenerator[None, None]:
        """Run the file watcher in a context manager."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def watch(
        self, 
        subscription_id: str, 
        path: str, 
        recursive: bool = False,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = False,
    ) -> bool:
        """Watch a path for changes.
        
        Args:
            subscription_id: ID of the subscription
            path: Path to watch
            recursive: Whether to watch subdirectories
            patterns: List of glob patterns to include
            ignore_patterns: List of glob patterns to exclude
            ignore_directories: Whether to ignore directory events
            
        Returns:
            bool: True if watching started successfully
        """
        if not os.path.exists(path):
            return False
        
        async with self._lock:
            # Stop existing observer for this subscription if any
            await self.unwatch(subscription_id)
            
            # Create event handler
            handler = FileChangeHandler(
                subscription_id=subscription_id,
                patterns=patterns,
                ignore_patterns=ignore_patterns,
                ignore_directories=ignore_directories,
                callback=self._handle_event,
            )
            
            # Create observer
            observer = Observer()
            watch = observer.schedule(handler, path, recursive=recursive)
            observer.start()
            
            # Store references
            self._observers[subscription_id] = observer
            self._handlers[subscription_id] = handler
            self._watches[subscription_id] = watch
            
            return True

    async def unwatch(self, subscription_id: str) -> bool:
        """Stop watching a subscription.
        
        Args:
            subscription_id: ID of the subscription
            
        Returns:
            bool: True if watching was stopped
        """
        async with self._lock:
            observer = self._observers.get(subscription_id)
            watch = self._watches.get(subscription_id)
            
            if observer and watch:
                observer.unschedule(watch)
                observer.stop()
                observer.join()
                
                del self._observers[subscription_id]
                del self._handlers[subscription_id]
                del self._watches[subscription_id]
                return True
            return False
