from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .base import TicketsHandlerBase


class TicketsHandlerStaff(TicketsHandlerBase):
    """Staff commands for ticket system (moderator+)."""
    @TicketsHandlerBase.ticket_command.subcommand("search", help="Search/filter tickets with clickable room links (moderator+)")
    @command.argument("filters", label="filters", required=False, pass_raw=True)
    async def ticket_search(self, evt: MessageEvent, filters: Optional[str] = None) -> None:
        self.log.info(f"Ticket search command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (search commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Search commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Search commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        # Ensure user has moderator permissions
        if not await self.ensure_moderator(evt):
            return

        # Parse filters (simple key=value parsing)
        status = None
        assignee = None
        creator = None
        category = None
        search_term = None

        if filters:
            # Try to parse key=value pairs separated by spaces
            # Simple parsing: split by spaces, then by =
            parts = filters.split()
            for part in parts:
                if '=' in part:
                    key, value = part.split('=', 1)
                    if key == 'status':
                        status = value
                    elif key == 'assignee':
                        assignee = value
                    elif key == 'creator':
                        creator = value
                    elif key == 'category':
                        category = value
                    elif key == 'search':
                        search_term = value
                else:
                    # If no =, treat as search term
                    search_term = part

        # Get tickets
        tickets = self.db.search_tickets(
            status=status,
            assignee=assignee,
            creator=creator,
            category_id=category,
            search_term=search_term,
            limit=20
        )

        if not tickets:
            await evt.reply("No tickets found matching your criteria.")
            return

        # Limit tickets shown to avoid message too long (like !ticket my)
        tickets_to_show = tickets[:5]

        # Build plain text and HTML responses
        plain_response = f"## Tickets ({len(tickets)} found)\n\n"
        html_response = f"<h2>Tickets ({len(tickets)} found)</h2><br>"

        invites_sent = 0
        for ticket in tickets_to_show:
            ticket_status = ticket.get("status", "open")
            footer_text = ""

            # Only send invites for open or in-progress tickets
            if ticket_status in ("open", "in_progress"):
                # Get user's membership state in the ticket room
                membership = await self._get_user_membership(evt.sender, ticket["ticket_room_id"])

                if membership == Membership.JOIN:
                    # User is already in the room, nothing needed
                    footer_text = "✅ You are already in this ticket room."
                elif membership == Membership.INVITE:
                    footer_text = "📬 You have a pending invite to this room."
                elif membership in (Membership.LEAVE, Membership.BAN, Membership.KNOCK) or membership is None:
                    # User is not a member, was banned, knocked, or membership check failed
                    # Check for recent invite attempts first
                    if self._has_recent_invite_attempt(evt.sender, ticket["ticket_room_id"]):
                        footer_text = "⏳ Recent invite attempt, please wait..."
                    else:
                        # Attempt to invite with rate limiting protection
                        if await self._attempt_invite_with_backoff(evt.sender, ticket["ticket_room_id"]):
                            invites_sent += 1
                            footer_text = "📬 You have been invited to this ticket room."
                        else:
                            footer_text = "⚠️ Could not send invite (rate limited or error). Please try again later."
                else:
                    # Unknown membership state, log but don't attempt invite
                    self.log.warning(f"Unexpected membership state for {evt.sender} in {ticket['ticket_room_id']}: {membership}")
                    footer_text = "⚠️ Unknown membership state."
            else:
                # Closed or resolved tickets - no invites
                footer_text = "✅ Ticket closed - no invite sent."

            # Format ticket card with footer text
            card_plain, card_html = self._format_ticket_card(ticket, footer_text=footer_text)
            plain_response += card_plain + "\n" + "─" * 40 + "\n\n"
            if card_html:
                html_response += card_html + "<hr><br>"

        # Add note about invites if any were sent
        if invites_sent > 0:
            invite_note = f"\n\n📬 Invites have been sent for {invites_sent} ticket(s) you are not currently in."
            plain_response += invite_note
            html_response += f"<br><br>📬 Invites have been sent for {invites_sent} ticket(s) you are not currently in."

        # Remove trailing separator
        plain_response = plain_response.rstrip("\n─" * 40 + "\n\n")
        html_response = html_response.rstrip("<hr><br>")

        # Add note if there are more tickets than shown
        if len(tickets) > 5:
            plain_response += f"\n\n*(Showing 5 of {len(tickets)} tickets)*"
            html_response += f"<br><br><i>(Showing 5 of {len(tickets)} tickets)</i>"

        plain_response += "\n\n**Filter syntax:** `status=open assignee=@user category=tech search=term`"
        plain_response += "\n**Example:** `!ticket search status=open assignee=@staff:example.com category=tech`"

        html_response += "<br><br><strong>Filter syntax:</strong> <code>status=open assignee=@user category=tech search=term</code>"
        html_response += "<br><strong>Example:</strong> <code>!ticket search status=open assignee=@staff:example.com category=tech</code>"

        # Send response with HTML if supported
        if hasattr(self.client, 'send_text'):
            await self.client.send_text(evt.room_id, text=plain_response, html=html_response)
        else:
            await evt.reply(plain_response)

    @TicketsHandlerBase.ticket_command.subcommand("assign", help="Assign ticket to a user. Usage: !ticket assign TICKET-XXXX [@user] (must be used in any enabled command room)")
    @command.argument("args", label="TICKET-XXXX [@user]", required=True, pass_raw=True)
    async def ticket_assign(self, evt: MessageEvent, args: str) -> None:
        self.log.info(f"Ticket assign command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Parse arguments
        ticket_number, user = self._parse_assign_args(evt)
        if not ticket_number:
            await evt.reply("❌ Invalid command syntax. Usage:\n"
                           "`!ticket assign TICKET-XXXX [@user]` (must be used in any enabled command room)\n"
                           "- `!ticket assign TICKET-XXXX` - assign yourself (moderator+)\n"
                           "- `!ticket assign TICKET-XXXX @user` - assign specific user (admin only)")
            return

        # Get ticket by number
        ticket = self.db.get_ticket_by_number(ticket_number)
        if not ticket:
            await evt.reply(f"❌ Ticket '{ticket_number}' not found.")
            return



        # Ensure the room is registered as a command room and is enabled
        command_room = self.db.get_command_room(evt.room_id)
        if not command_room:
            await evt.reply("❌ This room is not a valid command room.")
            return
        if not command_room.get("enabled", True):
            await evt.reply("❌ This command room is disabled.")
            return

        # Check permissions based on action
        # Self-assignment (user is None): requires moderator (power level >= 50)
        # Assigning others (user specified): requires admin (power level >= 100)
        if user is None:
            # Self-assignment - moderator permission required
            if not await self.ensure_moderator(evt):
                return
        else:
            # Assigning someone else - admin permission required
            if not await self.ensure_admin(evt):
                return

        # Determine target user (self if none specified)
        if user is None:
            user = evt.sender

        # Validate user ID format
        if not user.startswith("@") or ":" not in user:
            await evt.reply("❌ Invalid user ID format. Please use @user:server.com")
            return
        if user == self.client.mxid:
            await evt.reply("❌ Cannot assign ticket to the bot itself.")
            return

        # Check if user is staff in this command room
        if not await self._is_staff_user(user, evt.room_id):
            await evt.reply(f"❌ {user} does not have staff permissions (moderator/admin) in this command room.")
            return

        # Check if user is already assigned
        assignees = self.db.get_assignees(ticket["id"])
        if user in assignees:
            await evt.reply(f"❌ {user} is already assigned to this ticket.")
            return

        old_assignee_count = len(assignees)

        # Update database
        success = self.db.assign_ticket(ticket["id"], user)
        if success:
            # Update space visibility (ticket may now be in_progress)
            updated_ticket = self.db.get_ticket_by_number(ticket_number)
            if updated_ticket:
                space_success = await self._update_ticket_space_visibility(updated_ticket, updated_ticket.get("status", "open"))
                if not space_success:
                    self.log.warning("Failed to update space visibility, but assignment succeeded")
            # Update room topic to reflect in-progress status only if first assignee
            if old_assignee_count == 0:
                topic_success = await self._update_room_topic_for_ticket(ticket, "in_progress")
                if not topic_success:
                    self.log.warning("Failed to update room topic, but assignment succeeded")
            # Try to invite user to room and set power level
            try:
                await self.client.invite_user(ticket["ticket_room_id"], user)
            except Exception as e:
                # User might already be in the room, log and continue
                self.log.warning(f"Could not invite {user} to room (might already be member): {e}")

            # Update power levels to give moderator access
            power_success = await self._update_power_levels_for_user(ticket["ticket_room_id"], user, 50)
            if not power_success:
                self.log.warning(f"Failed to update power levels for {user}, but assignment succeeded")

            # Determine rooms
            ticket_room_id = ticket["ticket_room_id"]
            intake_room_id = ticket.get("intake_room_id")
            in_ticket_room = evt.room_id == ticket_room_id
            in_intake_room = intake_room_id and evt.room_id == intake_room_id
            self.log.debug(f"Context: in_ticket_room={in_ticket_room}, in_intake_room={in_intake_room}, intake_room_id={intake_room_id}")

            plain_mention, html_mention = self._format_user_mention(user)

            # Messages for different contexts
            if old_assignee_count > 0:
                # Adding additional assignee
                intake_plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" added assignee {plain_mention}"
                intake_html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" added assignee {html_mention}"
                ticket_plain_msg = f"✅ {plain_mention} has been added as an assignee to your ticket."
                ticket_html_msg = f"✅ {html_mention} has been added as an assignee to your ticket."
                reply_msg = f"✅ Added assignee {user}"
            else:
                # First assignee
                intake_plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" assigned to {plain_mention}"
                intake_html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" assigned to {html_mention}"
                ticket_plain_msg = f"✅ {plain_mention} has been assigned to your ticket."
                ticket_html_msg = f"✅ {html_mention} has been assigned to your ticket."
                reply_msg = f"✅ Ticket assigned to {user}"

                # Send notification to ticket room if not already there
                if not in_ticket_room:
                    self.log.debug(f"Sending notification to ticket room {ticket_room_id} (not in ticket room)")
                self.log.debug(f"Sending notification to ticket room {ticket_room_id} (not in ticket room)")
                try:
                    content = TextMessageEventContent(
                        msgtype=MessageType.TEXT,
                        body=ticket_plain_msg,
                        format=Format.HTML,
                        formatted_body=ticket_html_msg
                    )
                    await self.client.send_message(ticket_room_id, content)
                except Exception as e:
                    self.log.warning(f"Failed to send assignment notification to ticket room: {e}")

                # Send notification to intake room if different from current room
                if intake_room_id and not in_intake_room:
                    self.log.debug(f"Sending notification to intake room {intake_room_id} (not in intake room)")
                self.log.debug(f"Sending notification to intake room {intake_room_id} (not in intake room)")
                await self._notify_ntn_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)

            # Send reply in current room (if not already covered by notifications)
            # If we're in ticket room, we already sent notification, so skip reply
            # If we're in intake room, we're not sending intake notification, so send reply
            # If we're in other room, send reply
            if in_ticket_room:
                # Already notified ticket room, skip reply to avoid duplicate
                pass
            else:
                await evt.reply(reply_msg)
        else:
            await evt.reply("❌ Failed to assign ticket.")

    @TicketsHandlerBase.ticket_command.subcommand("unassign", help="Unassign the ticket (or specific user). Usage: !ticket unassign TICKET-XXXX [@user|all] (must be used in any enabled command room)")
    @command.argument("args", label="TICKET-XXXX [@user]", required=True, pass_raw=True)
    async def ticket_unassign(self, evt: MessageEvent, args: str) -> None:
        self.log.info(f"Ticket unassign command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Parse arguments
        ticket_number, user = self._parse_assign_args(evt)
        if not ticket_number:
            await evt.reply("❌ Invalid command syntax. Usage:\n"
                           "`!ticket unassign TICKET-XXXX [@user|all]` (must be used in any enabled command room)\n"
                           "- `!ticket unassign TICKET-XXXX` - unassign yourself (moderator+)\n"
                           "- `!ticket unassign TICKET-XXXX @user` - unassign specific user (admin only)\n"
                           "- `!ticket unassign TICKET-XXXX all` - unassign all assignees (admin only)")
            return

        # Get ticket by number
        ticket = self.db.get_ticket_by_number(ticket_number)
        if not ticket:
            await evt.reply(f"❌ Ticket '{ticket_number}' not found.")
            return



        # Ensure the room is registered as a command room and is enabled
        command_room = self.db.get_command_room(evt.room_id)
        if not command_room:
            await evt.reply("❌ This room is not a valid command room.")
            return
        if not command_room.get("enabled", True):
            await evt.reply("❌ This command room is disabled.")
            return

        assignees = self.db.get_assignees(ticket["id"])

        # Determine action based on user argument
        if user is None:
            # Self-unassignment
            target_user = evt.sender
            action = "self"
        elif user == "all":
            # Unassign all assignees
            action = "all"
        else:
            # Specific user
            # Validate user ID format
            if not user.startswith("@") or ":" not in user:
                await evt.reply("❌ Invalid user ID format. Please use @user:server.com")
                return
            if user == self.client.mxid:
                await evt.reply("❌ Cannot unassign the bot itself.")
                return
            target_user = user
            action = "specific"

        # Check permissions based on action
        # Self-unassignment: requires moderator (power level >= 50)
        # Unassign others or all: requires admin (power level >= 100)
        if action == "self":
            # Self-unassignment - moderator permission required
            if not await self.ensure_moderator(evt):
                return
        else:
            # Unassigning others or all - admin permission required
            if not await self.ensure_admin(evt):
                return

        # Handle self-unassignment
        if action == "self":
            if target_user not in assignees:
                await evt.reply("❌ You are not assigned to this ticket.")
                return

            success = self.db.remove_assignee(ticket["id"], target_user)
            if success:
                # Update power levels to remove moderator access
                try:
                    await self._update_power_levels_for_user(ticket["ticket_room_id"], target_user, 0)
                except Exception as e:
                    self.log.warning(f"Failed to update power levels for {target_user}: {e}")

                # Update room topic if no assignees remain
                remaining_assignees = self.db.get_assignees(ticket["id"])
                if not remaining_assignees:
                    topic_success = await self._update_room_topic_for_ticket(ticket, "open")
                    if not topic_success:
                        self.log.warning("Failed to update room topic, but assignee removed")

                # Update space visibility if status changed
                updated_ticket = self.db.get_ticket_by_number(ticket_number)
                if updated_ticket:
                    space_success = await self._update_ticket_space_visibility(updated_ticket, updated_ticket.get("status", "open"))
                    if not space_success:
                        self.log.warning("Failed to update space visibility, but assignee removed")

                # Determine rooms
                ticket_room_id = ticket["ticket_room_id"]
                intake_room_id = ticket.get("intake_room_id")
                in_ticket_room = evt.room_id == ticket_room_id
                in_intake_room = intake_room_id and evt.room_id == intake_room_id

                plain_mention, html_mention = self._format_user_mention(target_user)
                intake_plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" unassigned from {plain_mention}"
                intake_html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" unassigned from {html_mention}"
                ticket_plain_msg = f"ℹ️ {plain_mention} has been unassigned from your ticket."
                ticket_html_msg = f"ℹ️ {html_mention} has been unassigned from your ticket."
                reply_msg = f"✅ You have been unassigned from the ticket."

                # Send notification to ticket room if not already there
                if not in_ticket_room:
                    try:
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=ticket_plain_msg,
                            format=Format.HTML,
                            formatted_body=ticket_html_msg
                        )
                        await self.client.send_message(ticket_room_id, content)
                    except Exception as e:
                        self.log.warning(f"Failed to send unassignment notification to ticket room: {e}")

                # Send notification to intake room if different from current room
                if intake_room_id and not in_intake_room:
                    await self._notify_ntn_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)

                # Send reply in current room (if not already covered by notifications)
                if in_ticket_room:
                    # Already notified ticket room, skip reply to avoid duplicate
                    pass
                else:
                    await evt.reply(reply_msg)
            else:
                await evt.reply("❌ Failed to unassign yourself from ticket.")

        # Handle specific user unassignment
        elif action == "specific":
            # Check if specified user is actually assigned
            if target_user not in assignees:
                await evt.reply(f"❌ {target_user} is not assigned to this ticket.")
                return

            success = self.db.remove_assignee(ticket["id"], target_user)
            if success:
                # Update power levels to remove moderator access
                try:
                    await self._update_power_levels_for_user(ticket["ticket_room_id"], target_user, 0)
                except Exception as e:
                    self.log.warning(f"Failed to update power levels for {target_user}: {e}")

                # Update room topic if no assignees remain
                remaining_assignees = self.db.get_assignees(ticket["id"])
                if not remaining_assignees:
                    topic_success = await self._update_room_topic_for_ticket(ticket, "open")
                    if not topic_success:
                        self.log.warning("Failed to update room topic, but assignee removed")

                # Update space visibility if status changed
                updated_ticket = self.db.get_ticket_by_number(ticket_number)
                if updated_ticket:
                    space_success = await self._update_ticket_space_visibility(updated_ticket, updated_ticket.get("status", "open"))
                    if not space_success:
                        self.log.warning("Failed to update space visibility, but assignee removed")

                # Determine rooms
                ticket_room_id = ticket["ticket_room_id"]
                intake_room_id = ticket.get("intake_room_id")
                in_ticket_room = evt.room_id == ticket_room_id
                in_intake_room = intake_room_id and evt.room_id == intake_room_id

                plain_mention, html_mention = self._format_user_mention(target_user)
                intake_plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" unassigned from {plain_mention}"
                intake_html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" unassigned from {html_mention}"
                ticket_plain_msg = f"ℹ️ {plain_mention} has been unassigned from your ticket."
                ticket_html_msg = f"ℹ️ {html_mention} has been unassigned from your ticket."
                reply_msg = f"✅ {target_user} has been unassigned from the ticket."

                # Send notification to ticket room if not already there
                if not in_ticket_room:
                    try:
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=ticket_plain_msg,
                            format=Format.HTML,
                            formatted_body=ticket_html_msg
                        )
                        await self.client.send_message(ticket_room_id, content)
                    except Exception as e:
                        self.log.warning(f"Failed to send unassignment notification to ticket room: {e}")

                # Send notification to intake room if different from current room
                if intake_room_id and not in_intake_room:
                    await self._notify_ntn_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)

                # Send reply in current room (if not already covered by notifications)
                if in_ticket_room:
                    # Already notified ticket room, skip reply to avoid duplicate
                    pass
                else:
                    await evt.reply(reply_msg)
            else:
                await evt.reply("❌ Failed to unassign user from ticket.")

        # Handle unassign all
        elif action == "all":
            if not assignees:
                await evt.reply("❌ This ticket is not assigned to anyone.")
                return

            success = self.db.unassign_ticket(ticket["id"])
            if success:
                # Update power levels for all assignees (optional, could be heavy)
                # For now, skip - power levels will be updated if they leave the room

                # Update room topic to reflect open status
                topic_success = await self._update_room_topic_for_ticket(ticket, "open")
                if not topic_success:
                    self.log.warning("Failed to update room topic, but ticket unassigned")

                # Update space visibility if status changed
                updated_ticket = self.db.get_ticket_by_number(ticket_number)
                if updated_ticket:
                    space_success = await self._update_ticket_space_visibility(updated_ticket, updated_ticket.get("status", "open"))
                    if not space_success:
                        self.log.warning("Failed to update space visibility, but ticket unassigned")

                # Determine rooms
                ticket_room_id = ticket["ticket_room_id"]
                intake_room_id = ticket.get("intake_room_id")
                in_ticket_room = evt.room_id == ticket_room_id
                in_intake_room = intake_room_id and evt.room_id == intake_room_id
                self.log.debug(f"Context: in_ticket_room={in_ticket_room}, in_intake_room={in_intake_room}, intake_room_id={intake_room_id}")

                intake_plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" unassigned"
                intake_html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" unassigned"
                ticket_plain_msg = f"ℹ️ All assignees have been unassigned from your ticket."
                ticket_html_msg = f"ℹ️ All assignees have been unassigned from your ticket."
                reply_msg = "✅ Ticket unassigned."

                # Send notification to ticket room if not already there
                if not in_ticket_room:
                    self.log.debug(f"Sending notification to ticket room {ticket_room_id} (not in ticket room)")
                    try:
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=ticket_plain_msg,
                            format=Format.HTML,
                            formatted_body=ticket_html_msg
                        )
                        await self.client.send_message(ticket_room_id, content)
                    except Exception as e:
                        self.log.warning(f"Failed to send unassignment notification to ticket room: {e}")

                # Send notification to intake room if different from current room
                if intake_room_id and not in_intake_room:
                    self.log.debug(f"Sending notification to intake room {intake_room_id} (not in intake room)")
                    await self._notify_ntn_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)

                # Send reply in current room (if not already covered by notifications)
                if in_ticket_room:
                    # Already notified ticket room, skip reply to avoid duplicate
                    pass
                else:
                    await evt.reply(reply_msg)
            else:
                await evt.reply("❌ Failed to unassign ticket.")

    @TicketsHandlerBase.ticket_command.subcommand("debug", help="Debug command (staff only)")
    async def ticket_debug(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket debug command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        if not await self.ensure_moderator(evt):
            return

        # Check database
        intake_rooms = self.db.get_all_intake_rooms()
        command_rooms = self.db.get_all_command_rooms()
        tickets = self.db.get_all_tickets(limit=5)

        # Get schema information
        schema_info = ""
        try:
            from sqlalchemy import inspect
            inspector = inspect(self.db.engine)
            table_names = inspector.get_table_names()
            schema_info = f"\n**Tables:** {', '.join(table_names)}\n"
            for table_name in ['intake_rooms', 'command_rooms', 'tickets', 'categories']:
                if table_name in table_names:
                    columns = [col['name'] for col in inspector.get_columns(table_name)]
                    schema_info += f"- **{table_name}**: {', '.join(columns)}\n"
        except Exception as e:
            schema_info = f"\n**Schema inspection error:** {e}\n"

        response = (
            "## Debug Information\n\n"
            f"**Room ID:** `{evt.room_id}`\n"
            f"**User:** {evt.sender}\n"
            f"**Intake rooms in DB:** {len(intake_rooms)}\n"
            f"**Command rooms in DB:** {len(command_rooms)}\n"
            f"**Tickets in DB:** {len(tickets)}\n"
            f"**Bot MXID:** {self.client.mxid}\n"
            f"{schema_info}"
        )

        if intake_rooms:
            response += "\n**Intake rooms:**\n"
            for room in intake_rooms:
                response += f"- {room['name']} ({room['room_id']})\n"

        if command_rooms:
            response += "\n**Command rooms:**\n"
            for room in command_rooms:
                response += f"- {room['name']} ({room['room_id']})\n"

        await evt.reply(response)

    @event.on(EventType.ROOM_MESSAGE)
    async def handle_pending_ticket(self, evt: MessageEvent) -> None:
        """Handle pending ticket or New Ticket Notification room category selection."""
        # Skip if message is a command (starts with !)
        body = evt.content.body.strip()
        if body.startswith("!"):
            return
        # Check for pending ticket or intake room
        pending = self._get_pending_ticket(evt)
        if not pending:
            return

        pending_type = pending.get("type", "ticket")  # default to ticket for backward compatibility



        # For ticket pending, only process in DMs
        if pending_type == "ticket" and not await self._is_direct_message(evt):
            return

        # For intake room pending, we're already in the room (not DM)

        category_input = body.strip()
        all_categories = self.db.get_all_categories()
        categories = [cat for cat in all_categories if cat.get("enabled", True)]
        if not categories:
            # Should not happen because we checked earlier, but just in case
            await evt.reply("❌ No enabled categories available. Operation cancelled.")
            self._clear_pending_ticket(evt)
            return

        # Handle "all" selection
        selected_category = None
        category_id = None

        # Check for "all" selection (allowed for intake rooms)
        if category_input.lower() == "all" or category_input == "0":
            if pending_type == "intake_room":
                category_id = "all"
            else:
                await evt.reply(
                    "❌ Category 'all' is not a valid ticket category. "
                    "Please choose a category from the list."
                )
                self._clear_pending_ticket(evt)
                return
        else:
            # Find category by category_id first
            for cat in categories:
                if cat["category_id"] == category_input:
                    selected_category = cat
                    break
            # If not found, try numeric index (1-based, since 0 is "all")
            if not selected_category and category_input.isdigit():
                idx = int(category_input) - 1  # "1" -> 0, "2" -> 1, etc.
                if 0 <= idx < len(categories):
                    selected_category = categories[idx]

            if selected_category:
                category_id = selected_category["category_id"]

        if not category_id:
            # Invalid selection, show list again
            await evt.reply(
                f"❌ Invalid category selection: '{category_input}'. Please choose a valid category ID or number from the list above."
            )
            return

        # Clear pending state
        self._clear_pending_ticket(evt)
        # Add category to pending data
        pending["category_id"] = category_id

        # Route to appropriate creation method
        if pending_type == "intake_room":
            await self._create_intake_room_from_pending(evt, pending)
        else:  # ticket
            await self._create_ticket_from_pending(evt, pending)

    @event.on(EventType.ROOM_MEMBER)
    async def handle_membership(self, evt: StateEvent) -> None:
        """Handle membership events for ticket rooms (knocks, joins, leaves)."""
        room_id = evt.room_id
        actor_id = evt.sender  # User who caused the membership change
        user_id = evt.state_key  # User whose membership changed
        membership = evt.content.membership

        # Skip bot's own membership changes
        if actor_id == self.client.mxid or user_id == self.client.mxid:
            return

        self.log.info(f"Membership event: {actor_id} changed {user_id} to {membership} in room {room_id}")

        # Check if this is a ticket room
        ticket = await self._get_ticket_for_room(room_id)
        if not ticket:
            self.log.debug(f"Room {room_id} is not a ticket room, ignoring membership event")
            return

        ticket_id = ticket["id"]
        intake_room_id = ticket.get("intake_room_id")


        # Handle different membership types
        if membership == Membership.KNOCK:
            self.log.info(f"Knock received from {user_id} in ticket room {room_id}")

            # Check if user is ticket creator
            if user_id == ticket["creator"]:
                self.log.info(f"User {user_id} is ticket creator, approving knock")
                try:
                    await self.client.invite_user(room_id, user_id)
                    self.log.info(f"Invited ticket creator {user_id} to room {room_id}")
                except Exception as e:
                    self.log.error(f"Failed to invite ticket creator: {e}")
                return

            # Check if user is staff in any command room
            if await self._is_staff_in_any_command_room(user_id):
                self.log.info(f"User {user_id} is staff in a command room, approving knock")
                try:
                    await self.client.invite_user(room_id, user_id)
                    self.log.info(f"Invited staff {user_id} to room {room_id}")
                except Exception as e:
                    self.log.error(f"Failed to invite staff: {e}")
                return

            self.log.info(f"User {user_id} is not authorized to join ticket room {room_id}, ignoring knock")

        elif membership == Membership.JOIN:
            self.log.info(f"User {user_id} joined ticket room {room_id}")

            # Skip ticket creator (already in room, not staff assignment)
            if user_id == ticket["creator"]:
                self.log.info(f"User {user_id} is ticket creator, skipping auto-assignment")
                return

            # Check if user is staff in any command room
            if not await self._is_staff_in_any_command_room(user_id):
                self.log.info(f"User {user_id} is not staff in any command room, not auto-assigning")
                return

            self.log.info(f"User {user_id} is staff in a command room, proceeding with auto-assignment")



            # Check if user is already assigned
            assignees = self.db.get_assignees(ticket_id)
            self.log.info(f"Current assignees for ticket {ticket['ticket_number']}: {assignees}")
            if user_id in assignees:
                self.log.info(f"User {user_id} is already assigned to ticket {ticket['ticket_number']}")
                return

            self.log.info(f"Attempting to auto-assign {user_id} to ticket {ticket['ticket_number']}")

            # Auto-assign user to ticket
            try:
                success = self.db.add_assignee(ticket_id, user_id)
                if success:
                    self.log.info(f"Auto-assigned {user_id} to ticket {ticket['ticket_number']}")

                    # Update room topic to reflect in-progress status if needed
                    current_assignees = self.db.get_assignees(ticket_id)
                    if len(current_assignees) == 1:  # First assignee
                        topic_success = await self._update_room_topic_for_ticket(ticket, "in_progress")
                        if not topic_success:
                            self.log.warning("Failed to update room topic, but assignment succeeded")

                    # Update power levels to give moderator access
                    power_success = await self._update_power_levels_for_user(room_id, user_id, 50)
                    if not power_success:
                        self.log.warning(f"Failed to update power levels for {user_id}, but assignment succeeded")

                    # Notify intake room about auto-assignment
                    plain_mention, html_mention = self._format_user_mention(user_id)
                    plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" auto-assigned to {plain_mention} (joined room)"
                    html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" auto-assigned to {html_mention} (joined room)"
                    await self._notify_ntn_room(ticket, plain_msg, mention_staff=False, html_message=html_msg)

                    # Notify ticket room about assignment
                    try:
                        plain_text = f"✅ {plain_mention} has been assigned to your ticket (auto-assigned upon joining)."
                        html_text = f"✅ {html_mention} has been assigned to your ticket (auto-assigned upon joining)."
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=plain_text,
                            format=Format.HTML,
                            formatted_body=html_text
                        )
                        await self.client.send_message(room_id, content)
                    except Exception as e:
                        self.log.warning(f"Failed to send assignment notification to ticket room: {e}")
                else:
                    self.log.warning(f"Failed to auto-assign {user_id} to ticket {ticket['ticket_number']}")
            except Exception as e:
                self.log.error(f"Error auto-assigning {user_id} to ticket {ticket['ticket_number']}: {e}")

        elif membership in (Membership.LEAVE, Membership.BAN):
            # Determine action based on membership and who performed it
            if membership == Membership.BAN:
                action = "banned"
                action_description = f"banned by {actor_id}"
            elif membership == Membership.LEAVE:
                if actor_id != user_id:
                    action = "kicked"
                    action_description = f"kicked by {actor_id}"
                else:
                    action = "left"
                    action_description = "left"
            self.log.info(f"User {user_id} {action_description} ticket room {room_id}")

            # Log ticket details for debugging
            self.log.info(f"Ticket details: {ticket['ticket_number']} (ID: {ticket_id}), creator: {ticket['creator']}, intake room: {ticket.get('intake_room_id', 'none')}")

            # Skip ticket creator leaving (they might leave but ticket remains)
            if user_id == ticket["creator"]:
                self.log.info(f"User {user_id} is ticket creator, skipping unassignment")
                return

            # Check if user is assigned to this ticket
            assignees = self.db.get_assignees(ticket_id)
            self.log.info(f"Current assignees for ticket {ticket['ticket_number']}: {assignees}")
            if user_id not in assignees:
                self.log.info(f"User {user_id} is not assigned to ticket {ticket['ticket_number']}, ignoring {action}")
                return

            self.log.info(f"Proceeding with auto-unassignment of {user_id} from ticket {ticket['ticket_number']}")

            # Auto-unassign user from ticket
            try:
                success = self.db.remove_assignee(ticket_id, user_id)
                if success:
                    self.log.info(f"Auto-unassigned {user_id} from ticket {ticket['ticket_number']}")

                    # Remove power level entry for user (set to default 0)
                    power_success = await self._update_power_levels_for_user(room_id, user_id, 0)
                    if not power_success:
                        self.log.warning(f"Failed to update power levels for {user_id}, but unassignment succeeded")

                    # Update room topic if no assignees remain
                    remaining_assignees = self.db.get_assignees(ticket_id)
                    if not remaining_assignees:
                        topic_success = await self._update_room_topic_for_ticket(ticket, "open")
                        if not topic_success:
                            self.log.warning("Failed to update room topic, but unassignment succeeded")

                    # Update space visibility if status changed
                    updated_ticket = self.db.get_ticket_by_number(ticket["ticket_number"])
                    if updated_ticket:
                        space_success = await self._update_ticket_space_visibility(updated_ticket, updated_ticket.get("status", "open"))
                        if not space_success:
                            self.log.warning("Failed to update space visibility, but unassignment succeeded")

                    # Notify intake room about auto-unassignment
                    plain_mention, html_mention = self._format_user_mention(user_id)
                    plain_msg = f"🎫 Ticket **{ticket['ticket_number']}** \"{ticket['title']}\" auto-unassigned from {plain_mention} ({action} room)"
                    html_msg = f"🎫 Ticket <strong>{ticket['ticket_number']}</strong> \"{html.escape(ticket['title'])}\" auto-unassigned from {html_mention} ({html.escape(action)} room)"
                    await self._notify_ntn_room(ticket, plain_msg, mention_staff=False, html_message=html_msg)

                    # Notify ticket room about unassignment
                    try:
                        plain_text = f"ℹ️ {plain_mention} has been unassigned from your ticket (auto-unassigned upon {action})."
                        html_text = f"ℹ️ {html_mention} has been unassigned from your ticket (auto-unassigned upon {html.escape(action)})."
                        content = TextMessageEventContent(
                            msgtype=MessageType.TEXT,
                            body=plain_text,
                            format=Format.HTML,
                            formatted_body=html_text
                        )
                        await self.client.send_message(room_id, content)
                    except Exception as e:
                        self.log.warning(f"Failed to send unassignment notification to ticket room: {e}")
                else:
                    self.log.warning(f"Failed to auto-unassign {user_id} from ticket {ticket['ticket_number']}")
            except Exception as e:
                self.log.error(f"Error auto-unassigning {user_id} from ticket {ticket['ticket_number']}: {e}")

    async def _create_ticket_from_pending(self, evt: MessageEvent, pending: dict) -> None:
        """Create ticket from pending data (after category selection)."""
        title = pending["title"]
        description = pending["description"]
        category_id = pending["category_id"]

        # Get intake rooms that should receive notifications for this category
        matching_intake_rooms = self.db.get_intake_rooms_for_category(category_id)
        if not matching_intake_rooms:
            await evt.reply(
                 "❌ No New Ticket Notification rooms are configured to receive notifications for this category.\n"
                 "Please notify an administrator to add a New Ticket Notification room for category "
                f"`{category_id}` or `all`."
            )
            return

        # Sort intake rooms by specificity: exact category match first, then 'all', then NULL
        # Within each group, sort by name for consistency
        def specificity_key(room):
            cat = room.get("category_id")
            if cat == category_id:
                return 0  # Exact match highest priority
            elif cat == "all" or cat is None:
                return 1  # 'all' category second priority
            else:
                return 2  # Other (should not happen)

        matching_intake_rooms.sort(key=lambda r: (specificity_key(r), r["name"]))

        # Use the first matching intake room for parent space detection and ticket association
        intake_room = matching_intake_rooms[0]
        intake_room_id = intake_room["room_id"]
        intake_room_name = intake_room["name"]



        # Create ticket room
        try:
            ticket_number = self.db.get_next_ticket_number()

            # Debug: log all matching intake rooms and their spaces
            self.log.info(f"Ticket category: {category_id}, Matching intake rooms ({len(matching_intake_rooms)}):")
            for idx, room in enumerate(matching_intake_rooms):
                room_cat = room.get("category_id")
                room_space = room.get("space_id")
                self.log.info(f"  {idx}. Room: {room['room_id']}, Category: {room_cat}, Space: {room_space}")

            # Find the first intake room with a configured space (following specificity hierarchy)
            parent_space_id = None
            space_source_room = None
            for room in matching_intake_rooms:
                space_id = room.get("space_id")
                if space_id:
                    parent_space_id = space_id
                    space_source_room = room
                    self.log.info(f"Selected space {parent_space_id} from intake room {room['room_id']} (category: {room.get('category_id')})")
                    break

            if parent_space_id:
                self.log.info(f"Using configured ticket space {parent_space_id} from intake room {space_source_room['room_id']} (category: {space_source_room.get('category_id')})")
            else:
                self.log.info("No ticket space configured in any matching intake room, ticket will not be added to any space")
            ticket_room_id = await self._create_ticket_room(
                ticket_number=ticket_number,
                title=title,
                description=description,
                creator=evt.sender,
                parent_space_id=parent_space_id
            )
            self.log.info(f"Ticket room created: {ticket_room_id}")

            ticket_id = self.db.create_ticket(
                ticket_number=ticket_number,
                creator=evt.sender,
                intake_room_id=intake_room_id,
                ticket_room_id=ticket_room_id,
                title=title,
                description=description,
                category_id=category_id,
                ticket_space_id=parent_space_id
            )
            self.log.info(f"Ticket saved to database with ID: {ticket_id}")

            # Notify matching intake rooms for this category
            notification_sent = False
            for room in matching_intake_rooms:
                try:
                    staff_users = await self._get_staff_users(room["room_id"])
                    staff_users = [u for u in staff_users if u != evt.sender]

                    # Invite all staff users to the ticket room
                    invited_count = 0
                    for staff_user in staff_users:
                        try:
                            await self.client.invite_user(ticket_room_id, staff_user)
                            self.log.info(f"Invited staff user {staff_user} to ticket room {ticket_room_id}")
                            invited_count += 1
                        except Exception as e:
                            # User might already be invited or in the room
                            self.log.warning(f"Could not invite {staff_user} to room (might already be member): {e}")

                    # Format ticket as card
                    ticket_data = {
                        "ticket_number": ticket_number,
                        "title": title,
                        "description": description,
                        "creator": evt.sender,
                        "category_id": category_id,
                        "ticket_room_id": ticket_room_id,
                        "status": "open"
                    }
                    card_plain, card_html = self._format_ticket_card(
                        ticket_data,
                        include_staff_mentions=bool(staff_users),
                        staff_users=staff_users
                    )

                    # Add header for new ticket notification
                    if invited_count > 0:
                        join_hint = f"\n\n**Staff notified and invited ({invited_count} users).** They can accept the invitation to join."
                        html_join_hint = f"<br><br><strong>Staff notified and invited ({invited_count} users).</strong> They can accept the invitation to join."
                    else:
                        join_hint = f"\n\n**Staff notified but could not be invited.** An admin can use `!ticket assign {ticket_number} @user` to invite specific staff members."
                        html_join_hint = f"<br><br><strong>Staff notified but could not be invited.</strong> An admin can use <code>!ticket assign {ticket_number} @user</code> to invite specific staff members."

                    plain_text = f"🎫 **New Ticket Created: {ticket_number}**\n\n{card_plain}{join_hint}"
                    html_text = f"🎫 <strong>New Ticket Created: {ticket_number}</strong><br><br>{card_html}{html_join_hint}" if card_html else None
                    self.log.info(f"Sending notification to intake room {room['room_id']}")
                    if html_text:
                        if hasattr(self.client, 'send_text'):
                            await self.client.send_text(room["room_id"], text=plain_text, html=html_text)
                        else:
                            content = TextMessageEventContent(
                                msgtype=MessageType.TEXT,
                                body=plain_text,
                                format=Format.HTML,
                                formatted_body=html_text
                            )
                            await self.client.send_message(room["room_id"], content)
                    else:
                        if hasattr(self.client, 'send_text'):
                            await self.client.send_text(room["room_id"], text=plain_text)
                        else:
                            content = TextMessageEventContent(msgtype=MessageType.TEXT, body=plain_text)
                            await self.client.send_message(room["room_id"], content)
                    notification_sent = True
                except Exception as e:
                    self.log.error(f"Failed to send notification to intake room {room['room_id']}: {e}")
                    continue
            if not notification_sent:
                self.log.error("Failed to send notification to any intake room")

            # Reply to user in DM with ticket card
            ticket_data = {
                "ticket_number": ticket_number,
                "title": title,
                "description": description,
                "creator": evt.sender,
                "category_id": category_id,
                "ticket_room_id": ticket_room_id,
                "status": "open"
            }
            card_plain, card_html = self._format_ticket_card(
                ticket_data,
                footer_text="Support staff have been notified and will assist you soon."
            )

            # Add success header
            plain_text = f"✅ Ticket **{ticket_number}** has been created!\n\n{card_plain}"
            html_text = f"✅ <strong>Ticket {ticket_number} has been created!</strong><br><br>{card_html}" if card_html else None

            # Send response with HTML if supported
            if hasattr(self.client, 'send_text'):
                await self.client.send_text(evt.room_id, text=plain_text, html=html_text)
            else:
                await evt.reply(plain_text)
        except Exception as e:
            self.log.error(f"Error creating ticket from pending: {e}")
            await evt.reply("❌ Failed to create ticket. Please try again later.")

    async def _create_intake_room_from_pending(self, evt: MessageEvent, pending: dict) -> None:
        """Create New Ticket Notification room from pending data (after category selection)."""
        room_id = pending["room_id"]
        category_id = pending["category_id"]

        # Generate a name based on category
        if category_id == "all":
            name = f"New Ticket Notification Room (all categories)"
        else:
            # Try to get category name for display
            category = self.db.get_category(category_id)
            if category:
                name = f"New Ticket Notification Room ({category['name']})"
            else:
                name = f"New Ticket Notification Room ({category_id})"

        try:
            success = self.db.add_intake_room(room_id, name, category_id)
            if success:
                await evt.reply(
                    f"✅ This room has been registered as a New Ticket Notification room with name: {name} "
                    f"and category: {category_id}. I will notify here when new tickets are created."
                )
                # Send a test notification
                try:
                    content = TextMessageEventContent(
                        msgtype=MessageType.TEXT,
                        body="🎫 **Test notification**: This room will receive notifications when new tickets are created."
                    )
                    await self.client.send_message(room_id, content)
                except Exception as e:
                    self.log.error(f"Failed to send test notification: {e}")
                    await evt.reply("⚠️ Note: I couldn't send a test message to this room. Make sure I have permission to send messages here.")
            else:
                await evt.reply("❌ Failed to register New Ticket Notification room. It may already exist.")
        except Exception as e:
            self.log.error(f"Error creating intake room from pending: {e}")
            await evt.reply("❌ Failed to create New Ticket Notification room. Please try again later.")



