"""
Resource handling for the File Change Monitoring MCP server.
"""
import json
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Tuple

import anyio

from .subscriptions import SubscriptionManager


class ResourceManager:
    """
    Manages resources for the File Change Monitoring MCP server.
    
    Handles three types of resources:
    1. File resources (file://{path})
    2. Directory resources (dir://{path})
    3. Subscription resources (subscription://{subscription_id})
    """
    
    def __init__(self, subscription_manager: SubscriptionManager):
        """Initialize the resource manager."""
        self.subscription_manager = subscription_manager
    
    async def list_resources(self) -> List[Dict[str, str]]:
        """
        List all available resources.
        
        For subscriptions, this will include all active subscriptions.
        Returns:
            List of resource metadata dicts with uri, name, and mimeType
        """
        resources = []
        
        # Add subscription resources
        subscriptions = await self.subscription_manager.list_subscriptions()
        for subscription in subscriptions:
            resources.append({
                "uri": f"subscription://{subscription.id}",
                "name": f"Subscription: {subscription.path}",
                "mimeType": "application/json",
                "description": f"File change notifications for {subscription.path}"
            })
            
        return resources
    
    async def list_resource_templates(self) -> List[Dict[str, str]]:
        """
        List all resource templates.
        
        This includes templates for file, directory, and subscription resources.
        Returns:
            List of resource template metadata dicts with uriTemplate and description
        """
        return [
            {
                "uriTemplate": "file://{path}",
                "name": "File Content",
                "description": "Content of a file"
            },
            {
                "uriTemplate": "dir://{path}",
                "name": "Directory Listing",
                "description": "List of files in a directory",
                "mimeType": "application/json"
            },
            {
                "uriTemplate": "subscription://{subscription_id}",
                "name": "Subscription Status",
                "description": "Status and recent changes for a subscription",
                "mimeType": "application/json"
            }
        ]
    
    async def read_resource(self, uri: str) -> Tuple[str, str]:
        """
        Read a resource's content.
        
        Args:
            uri: Resource URI (file://, dir://, or subscription://)
            
        Returns:
            Tuple of (content, mimeType)
            
        Raises:
            ValueError: If the resource URI is invalid or resource not found
        """
        # Parse the URI scheme and path
        if not uri or "://" not in uri:
            raise ValueError(f"Invalid resource URI: {uri}")
        
        scheme, path = uri.split("://", 1)
        
        # Handle file resources
        if scheme == "file":
            return await self._read_file_resource(path)
            
        # Handle directory resources
        elif scheme == "dir":
            return await self._read_directory_resource(path)
            
        # Handle subscription resources
        elif scheme == "subscription":
            return await self._read_subscription_resource(path)
            
        else:
            raise ValueError(f"Unsupported resource scheme: {scheme}")
    
    async def _read_file_resource(self, path: str) -> Tuple[str, str]:
        """Read a file resource."""
        if not os.path.isfile(path):
            raise ValueError(f"File not found: {path}")
            
        try:
            async with await anyio.open_file(path, "rb") as file:
                content = await file.read()
                
            # Try to determine MIME type
            mime_type = self._guess_mime_type(path)
            
            # For text files, decode to string
            if mime_type.startswith("text/") or mime_type in (
                "application/json", 
                "application/xml",
                "application/javascript",
                "application/yaml"
            ):
                return content.decode("utf-8"), mime_type
            
            # For binary files, use base64 encoding (not implemented in this example)
            # In a real implementation, you would return:
            # return base64.b64encode(content).decode("ascii"), mime_type
            
            # For simplicity in this example, we'll just return binary data as UTF-8
            # This wouldn't work for actual binary files, but simplifies the example
            return str(content), mime_type
            
        except Exception as e:
            raise ValueError(f"Error reading file {path}: {str(e)}")
    
    async def _read_directory_resource(self, path: str) -> Tuple[str, str]:
        """Read a directory resource."""
        if not os.path.isdir(path):
            raise ValueError(f"Directory not found: {path}")
            
        try:
            # Get directory contents
            entries = []
            for entry in os.scandir(path):
                entry_type = "file" if entry.is_file() else "directory"
                entries.append({
                    "name": entry.name,
                    "path": entry.path,
                    "type": entry_type,
                    "size": entry.stat().st_size if entry.is_file() else None,
                    "modified": entry.stat().st_mtime
                })
                
            # Sort by name
            entries.sort(key=lambda e: e["name"] or "")
            
            return json.dumps({
                "path": path,
                "entries": entries
            }, indent=2), "application/json"
            
        except Exception as e:
            raise ValueError(f"Error reading directory {path}: {str(e)}")
    
    async def _read_subscription_resource(self, subscription_id: str) -> Tuple[str, str]:
        """Read a subscription resource."""
        subscription = await self.subscription_manager.get_subscription(subscription_id)
        if not subscription:
            raise ValueError(f"Subscription not found: {subscription_id}")
            
        # Convert to dict for JSON serialization
        subscription_data = subscription.to_dict()
        
        # Add recent changes
        subscription_data["recent_changes"] = [
            change.to_dict() for change in subscription.changes[-20:]  # Show last 20 changes
        ]
        
        return json.dumps(subscription_data, indent=2), "application/json"
    
    def _guess_mime_type(self, path: str) -> str:
        """Guess the MIME type of a file based on its extension."""
        import mimetypes
        mime_type, _ = mimetypes.guess_type(path)
        return mime_type or "application/octet-stream"
        
    @asynccontextmanager
    async def subscribe_resource(self, uri: str, client_id: str) -> AsyncGenerator[None, None]:
        """
        Subscribe to a resource.
        
        Args:
            uri: Resource URI
            client_id: Unique identifier for the client
            
        Yields:
            None
            
        Raises:
            ValueError: If the resource URI is invalid or resource not found
        """
        # Only subscription resources support subscription
        if not uri.startswith("subscription://"):
            raise ValueError(f"Resource does not support subscription: {uri}")
            
        subscription_id = uri.split("://", 1)[1]
        
        # Add client as subscriber
        success = await self.subscription_manager.add_resource_subscriber(
            subscription_id, client_id
        )
        
        if not success:
            raise ValueError(f"Subscription not found: {subscription_id}")
            
        try:
            yield
        finally:
            # Remove client as subscriber when done
            await self.subscription_manager.remove_resource_subscriber(
                subscription_id, client_id
            )
