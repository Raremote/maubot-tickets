from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .base import TicketsHandlerBase


class TicketsHandlerEvents(TicketsHandlerBase):
    """Event handlers for ticket system."""
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



    @TicketsHandlerBase.ticket_command.subcommand("help", help="Show help for ticket commands")
    async def ticket_help(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket help command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        await self._show_help(evt)