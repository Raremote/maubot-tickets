#!/usr/bin/env python3
"""Test admin_add command."""
import sys
import asyncio
from unittest.mock import AsyncMock, Mock, MagicMock

sys.path.insert(0, '.')

# Mock the maubot imports before importing TicketsHandler
sys.modules['maubot'] = Mock()
sys.modules['maubot.plugin_base'] = Mock()
sys.modules['maubot.handlers'] = Mock()
sys.modules['maubot.matrix'] = Mock()
sys.modules['mautrix.errors'] = Mock()
sys.modules['mautrix.types'] = Mock()

# Now import
from tickets.listener import TicketsHandler

async def test_admin_add():
    # Create mock database
    mock_db = Mock()
    mock_db.get_all_categories.return_value = [
        {"category_id": "tech", "name": "Technical", "enabled": True, "description": "Tech issues"},
        {"category_id": "billing", "name": "Billing", "enabled": True, "description": None},
    ]
    mock_db.get_intake_room.return_value = None
    
    # Create mock plugin
    mock_plugin = Mock()
    mock_plugin.client = Mock()
    mock_plugin.log = Mock()
    
    # Create handler
    handler = TicketsHandler(mock_db, mock_plugin)
    
    # Mock client.mxid
    handler.client.mxid = "@bot:example.com"
    
    # Mock _is_direct_message to return False (regular room)
    async def mock_is_dm(evt):
        return False
    handler._is_direct_message = mock_is_dm
    
    # Mock _get_ticket_for_room to return None (not a ticket room)
    async def mock_get_ticket(room_id):
        return None
    handler._get_ticket_for_room = mock_get_ticket
    
    # Mock ensure_admin to return True (admin user)
    async def mock_ensure_admin(evt):
        return True
    handler.ensure_admin = mock_ensure_admin
    
    # Mock _set_pending_ticket
    handler._set_pending_ticket = Mock()
    
    # Mock evt.reply
    mock_reply = AsyncMock()
    
    # Create mock event
    mock_evt = Mock()
    mock_evt.content.body = "!ticket intake add"
    mock_evt.room_id = "!room:example.com"
    mock_evt.sender = "@user:example.com"
    mock_evt.reply = mock_reply
    
    # Call admin_add
    await handler.admin_add(mock_evt)
    
    # Verify
    print("✓ admin_add called without exception")
    print(f"   reply called: {mock_reply.called}")
    if mock_reply.called:
        args, kwargs = mock_reply.call_args
        print(f"   reply content snippet: {args[0][:100]}...")
    
    # Check pending data stored
    print(f"   _set_pending_ticket called: {handler._set_pending_ticket.called}")
    if handler._set_pending_ticket.called:
        args, kwargs = handler._set_pending_ticket.call_args
        print(f"   pending data: {args[1]}")

if __name__ == "__main__":
    asyncio.run(test_admin_add())