from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .base import TicketsHandlerBase


class TicketsHandlerNotifications(TicketsHandlerBase):
    """Notification methods for ticket system."""
    async def _notify_ntn_room(self, ticket: Dict[str, Any], message: str, mention_staff: bool = False, html_message: Optional[str] = None) -> bool:
        """Send a notification to the ticket's New Ticket Notification room.

        Args:
            ticket: Ticket dictionary
            message: Plain text message
            mention_staff: Whether to mention staff users
            html_message: Optional HTML formatted message. If provided, will be used as formatted_body.
        """
        intake_room_id = ticket.get("intake_room_id")
        if not intake_room_id:
            self.log.warning(f"Ticket {ticket['ticket_number']} has no New Ticket Notification room ID")
            return False
        self.log.debug(f"Notifying New Ticket Notification room {intake_room_id} for ticket {ticket['ticket_number']}: {message[:100]}...")

        # Check if New Ticket Notification room still exists and is enabled
        intake_room = self.db.get_intake_room(intake_room_id)
        if not intake_room or not intake_room.get("enabled", True):
            self.log.warning(f"New Ticket Notification room {intake_room_id} not found or disabled")
            return False

        try:
            if mention_staff:
                # Get staff users to mention
                staff_users = await self._get_staff_users(intake_room_id)
                if staff_users:
                    # Create plain text mentions
                    mention_text = " ".join(staff_users)
                    full_message = f"{message}\n\n**Staff notified:** {mention_text}"

                    # Create HTML mentions
                    mention_html = " ".join(
                        f'<a href="https://matrix.to/#/{user}">{html.escape(user)}</a>'
                        for user in staff_users
                    )
                    staff_html_message = f"{html.escape(message)}<br><br><strong>Staff notified:</strong> {mention_html}"

                    if hasattr(self.client, 'send_text'):
                        await self.client.send_text(intake_room_id, text=full_message, html=staff_html_message)
                    else:
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=full_message,
                            format=Format.HTML,
                            formatted_body=staff_html_message
                        )
                        await self.client.send_message(intake_room_id, content)
                else:
                    if hasattr(self.client, 'send_text'):
                        if html_message:
                            await self.client.send_text(intake_room_id, text=message, html=html_message)
                        else:
                            await self.client.send_text(intake_room_id, text=message)
                    else:
                        if html_message:
                            content = TextMessageEventContent(
                                msgtype=MessageType.TEXT,
                                body=message,
                                format=Format.HTML,
                                formatted_body=html_message
                            )
                        else:
                            content = TextMessageEventContent(msgtype=MessageType.TEXT, body=message)
                        await self.client.send_message(intake_room_id, content)
            else:
                if hasattr(self.client, 'send_text'):
                    if html_message:
                        await self.client.send_text(intake_room_id, text=message, html=html_message)
                    else:
                        await self.client.send_text(intake_room_id, text=message)
                else:
                    if html_message:
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=message,
                            format=Format.HTML,
                            formatted_body=html_message
                        )
                    else:
                        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=message)
                    await self.client.send_message(intake_room_id, content)

            self.log.info(f"Notification sent to New Ticket Notification room {intake_room_id}")
            return True
        except Exception as e:
            self.log.error(f"Failed to send notification to New Ticket Notification room {intake_room_id}: {e}")
            return False