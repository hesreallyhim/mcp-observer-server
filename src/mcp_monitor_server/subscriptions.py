"""
Subscription management for the File Change Monitoring MCP server.
"""
import asyncio
import datetime
import uuid
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field


class FileChange(BaseModel):
    """Represents a file change event."""
    path: str
    event: str  # "created", "modified", "deleted"
    timestamp: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "path": self.path,
            "event": self.event,
            "timestamp": self.timestamp.isoformat()
        }


class Subscription(BaseModel):
    """Represents a file monitoring subscription."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    path: str
    recursive: bool = False
    patterns: List[str] = Field(default_factory=list)
    ignore_patterns: List[str] = Field(default_factory=list)
    events: List[str] = Field(default_factory=lambda: ["created", "modified", "deleted"])
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    changes: List[FileChange] = Field(default_factory=list)
    resource_subscribers: Set[str] = Field(default_factory=set)

    model_config = ConfigDict(validate_assignment=True)

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "path": self.path,
            "recursive": self.recursive,
            "patterns": self.patterns,
            "ignore_patterns": self.ignore_patterns,
            "events": self.events,
            "created_at": self.created_at.isoformat()
        }

    def add_change(self, path: str, event: str) -> Optional[FileChange]:
        """Add a change event to this subscription."""
        # Check if the event type is one we're monitoring
        if event not in self.events:
            return None
        
        change = FileChange(path=path, event=event)
        self.changes.append(change)
        # Limit changes list to recent 100 events to prevent memory bloat
        if len(self.changes) > 100:
            self.changes = self.changes[-100:]
        return change

    def get_changes_since(self, since: Optional[datetime.datetime] = None) -> List[FileChange]:
        """Get changes that occurred after the specified time."""
        if since is None:
            return self.changes
        
        return [change for change in self.changes if change.timestamp > since]


class SubscriptionManager:
    """Manages file monitoring subscriptions."""
    
    def __init__(self) -> None:
        self.subscriptions: Dict[str, Subscription] = {}
        self._lock = asyncio.Lock()
    
    async def create_subscription(
        self, 
        path: str, 
        recursive: bool = False,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        events: Optional[List[str]] = None
    ) -> Subscription:
        """Create a new subscription."""
        async with self._lock:
            subscription = Subscription(
                path=path,
                recursive=recursive,
                patterns=patterns or [],
                ignore_patterns=ignore_patterns or [],
                events=events or ["created", "modified", "deleted"]
            )
            self.subscriptions[subscription.id] = subscription
            return subscription
    
    async def get_subscription(self, subscription_id: str) -> Optional[Subscription]:
        """Get a subscription by ID."""
        async with self._lock:
            return self.subscriptions.get(subscription_id)
    
    async def delete_subscription(self, subscription_id: str) -> Tuple[bool, str]:
        """Delete a subscription."""
        async with self._lock:
            if subscription_id in self.subscriptions:
                del self.subscriptions[subscription_id]
                return True, "Subscription successfully deleted"
            return False, "Subscription not found"
    
    async def list_subscriptions(self) -> List[Subscription]:
        """List all active subscriptions."""
        async with self._lock:
            return list(self.subscriptions.values())
            
    async def add_change(self, subscription_id: str, path: str, event: str) -> Optional[FileChange]:
        """Add a change event to a subscription."""
        async with self._lock:
            subscription = self.subscriptions.get(subscription_id)
            if subscription:
                return subscription.add_change(path, event)
            return None
    
    async def add_resource_subscriber(self, subscription_id: str, client_id: str) -> bool:
        """Add a client as a subscriber to a subscription resource."""
        async with self._lock:
            subscription = self.subscriptions.get(subscription_id)
            if subscription:
                subscription.resource_subscribers.add(client_id)
                return True
            return False
    
    async def remove_resource_subscriber(self, subscription_id: str, client_id: str) -> bool:
        """Remove a client as a subscriber to a subscription resource."""
        async with self._lock:
            subscription = self.subscriptions.get(subscription_id)
            if subscription and client_id in subscription.resource_subscribers:
                subscription.resource_subscribers.remove(client_id)
                return True
            return False

    async def get_subscribers(self, subscription_id: str) -> Set[str]:
        """Get all subscribers to a subscription resource."""
        async with self._lock:
            subscription = self.subscriptions.get(subscription_id)
            if subscription:
                return subscription.resource_subscribers.copy()
            return set()
