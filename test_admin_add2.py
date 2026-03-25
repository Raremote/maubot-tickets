#!/usr/bin/env python3
"""Test admin_add command with proper mocks."""
import sys
import asyncio
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, '.')

async def test_admin_add():
    # Mock the database
    mock_db = Mock()
    mock_db.get_all_categories.return_value = [
        {"category_id": "tech", "name": "Technical", "enabled": True, "description": "Tech issues"},
        {"category_id": "billing", "name": "Billing", "enabled": True, "description": None},
    ]
    mock_db.get_intake_room.return_value = None
    
    # Mock plugin
    mock_plugin = Mock()
    mock_plugin.client = Mock()
    mock_plugin.log = Mock()
    
    # Import after mocking
    from tickets.listener import TicketsHandler
    
    # Create handler
    handler = TicketsHandler(mock_db, mock_plugin)
    
    # Mock client.mxid
    handler.client.mxid = "@bot:example.com"
    
    # Mock async methods
    handler._is_direct_message = AsyncMock(return_value=False)
    handler._get_ticket_for_room = AsyncMock(return_value=None)
    handler.ensure_admin = AsyncMock(return_value=True)
    handler._set_pending_ticket = Mock()
    
    # Mock evt.reply
    mock_reply = AsyncMock()
    mock_evt = Mock()
    mock_evt.content.body = "!ticket admin add"
    mock_evt.room_id = "!room:example.com"
    mock_evt.sender = "@user:example.com"
    mock_evt.reply = mock_reply
    
    # Call admin_add
    await handler.admin_add(mock_evt)
    
    # Verify
    print("✓ admin_add executed successfully")
    print(f"   _is_direct_message called: {handler._is_direct_message.called}")
    print(f"   _get_ticket_for_room called: {handler._get_ticket_for_room.called}")
    print(f"   ensure_admin called: {handler.ensure_admin.called}")
    print(f"   reply called: {mock_reply.called}")
    if mock_reply.called:
        args, kwargs = mock_reply.call_args
        reply_text = args[0]
        print(f"   reply length: {len(reply_text)} chars")
        if "Select a category" in reply_text:
            print("   ✓ Contains category selection prompt")
        else:
            print("   ✗ Missing category prompt")
            print(f"   First 200 chars: {reply_text[:200]}")
    
    print(f"   _set_pending_ticket called: {handler._set_pending_ticket.called}")
    if handler._set_pending_ticket.called:
        args, kwargs = handler._set_pending_ticket.call_args
        pending_data = args[1]
        print(f"   pending type: {pending_data.get('type')}")
        print(f"   pending room_id: {pending_data.get('room_id')}")

if __name__ == "__main__":
    asyncio.run(test_admin_add())