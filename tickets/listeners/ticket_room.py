from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .base import TicketsHandlerBase


class TicketsHandlerTicketRoom(TicketsHandlerBase):
    """Ticket room commands (usable in ticket rooms)."""
    @TicketsHandlerBase.ticket_command.subcommand("close", help="Close the ticket")
    async def ticket_close(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket close command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._can_manage_ticket(evt, ticket):
            await evt.reply("❌ You must be an admin, moderator, or the ticket creator to close this ticket.")
            return

        success = self.db.close_ticket(ticket["id"])
        if success:
            # Update room topic to reflect closed status
            topic_success = await self._update_room_topic_for_ticket(ticket, "closed")
            if not topic_success:
                self.log.warning("Failed to update room topic, but ticket closed")
            # Update space visibility (remove from parent space)
            space_success = await self._update_ticket_space_visibility(ticket, "closed")
            if not space_success:
                self.log.warning("Failed to update space visibility, but ticket closed")
            # Notify intake room
            notification_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" closed"
            await evt.reply("✅ Ticket closed.")
            await self._notify_ntn_room(ticket, notification_msg, mention_staff=False)

            # Make all users leave the room to signify closure
            ticket_room_id = ticket["ticket_room_id"]
            try:
                members = await self.client.get_joined_members(ticket_room_id)
                self.log.info(f"Ticket closed, kicking {len(members)} users from room {ticket_room_id}")

                for user_id in members:
                    if user_id == self.client.mxid:
                        # Don't kick the bot itself
                        continue

                    try:
                        await self.client.kick_user(ticket_room_id, user_id, reason="Ticket closed")
                        self.log.info(f"Kicked user {user_id} from closed ticket room")
                    except Exception as e:
                        self.log.warning(f"Failed to kick user {user_id} from closed ticket room: {e}")
                        # Continue with other users
            except Exception as e:
                self.log.warning(f"Failed to get members or kick users from closed ticket room: {e}")
        else:
            await evt.reply("❌ Failed to close ticket.")

    @TicketsHandlerBase.ticket_command.subcommand("resolve", help="Resolve the ticket")
    async def ticket_resolve(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket resolve command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._can_manage_ticket(evt, ticket):
            await evt.reply("❌ You must be an admin, moderator, or the ticket creator to resolve this ticket.")
            return

        success = self.db.update_ticket_status(ticket["id"], "resolved")
        if success:
            # Update room topic to reflect resolved status
            topic_success = await self._update_room_topic_for_ticket(ticket, "resolved")
            if not topic_success:
                self.log.warning("Failed to update room topic, but ticket resolved")
            # Update space visibility (remove from parent space)
            space_success = await self._update_ticket_space_visibility(ticket, "resolved")
            if not space_success:
                self.log.warning("Failed to update space visibility, but ticket resolved")
            # Notify intake room
            notification_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" resolved"
            await evt.reply("✅ Ticket resolved.")
            await self._notify_ntn_room(ticket, notification_msg, mention_staff=False)

            # Make all users leave the room to signify resolution
            ticket_room_id = ticket["ticket_room_id"]
            try:
                members = await self.client.get_joined_members(ticket_room_id)
                self.log.info(f"Ticket resolved, kicking {len(members)} users from room {ticket_room_id}")

                for user_id in members:
                    if user_id == self.client.mxid:
                        # Don't kick the bot itself
                        continue

                    try:
                        await self.client.kick_user(ticket_room_id, user_id, reason="Ticket resolved")
                        self.log.info(f"Kicked user {user_id} from resolved ticket room")
                    except Exception as e:
                        self.log.warning(f"Failed to kick user {user_id} from resolved ticket room: {e}")
                        # Continue with other users
            except Exception as e:
                self.log.warning(f"Failed to get members or kick users from resolved ticket room: {e}")
        else:
            await evt.reply("❌ Failed to resolve ticket.")

    @TicketsHandlerBase.ticket_command.subcommand("reopen", help="Reopen a closed ticket")
    async def ticket_reopen(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket reopen command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._is_staff_in_ticket_room(evt, ticket):
            await evt.reply("❌ You must be an admin or moderator to reopen this ticket.")
            return

        success = self.db.update_ticket_status(ticket["id"], "open")
        if success:
            topic_success = await self._update_room_topic_for_ticket(ticket, "open")
            if not topic_success:
                self.log.warning("Failed to update room topic, but ticket reopened")
            # Update space visibility (add back to parent space)
            space_success = await self._update_ticket_space_visibility(ticket, "open")
            if not space_success:
                self.log.warning("Failed to update space visibility, but ticket reopened")
            # Notify intake room
            notification_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" reopened"
            await evt.reply("✅ Ticket reopened.")
            await self._notify_ntn_room(ticket, notification_msg, mention_staff=False)
        else:
            await evt.reply("❌ Failed to reopen ticket.")

    @TicketsHandlerBase.ticket_command.subcommand("note", help="Add a note to the ticket")
    @command.argument("text", label="note text", required=True, pass_raw=True)
    async def ticket_note(self, evt: MessageEvent, text: str) -> None:
        self.log.info(f"Ticket note command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._is_staff_in_ticket_room(evt, ticket):
            await evt.reply("❌ You must be an admin or moderator to add notes.")
            return

        # Save note to database
        try:
            note_id = self.db.add_note(ticket["id"], evt.sender, text)
            self.log.info(f"Note added to ticket {ticket['ticket_number']} (note ID: {note_id}): {text}")
            await evt.reply(f"✅ Note added: {text}")
        except Exception as e:
            self.log.error(f"Failed to save note for ticket {ticket['id']}: {e}")
            await evt.reply("❌ Failed to save note. Please try again.")

    @TicketsHandlerBase.ticket_command.subcommand("info", help="Show ticket information")
    async def ticket_info(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket info command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return

        # Get notes for this ticket
        notes = self.db.get_notes_for_ticket(ticket["id"])
        note_count = len(notes)
        last_note = notes[-1] if notes else None

        # Get assignees
        assignees = self.db.get_assignees(ticket["id"])
        assignee_text = ", ".join(assignees) if assignees else "None"

        response = (
            f"## Ticket {ticket['ticket_number']}\n\n"
            f"**Title:** {ticket['title']}\n"
            f"**Description:** {ticket['description']}\n"
            f"**Category:** {ticket['category_id'] or 'None'}\n"
            f"**Status:** {ticket['status']}\n"
            f"**Creator:** {ticket['creator']}\n"
            f"**Assignee:** {assignee_text}\n"
            f"**Created:** {ticket['created_at']}\n"
            f"**Updated:** {ticket['updated_at']}\n"
            f"**Notes:** {note_count} note(s)\n"
        )

        if last_note:
            response += f"\n**Last note** ({last_note['created_at']} by {last_note['author']}):\n"
            response += f"{last_note['content'][:200]}{'...' if len(last_note['content']) > 200 else ''}\n"

        if note_count > 0:
            response += f"\nUse `!ticket notes` to view all notes."

        await evt.reply(response)

    @TicketsHandlerBase.ticket_command.subcommand("notes", help="Show all notes for this ticket")
    async def ticket_notes(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket notes command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return

        # Get notes for this ticket
        notes = self.db.get_notes_for_ticket(ticket["id"])

        if not notes:
            await evt.reply("No notes have been added to this ticket.")
            return

        response = f"## Notes for Ticket {ticket['ticket_number']}\n\n"

        for i, note in enumerate(notes, 1):
            response += f"**Note #{i}** ({note['created_at']} by {note['author']}):\n"
            response += f"{note['content']}\n\n"

        response += f"Total: {len(notes)} note(s)"

        await evt.reply(response)

