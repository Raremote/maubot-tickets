from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .base import TicketsHandlerBase


class TicketsHandlerUser(TicketsHandlerBase):
    """User DM commands for ticket system."""
    @TicketsHandlerBase.ticket_command.subcommand("create", help="Create a new support ticket (title | description)")
    @command.argument("details", label="title | description", required=True, pass_raw=True)
    async def ticket_create(self, evt: MessageEvent, details: str) -> None:
        self.log.info(f"Ticket create command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message with the bot
        if not await self._is_direct_message(evt):
            await evt.reply(
                "❌ This command can only be used in a direct message with the bot.\n\n"
                "**To create a ticket:**\n"
                "1. Send a direct message to this bot\n"
                "2. Use: `!ticket create <title> | <description>`"
            )
            return

        # Ensure this is not a ticket room (ticket rooms can have only 2 members)
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ This command cannot be used in a ticket room. Please use a direct message with the bot.")
            return

        # Ensure this is not a New Ticket Notification room
        intake_room = self.db.get_intake_room(evt.room_id)
        if intake_room:
            await evt.reply("❌ This command cannot be used in a New Ticket Notification room. Please use a direct message with the bot.")
            return

        # Parse title and description
        parts = details.split("|", 1)
        if len(parts) < 2:
            await evt.reply(
                "Please provide both title and description separated by '|'.\n"
                "Example: `!ticket create Login issue | I cannot login to my account`"
            )
            return

        title = parts[0].strip()
        description = parts[1].strip()

        if not title or not description:
            await evt.reply("Title and description cannot be empty.")
            return

        # Get enabled categories
        all_categories = self.db.get_all_categories()
        categories = [cat for cat in all_categories if cat.get("enabled", True)]
        if not categories:
            await evt.reply(
                "❌ No enabled ticket categories exist, therefore no ticket can be created.\n"
                "Please notify an administrator to enable categories."
            )
            return

        # Check that at least one enabled New Ticket Notification room exists
        try:
            intake_rooms = self.db.get_all_intake_rooms()
            enabled_intake_rooms = [r for r in intake_rooms if r.get("enabled", True)]

            if not enabled_intake_rooms:
                await evt.reply(
                     "❌ No New Ticket Notification rooms are configured for ticket notifications.\n\n"
                    "**An admin needs to:**\n"
                    "1. Go to a support room\n"
                    "2. Run: `!ticket ntn_room add`\n"
                    "3. Ensure the room is enabled with: `!ticket ntn_room enable`"
                )
                return
        except Exception as e:
            self.log.error(f"Database error getting intake rooms: {e}")
            await evt.reply("❌ Database error. Please try again later.")
            return

        # Store pending ticket data (New Ticket Notification room will be selected after category selection)
        pending_data = {
            "type": "ticket",
            "title": title,
            "description": description,
        }
        self._set_pending_ticket(evt, pending_data)

        # List categories for user to choose
        category_list = "## Select a category for your ticket:\n\n"
        for i, cat in enumerate(categories, 1):
            status = "✅" if cat["enabled"] else "❌"
            category_list += f"{i}. **{cat['name']}** (`{cat['category_id']}`) {status}\n"
            if cat["description"]:
                category_list += f"   {cat['description']}\n"
            category_list += "\n"
        category_list += (
            "Please reply with the category **ID** (e.g., `tech`) or **number** (e.g., `1`).\n"
            f"If you don't respond within {self.pending_timeout} seconds, the ticket creation will be cancelled."
        )

        await evt.reply(category_list)
    @TicketsHandlerBase.ticket_command.subcommand("my", help="List your tickets (optional status: open, in_progress, closed, resolved, all). If you are not in a ticket room, an invite will be sent.")
    @command.argument("status", label="status", required=False)
    async def ticket_my(self, evt: MessageEvent, status: Optional[str] = None) -> None:
        self.log.info(f"Ticket my command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message with the bot
        if not await self._is_direct_message(evt):
            await evt.reply(
                "❌ This command can only be used in a direct message with the bot.\n\n"
                "**To view your tickets:**\n"
                "1. Send a direct message to this bot\n"
                "2. Use: `!ticket my`"
            )
            return

        # Ensure this is not a ticket room (ticket rooms can have only 2 members)
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ This command cannot be used in a ticket room. Please use a direct message with the bot.")
            return

        # Ensure this is not a New Ticket Notification room
        intake_room = self.db.get_intake_room(evt.room_id)
        if intake_room:
            await evt.reply("❌ This command cannot be used in a New Ticket Notification room. Please use a direct message with the bot.")
            return

        # Validate status if provided
        if status:
            status = status.lower()
            valid_statuses = ["open", "in_progress", "closed", "resolved", "all"]
            if status not in valid_statuses:
                await evt.reply(
                    f"❌ Invalid status. Valid options are: {', '.join(valid_statuses)}.\n"
                    f"Example: `!ticket my open`"
                )
                return

        try:
            # Convert "all" to None (no filtering)
            filter_status = None if status == "all" else status

            tickets = self.db.get_tickets_by_creator(evt.sender, filter_status)

            if not tickets:
                status_msg = f" with status '{status}'" if status and status != "all" else ""
                await evt.reply(f"You have no tickets{status_msg}.")
                return

            # Limit to 5 tickets to avoid message too long
            tickets_to_show = tickets[:5]

            # Build plain text and HTML responses
            status_label = f" (status: {status})" if status and status != "all" else ""
            plain_response = f"## Your Tickets{status_label}:\n\n"
            html_response = f"<h2>Your Tickets{status_label}:</h2><br>"

            invites_sent = 0
            for ticket in tickets_to_show:
                # Get user's membership state in the ticket room
                membership = await self._get_user_membership(evt.sender, ticket["ticket_room_id"])
                ticket_status = ticket.get("status", "open")
                footer_text = ""

                if ticket_status in ("closed", "resolved"):
                    # Closed or resolved tickets - no invites, but show appropriate status
                    if membership == Membership.JOIN:
                        # User is already in the room, nothing needed
                        pass
                    elif membership == Membership.INVITE:
                        footer_text = "📬 You have a pending invite to this closed ticket."
                    else:
                        # User is not a member or unknown state
                        footer_text = "✅ Ticket closed - no invite sent."
                else:
                    # Open or in-progress tickets - normal invite logic
                    if membership == Membership.JOIN:
                        # User is already in the room, nothing needed
                        pass
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
                                footer_text = "📬 You have been invited back to this ticket room."
                            else:
                                footer_text = "⚠️ Could not send invite (rate limited or error). Please try again later."
                    else:
                        # Unknown membership state, log but don't attempt invite
                        self.log.warning(f"Unexpected membership state for {evt.sender} in {ticket['ticket_room_id']}: {membership}")
                        footer_text = "⚠️ Unknown membership state."

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

            # Send response with HTML if supported
            if hasattr(self.client, 'send_text'):
                await self.client.send_text(evt.room_id, text=plain_response, html=html_response)
            else:
                await evt.reply(plain_response)
        except Exception as e:
            self.log.error(f"Error in ticket_my command: {e}")
            await evt.reply("❌ Error retrieving your tickets. Please try again later.")

