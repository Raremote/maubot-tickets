from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .base import TicketsHandlerBase


class TicketsHandlerAdmin(TicketsHandlerBase):
    """Admin commands for ticket system (power level 100)."""
    @TicketsHandlerBase.ticket_command.subcommand("ntn_room", help="Manage New Ticket Notification rooms (admin only)")
    async def ntn_room(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin intake command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        await evt.reply(
            "## New Ticket Notification Room Commands (Admin only - power level 100):\n\n"
            "- `!ticket ntn_room add` - Add this room as a New Ticket Notification room\n"
            "- `!ticket ntn_room remove [all]` - Remove this room (or all) from New Ticket Notification rooms\n"
            "- `!ticket ntn_room list` - List all New Ticket Notification rooms\n"
            "- `!ticket ntn_room enable` - Enable this New Ticket Notification room\n"
            "- `!ticket ntn_room disable` - Disable this New Ticket Notification room\n"
            "- `!ticket ntn_room tris [set|unset|unset all|debug|fix]` - Ticket Room Intake Space: Configure space where new ticket rooms will be added (unset to detach from spaces)\n"
        )
    @ntn_room.subcommand("add", help="Add this room as a New Ticket Notification room")
    async def admin_add(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin add command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        # Check if room already registered as New Ticket Notification room
        existing_by_room = self.db.get_intake_room(evt.room_id)
        if existing_by_room:
            await evt.reply("❌ This room is already registered as a New Ticket Notification room.")
            return

        # Check if room is already a command room (prevent dual registration)
        existing_command_room = self.db.get_command_room(evt.room_id)
        if existing_command_room:
            await evt.reply("❌ This room is already registered as a command room. A room cannot be both a command room and a New Ticket Notification room.")
            return

        # Get enabled categories
        all_categories = self.db.get_all_categories()
        categories = [cat for cat in all_categories if cat.get("enabled", True)]
        if not categories:
            await evt.reply(
                "❌ No enabled ticket categories exist. Please create and enable at least one category first.\n"
                "Use: `!ticket category add <category_id> <name> [description]`"
            )
            return

        # Store pending New Ticket Notification room data (name will be generated after category selection)
        pending_data = {
            "type": "intake_room",
            "room_id": evt.room_id,
        }
        self._set_pending_ticket(evt, pending_data)

        # List categories for user to choose (including "all" option)
        category_list = "## Select a category for this New Ticket Notification room:\n\n"
        # Add "all" option first
        category_list += "0. **All Categories** (`all`) - Receive notifications for all ticket categories\n\n"

        # List enabled categories
        for i, cat in enumerate(categories, 1):
            status = "✅" if cat["enabled"] else "❌"
            category_list += f"{i}. **{cat['name']}** (`{cat['category_id']}`) {status}\n"
            if cat["description"]:
                category_list += f"   {cat['description']}\n"
            category_list += "\n"
        category_list += (
            "Please reply with the category **ID** (e.g., `tech`), **number** (e.g., `1`), or `all`.\n"
            f"If you don't respond within {self.pending_timeout} seconds, the New Ticket Notification room creation will be cancelled."
        )

        await evt.reply(category_list)

    @ntn_room.subcommand("remove", help="Remove this room (or all) from New Ticket Notification rooms")
    @command.argument("scope", label="scope", required=False)
    async def admin_remove(self, evt: MessageEvent, scope: Optional[str] = None) -> None:
        self.log.info(f"Admin remove command triggered: {evt.content.body}")

        is_all_operation = scope and scope.lower() == "all"

        # For "all" operation, allow from any room type
        if not is_all_operation:
            # Block if in New Ticket Notification room (except allowed commands)
            if await self._block_if_ntn_room(evt):
                return

            # Block New Ticket Notification admin commands in command rooms
            if await self._block_if_command_room_for_ntn_commands(evt):
                return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # For "all" operation, allow from ticket rooms too
        if not is_all_operation:
            # Ensure this is not a ticket room
            ticket = await self._get_ticket_for_room(evt.room_id)
            if ticket:
                await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
                return

        if not await self.ensure_admin(evt):
            return

        if is_all_operation:
            # Remove all New Ticket Notification rooms and their spaces
            removed_count = self.db.remove_all_intake_rooms()
            # Also clear all New Ticket Notification room spaces
            cleared_count = self.db.clear_all_intake_room_spaces()
            await evt.reply(f"✅ Removed all New Ticket Notification rooms ({removed_count} rooms removed) and cleared {cleared_count} space configurations.")
        else:
            # Remove only this room
            success = self.db.remove_intake_room(evt.room_id)
            if success:
                await evt.reply("✅ This room has been removed from New Ticket Notification rooms.")
            else:
                await evt.reply("❌ This room is not registered as a New Ticket Notification room.")

    @ntn_room.subcommand("list", help="List all New Ticket Notification rooms")
    async def admin_list(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin list command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        rooms = self.db.get_all_intake_rooms()
        if not rooms:
            await evt.reply("No New Ticket Notification rooms registered.")
            return

        response = "## New Ticket Notification Rooms:\n\n"
        for room in rooms:
            status = "✅ Enabled" if room["enabled"] else "❌ Disabled"
            category = room.get("category_id") or "all"
            response += f"- **{room['name']}** ({status})\n"
            response += f"  Room ID: `{room['room_id']}`\n"
            response += f"  Category: `{category}`\n\n"

        await evt.reply(response)

    @ntn_room.subcommand("enable", help="Enable this New Ticket Notification room")
    async def admin_enable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin enable command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        success = self.db.set_intake_room_enabled(evt.room_id, True)
        if success:
            await evt.reply("✅ This New Ticket Notification room has been enabled.")
        else:
            await evt.reply("❌ This room is not registered as a New Ticket Notification room.")

    @ntn_room.subcommand("disable", help="Disable this New Ticket Notification room")
    async def admin_disable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin disable command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        success = self.db.set_intake_room_enabled(evt.room_id, False)
        if success:
            await evt.reply("✅ This New Ticket Notification room has been disabled. Users cannot create tickets here.")
        else:
            await evt.reply("❌ This room is not registered as a New Ticket Notification room.")

    @ntn_room.subcommand("tris", help="Ticket Room Intake Space: Configure space where new ticket rooms will be added (unset to detach from spaces)")
    @command.argument("action", label="action", required=False, pass_raw=True)
    async def admin_intake_tris(self, evt: MessageEvent, action: Optional[str] = None) -> None:
        self.log.info(f"Admin tris (ticket room intake space) command triggered: {evt.content.body}")

        # Parse action early to check if it's "unset all"
        action_parts = action.split() if action else []
        subaction = action_parts[0].lower() if action_parts else None
        subaction_arg = " ".join(action_parts[1:]) if len(action_parts) > 1 else None
        action_lower = subaction
        is_unset_all = (action_lower == "unset" and subaction_arg == "all")

        # For "unset all" operation, allow from any room type
        if not is_unset_all:
            # Block if in command room (New Ticket Notification admin commands not allowed in command rooms)
            if await self._block_if_command_room_for_ntn_commands(evt):
                return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # For "unset all" operation, allow from ticket rooms too
        if not is_unset_all:
            # Ensure this is not a ticket room
            ticket = await self._get_ticket_for_room(evt.room_id)
            if ticket:
                await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
                return

        if not await self.ensure_admin(evt):
            return

        # For "unset all" operation, don't require the room to be an intake room
        if not is_unset_all:
        # Check if this room is a New Ticket Notification room
            intake_room = self.db.get_intake_room(evt.room_id)
            if not intake_room:
                await evt.reply("❌ This room is not registered as a New Ticket Notification room.")
                return
        else:
            # For "unset all", we still need to get intake rooms for the operation
            intake_room = self.db.get_intake_room(evt.room_id)

        if action_lower == "unset" and subaction_arg == "all":
            # Unset spaces from ALL intake rooms (including those bot has left)
            cleared_count = self.db.clear_all_intake_room_spaces()
            response = f"✅ Unset ticket spaces from {cleared_count} New Ticket Notification room(s)."
            response += "\nAll New Ticket Notification rooms now have no space configured."
            response += "\nNew tickets will NOT be added to any space."
            await evt.reply(response)
            return
        elif action_lower == "unset":
            success = self.db.set_intake_room_space(evt.room_id, None)
            if success:
                # Check if other intake rooms have spaces configured
                all_intake_rooms = self.db.get_all_intake_rooms()
                other_rooms_with_space = [
                    r for r in all_intake_rooms
                    if r.get("space_id") and r.get("enabled", True) and r["room_id"] != evt.room_id
                ]
                response = "✅ Ticket space unset for this New Ticket Notification room."
                if other_rooms_with_space:
                    # Get the category name for this intake room for context
                    category_id = intake_room.get("category_id")
                    category = self.db.get_category(category_id) if category_id else None
                    category_name = category.get("name") if category else category_id or "unknown"

                    response += f"\n\n⚠️ **Note:** {len(other_rooms_with_space)} other New Ticket Notification room(s) still have spaces configured."
                    response += f"\nNew tickets created from those New Ticket Notification rooms will still be added to their spaces."
                    response += "\nTo unset all spaces, use `!ticket ntn_room tris unset all` or run `!ticket ntn_room tris unset` in those rooms."
                else:
                    response += "\nNew tickets will NOT be added to any space (no other New Ticket Notification rooms have spaces configured)."
                await evt.reply(response)
            else:
                await evt.reply("❌ Failed to unset ticket space.")
            return
        elif action_lower == "set":
            # Check if manual space ID provided
            if subaction_arg:
                space_id = subaction_arg.strip()
                self.log.warning(f"Manual space ID specified: {space_id}")
                # Validate it's a valid room ID format
                if not space_id.startswith("!"):
                    await evt.reply(f"❌ Invalid space ID format: {space_id}. Space IDs should start with '!'")
                    return
                # Verify the room exists and is a space
                try:
                    is_space = await self._is_space_room(space_id)
                    if not is_space:
                        await evt.reply(f"❌ Room `{space_id}` is not a space (type not m.space). Please specify a valid space room ID.")
                        return
                except Exception as e:
                    await evt.reply(f"❌ Error validating space `{space_id}`: {e}. Please check the space ID and ensure the bot has access.")
                    return
                manual_space = True
                is_room_space = False  # Manual space, not necessarily the room itself
            else:
                # Auto-detect: Get space for this intake room (could be the room itself if it's a space, or its parent space)
                space_id = await self._get_space_for_room(evt.room_id)
                manual_space = False

            if not space_id:
                # Get detailed diagnostics for debugging
                is_space = await self._is_space_room(evt.room_id)
                all_parents = await self._get_all_space_parent_details(evt.room_id)

                diagnostic = f"Room `{evt.room_id}` diagnostics:\n"
                diagnostic += f"- Is space: {is_space}\n"
                diagnostic += f"- Number of SPACE_PARENT events: {len(all_parents)}\n"

                if all_parents:
                    diagnostic += "\n**SPACE_PARENT events found:**\n"
                    for idx, (parent_id, content, has_valid_via) in enumerate(all_parents):
                        via = content.get("via") if content else None
                        diagnostic += f"{idx+1}. Parent space: `{parent_id}`\n"
                        diagnostic += f"   - Has valid via: {has_valid_via}\n"
                        diagnostic += f"   - Via field: {via}\n"
                        diagnostic += f"   - Content: {json.dumps(content) if content else 'None'}\n"

                    # Check if any parent spaces have valid via
                    valid_parents = [p for p in all_parents if p[2]]
                    if valid_parents:
                        diagnostic += f"\n**Note:** Found {len(valid_parents)} parent space(s) with valid via field.\n"
                        diagnostic += "The room IS in these spaces, but space detection may have failed.\n"
                    else:
                        diagnostic += "\n**Note:** No parent spaces have a valid via field (non-empty list).\n"
                        diagnostic += "This means the room is NOT actually in any of these spaces.\n"
                        diagnostic += "The via field may be empty or missing.\n"
                else:
                    diagnostic += "\nNo SPACE_PARENT events found.\n"

                self.log.info(f"Space detection failed. {diagnostic}")

                response = "❌ This New Ticket Notification room is not a space and not in a space.\n\n"
                response += diagnostic + "\n"
                response += "**Options:**\n"
                response += "1. Add this room to a space (as a child room)\n"
                response += "2. Make this room a space (change room type to space)\n"
                if all_parents and not any(p[2] for p in all_parents):
                    response += "\n**If room already has SPACE_PARENT events:**\n"
                    response += "The via field may be empty. You may need to re-add the room to the space.\n"
                response += "\nThen try again: `!ticket ntn_room tris set`"

                await evt.reply(response)
                return

            # Check if the space is the room itself (room is a space) or a parent space
            if manual_space:
                # For manual space, check if it's the same as this room
                is_room_space = (space_id == evt.room_id)
                space_type = "manual space"
                # Don't check parent spaces for manual selection
                all_parents = []
                valid_parents = []
            else:
                # Auto-detected space logic
                is_room_space = await self._is_space_room(evt.room_id)
                space_type = "this space" if is_room_space else "parent space"

                # Get all parent spaces for diagnostic info
                all_parents = await self._get_all_space_parent_details(evt.room_id)
                valid_parents = [p for p in all_parents if p[2]]  # has_valid_via

            # Check bot membership and power level in selected space
            membership_warning = ""
            try:
                members = await self.client.get_joined_members(space_id)
                is_member = self.client.mxid in members
                if not is_member:
                    membership_warning = f"\n⚠️ **Warning:** Bot is not a member of space `{space_id}`.\n"
                    membership_warning += "The bot must be invited to the space to add ticket rooms.\n"
                else:
                    # Check power level
                    try:
                        power_levels = await self.client.get_state_event(space_id, EventType.ROOM_POWER_LEVELS)
                        bot_level = power_levels.get("users", {}).get(self.client.mxid, power_levels.get("users_default", 0))
                        if bot_level < 50:
                            membership_warning += f"\n⚠️ **Warning:** Bot power level in space is {bot_level} (< 50).\n"
                            membership_warning += "Bot may not be able to send SPACE_CHILD events.\n"
                            membership_warning += "Admin should grant bot power level ≥50 in space settings.\n"
                    except Exception as e:
                        membership_warning += f"\n⚠️ Could not check bot power level: {e}\n"
            except Exception as e:
                membership_warning = f"\n⚠️ Could not check bot membership in space: {e}\n"

            # Set the detected space as ticket space for this intake room
            success = self.db.set_intake_room_space(evt.room_id, space_id)
            if success:
                # Get category info for context
                category_id = intake_room.get("category_id")
                category = self.db.get_category(category_id) if category_id else None
                category_name = category.get("name") if category else category_id or "unknown"

                response = f"✅ Ticket space set to `{space_id}` ({space_type}).\n"
                response += f"**Category:** {category_name} (ID: {category_id})\n"
                response += "New tickets created in this New Ticket Notification room will be added to this space.\n"

                if manual_space:
                    response += f"\n**Selection reason:** Manually specified space ID."
                elif is_room_space:
                    response += f"\n**Selection reason:** This New Ticket Notification room is a space (room type: `m.space`)."
                elif len(valid_parents) == 1:
                    response += f"\n**Selection reason:** This New Ticket Notification room is in exactly one space."
                elif len(valid_parents) > 1:
                    parent_ids = [p[0] for p in valid_parents]
                    response += f"\n**Selection reason:** This New Ticket Notification room is in {len(valid_parents)} spaces: {', '.join([f'`{pid}`' for pid in parent_ids])}.\n"
                    response += f"Selected `{space_id}` as the most specific subspace."
                else:
                    response += f"\n**Note:** No valid parent spaces found with non-empty via field."

                # Add diagnostic summary
                if all_parents:
                    response += f"\n\n**Space detection details:**\n"
                    response += f"- Total SPACE_PARENT events: {len(all_parents)}\n"
                    response += f"- Valid parent spaces (non-empty via): {len(valid_parents)}\n"
                    if all_parents:
                        for idx, (parent_id, content, has_valid_via) in enumerate(all_parents):
                            via = content.get("via") if content else None
                            is_selected = parent_id == space_id
                            selected_marker = " ✅ SELECTED" if is_selected else ""
                            response += f"  {idx+1}. `{parent_id}` - via: {via} (valid: {has_valid_via}){selected_marker}\n"

                # Add membership warning if any
                if membership_warning:
                    response += f"\n{membership_warning}"

                response += f"\n**Next steps:**\n"
                response += "1. Ensure bot is a member of the space (invite if needed)\n"
                response += "2. Grant bot power level ≥50 in space settings\n"
                response += "3. Test by creating a new ticket from this New Ticket Notification room\n"
                response += "4. Use `!ticket ntn_room tris debug` to verify configuration\n"

                await evt.reply(response)
            else:
                await evt.reply("❌ Failed to set ticket space.")
            return
        elif action_lower == "debug":
            # Show detailed debugging information about the ticket space for THIS intake room
            current_ticket_space = intake_room.get("space_id")

            response = "## Space Configuration Debug (New Ticket Notification Room)\n\n"

            if not current_ticket_space:
                response += "❌ No space configured for new ticket rooms from this New Ticket Notification room.\n"
                # Show category info
                category_id = intake_room.get("category_id")
                if category_id:
                    category = self.db.get_category(category_id)
                    category_name = category.get("name") if category else category_id
                    response += f"\n**Category:** {category_name} (ID: {category_id})\n"
                await evt.reply(response)
                return

            response += f"**Configured space for new ticket rooms:** `{current_ticket_space}`\n\n"

            # Show category info
            category_id = intake_room.get("category_id")
            if category_id:
                category = self.db.get_category(category_id)
                category_name = category.get("name") if category else category_id
                response += f"**Category:** {category_name} (ID: {category_id})\n\n"

            # Check if room is actually a space
            try:
                is_space = await self._is_space_room(current_ticket_space)
                response += f"**Is valid space (m.space type):** {is_space}\n"

                if not is_space:
                    response += "❌ WARNING: Room is not a space (type not m.space). Ticket rooms cannot be added to non-space rooms.\n\n"
                    # Try to get room create event
                    try:
                        create_event = await self.client.get_state_event(current_ticket_space, EventType.ROOM_CREATE)
                        response += f"**Room create event:** `{json.dumps(create_event) if isinstance(create_event, dict) else str(create_event)}`\n\n"
                    except Exception as e:
                        response += f"**Failed to get room create event:** {e}\n\n"
            except Exception as e:
                response += f"**Error checking if room is space:** {e}\n\n"

            # Check bot membership in space
            try:
                members = await self.client.get_joined_members(current_ticket_space)
                is_member = self.client.mxid in members
                response += f"**Bot is member of space:** {is_member}\n"
                if not is_member:
                    response += "❌ WARNING: Bot is not a member of this space. Cannot set SPACE_PARENT/SPACE_CHILD events.\n"
            except Exception as e:
                response += f"**Error checking bot membership:** {e}\n"

            # Check bot power level in space
            try:
                power_levels = await self.client.get_state_event(current_ticket_space, EventType.ROOM_POWER_LEVELS)
                bot_level = power_levels.get("users", {}).get(self.client.mxid, power_levels.get("users_default", 0))
                response += f"**Bot power level in space:** {bot_level}\n"
                if bot_level < 50:
                    response += "⚠️ WARNING: Bot has insufficient power level (< 50) to send state events in this space.\n"
            except Exception as e:
                response += f"**Error checking bot power level:** {e}\n"

            # Check existing SPACE_CHILD events in space (list some)
            try:
                space_state = await self.client.get_state(current_ticket_space)
                space_child_events = [ev for ev in space_state if ev.type == EventType.SPACE_CHILD]
                response += f"**Number of SPACE_CHILD events in space:** {len(space_child_events)}\n"
                if space_child_events:
                    response += "**Sample SPACE_CHILD events (first 5):**\n"
                    for ev in space_child_events[:5]:
                        response += f"- State key: `{ev.state_key}`, Content: `{json.dumps(ev.content) if isinstance(ev.content, dict) else str(ev.content)}`\n"
                    if len(space_child_events) > 5:
                        response += f"- ... and {len(space_child_events) - 5} more\n"
                response += "\n"
            except Exception as e:
                response += f"**Error checking SPACE_CHILD events:** {e}\n"

            # Check server name extraction
            mxid_parts = self.client.mxid.split(":")
            server_name = mxid_parts[1] if len(mxid_parts) >= 2 else ""
            response += f"**Bot server name (for via field):** `{server_name}`\n"
            if not server_name:
                response += "❌ ERROR: Cannot extract server name from bot MXID.\n"

            # Test SPACE_PARENT event format
            if server_name:
                test_content = {"via": [server_name]}
                response += f"**SPACE_PARENT content example:** `{json.dumps(test_content)}`\n"

            response += "\n**Recent tickets from this New Ticket Notification room (last 5):**\n"
            # Get tickets for this intake room's category
            if category_id:
                category_tickets = self.db.search_tickets(category_id=category_id, limit=5)
                for ticket in category_tickets:
                    room_id = ticket.get("ticket_room_id", "unknown")
                    status = ticket.get("status", "unknown")
                    response += f"- Ticket {ticket.get('ticket_number', 'unknown')} (status: {status}): `{room_id}`\n"
                    # Check SPACE_PARENT event for this ticket room
                    try:
                        space_parent = await self.client.get_state_event(room_id, EventType.SPACE_PARENT, state_key=current_ticket_space)
                        if space_parent and space_parent.get("via"):
                            response += f"  ✅ Has SPACE_PARENT to this space (via: {space_parent.get('via')})\n"
                        elif space_parent:
                            response += f"  ⚠️ Has SPACE_PARENT but empty via field (room not in space)\n"
                        else:
                            response += f"  ❌ No SPACE_PARENT to this space\n"
                    except Exception as e:
                        if "M_NOT_FOUND" in str(e):
                            response += f"  ❌ No SPACE_PARENT event found\n"
                        else:
                            response += f"  ❓ Error checking SPACE_PARENT: {e}\n"
            else:
                response += "No category associated with this New Ticket Notification room.\n"

            await evt.reply(response)
            return
        elif action_lower == "fix":
            # Fix SPACE_PARENT events for all open/in_progress tickets from this intake room's category
            current_ticket_space = intake_room.get("space_id")
            if not current_ticket_space:
                await evt.reply("❌ No space configured for new ticket rooms from this New Ticket Notification room. Use `!ticket ntn_room tris set` first.")
                return

            category_id = intake_room.get("category_id")
            if not category_id:
                await evt.reply("❌ This New Ticket Notification room has no category associated. Cannot identify which tickets to fix.")
                return

            await evt.reply(f"🔧 Fixing SPACE_PARENT events for all open/in_progress tickets in category {category_id} to space `{current_ticket_space}`...")

            # Get all open and in_progress tickets for this category
            open_tickets = self.db.search_tickets(status="open", category_id=category_id)
            in_progress_tickets = self.db.search_tickets(status="in_progress", category_id=category_id)
            all_tickets = open_tickets + in_progress_tickets

            if not all_tickets:
                await evt.reply(f"No open or in_progress tickets found for category {category_id}.")
                return

            fixed_count = 0
            error_count = 0

            for ticket in all_tickets:
                room_id = ticket.get("ticket_room_id")
                ticket_id = ticket.get("id")
                ticket_number = ticket.get("ticket_number", "unknown")
                if not room_id:
                    continue

                # Determine which space to use for this ticket
                # 1. Use ticket's stored ticket_space_id if available
                # 2. Otherwise use current intake room's space
                target_space_id = ticket.get("ticket_space_id")
                using_stored_space = True
                if not target_space_id:
                    target_space_id = current_ticket_space
                    using_stored_space = False
                    self.log.info(f"Ticket {ticket_number} has no stored space, using intake room space {target_space_id}")
                else:
                    self.log.info(f"Ticket {ticket_number} has stored space {target_space_id}")

                if not target_space_id:
                    self.log.warning(f"Ticket {ticket_number} has no target space, skipping")
                    continue

                try:
                    # Check current SPACE_PARENT event to target space
                    try:
                        existing = await self.client.get_state_event(room_id, EventType.SPACE_PARENT, state_key=target_space_id)

                        # Extract via field
                        existing_via = None
                        if isinstance(existing, dict):
                            existing_via = existing.get("via")
                        elif hasattr(existing, "via"):
                            existing_via = existing.via
                        elif hasattr(existing, "get"):
                            existing_via = existing.get("via")

                        if existing_via:
                            self.log.info(f"Ticket room {room_id} already has valid SPACE_PARENT to {target_space_id} with via: {existing_via}")
                            # Update ticket_space_id if not set
                            if not using_stored_space and ticket_id:
                                self.db.update_ticket_space_id(ticket_id, target_space_id)
                                self.log.info(f"Updated ticket {ticket_number} to store space {target_space_id}")
                            continue

                        # Missing or empty via, need to fix
                        self.log.info(f"Fixing SPACE_PARENT for ticket room {room_id} (ticket: {ticket_number}) to space {target_space_id}")
                    except Exception as e:
                        error_str = str(e)
                        if "M_NOT_FOUND" in error_str or "Event not found" in error_str or "not found" in error_str.lower():
                            self.log.info(f"Creating SPACE_PARENT for ticket room {room_id} (ticket: {ticket_number}) to space {target_space_id} - no existing SPACE_PARENT event")
                        else:
                            self.log.warning(f"Error checking SPACE_PARENT for room {room_id} to space {target_space_id}: {e}")
                            error_count += 1
                            continue

                    # Set SPACE_PARENT with via
                    mxid_parts = self.client.mxid.split(":")
                    if len(mxid_parts) < 2:
                        self.log.error(f"Invalid bot MXID format: {self.client.mxid}")
                        error_count += 1
                        continue

                    server_name = mxid_parts[1]
                    space_content = {"via": [server_name]}

                    self.log.info(f"Attempting to set SPACE_PARENT for room {room_id} to space {target_space_id} with via {server_name}")
                    await self.client.send_state_event(
                        room_id,
                        EventType.SPACE_PARENT,
                        space_content,
                        state_key=target_space_id
                    )

                    fixed_count += 1
                    self.log.info(f"Fixed SPACE_PARENT for ticket room {room_id} to space {target_space_id}")

                    # Update ticket_space_id if not already set
                    if not using_stored_space and ticket_id:
                        self.db.update_ticket_space_id(ticket_id, target_space_id)
                        self.log.info(f"Updated ticket {ticket_number} to store space {target_space_id}")

                except Exception as e:
                    self.log.error(f"Failed to fix SPACE_PARENT for room {room_id} to space {target_space_id}: {e}")
                    error_count += 1

            # Check bot power level in space
            try:
                power_levels = await self.client.get_state_event(current_ticket_space, EventType.ROOM_POWER_LEVELS)
                bot_level = power_levels.get("users", {}).get(self.client.mxid, power_levels.get("users_default", 0))
                power_level_warning = f"\n\n⚠️ **Bot power level in space:** {bot_level}"
                if bot_level < 50:
                    power_level_warning += f"\n❌ Bot has insufficient power level ({bot_level} < 50) to send SPACE_CHILD events.\n"
                    power_level_warning += "**Admin must grant bot power level ≥50 in the space.**\n"
                    power_level_warning += "In Element: Space settings → Roles & Permissions → Add bot with power level ≥50"
            except Exception as e:
                power_level_warning = f"\n\n⚠️ Could not check bot power level: {e}"

            response = f"## SPACE_PARENT Fix Results\n\n"
            response += f"**Category:** {category_id}\n"
            response += f"**Space:** `{current_ticket_space}`\n"
            response += f"**Tickets processed:** {len(all_tickets)}\n"
            response += f"**SPACE_PARENT events fixed:** {fixed_count}\n"
            response += f"**Errors:** {error_count}\n"

            if fixed_count > 0:
                response += f"\n✅ Fixed SPACE_PARENT events for {fixed_count} ticket rooms.\n"
                response += "Ticket rooms should now appear in the space (if bot has sufficient power level for SPACE_CHILD).\n"

            response += power_level_warning

            response += f"\n**Next steps:**\n"
            response += "1. Grant bot power level ≥50 in the space (Element: Space settings → Roles & Permissions)\n"
            response += "2. New tickets will automatically be added to the space\n"
            response += "3. Use `!ticket ntn_room tris debug` to verify configuration\n"

            await evt.reply(response)
            return
        elif action_lower is None:
            # Show current ticket space configuration
            # Get all intake rooms with space_id set
            all_intake_rooms = self.db.get_all_intake_rooms()
            rooms_with_space = [r for r in all_intake_rooms if r.get("space_id") and r.get("enabled", True)]

            # Get space for this intake room (could be the room itself if it's a space, or its parent space)
            this_space = await self._get_space_for_room(evt.room_id)
            is_room_space = await self._is_space_room(evt.room_id)

            response = "## Ticket Space Configuration (Intake Room)\n\n"

            # Show this intake room's configured space
            this_intake_room_space = intake_room.get("space_id")
            this_room_has_space = any(r['room_id'] == evt.room_id for r in rooms_with_space)

            # Show category info
            category_id = intake_room.get("category_id")
            if category_id:
                category = self.db.get_category(category_id)
                category_name = category.get("name") if category else category_id
                response += f"**Category:** {category_name} (ID: {category_id})\n\n"

            if this_intake_room_space:
                response += f"**This New Ticket Notification room's configured space:** `{this_intake_room_space}`\n"
                if this_room_has_space:
                    response += "    This New Ticket Notification room's space is active and will be used for new tickets.\n"
                else:
                    response += "    Note: This New Ticket Notification room has a space configured but is not enabled.\n"
                if rooms_with_space:
                    room_names = [f"\"{r['name']}\" (`{r['room_id']}`)" for r in rooms_with_space]
                    response += f"Other New Ticket Notification rooms with spaces: {', '.join(room_names)}\n"
                    if len(rooms_with_space) > 1:
                        response += f"\n⚠️ **Note:** {len(rooms_with_space)} New Ticket Notification rooms have spaces configured.\n"
                        response += "Each New Ticket Notification room's space is used for tickets created in that room.\n"
                response += "\n"
            else:
                response += "**This New Ticket Notification room's configured space:** None\n"
                response += "    New tickets created here will NOT be added to any space.\n"
                if rooms_with_space:
                    response += f"\n⚠️ **Note:** {len(rooms_with_space)} other New Ticket Notification room(s) have spaces configured.\n"
                    response += "Tickets created in those rooms will be added to their respective spaces.\n"
                response += "\n"

            if this_space:
                if is_room_space:
                    response += f"**This New Ticket Notification room is a space:** `{this_space}`\n"
                    response += "You can set this space for new ticket rooms with: `!ticket ntn_room tris set`\n"
                else:
                    response += f"**This New Ticket Notification room is in space:** `{this_space}`\n"
                    response += "You can set this space for new ticket rooms with: `!ticket ntn_room tris set`\n"
            else:
                response += "**This New Ticket Notification room is not a space and not in a space.**\n"
                response += "**Options to set a space for new ticket rooms:**\n"
                response += "1. Add this room to a space (as a child room)\n"
                response += "2. Make this room a space (change room type to space)\n"
                response += "Then use: `!ticket ntn_room tris set`\n"

            response += "\nUse `!ticket ntn_room tris debug` for detailed diagnostics or `!ticket ntn_room tris fix` to repair SPACE_PARENT events."

            if this_intake_room_space:
                response += "\nTo detach new ticket rooms from spaces: `!ticket ntn_room tris unset` (or `unset all` to detach from all New Ticket Notification rooms)"

            await evt.reply(response)
            return
        else:
            await evt.reply("❌ Unknown action. Use `set`, `unset`, `unset all`, `debug`, `fix`, or no action to show current configuration.")

    @TicketsHandlerBase.ticket_command.subcommand("command_room", help="Manage command rooms (admin only)")
    async def command_room(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command room command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        await evt.reply(
            "## Command Room Commands (Admin only - power level 100):\n\n"
            "- `!ticket command_room add` - Mark this room as a command room\n"
            "- `!ticket command_room remove [all]` - Remove this room (or all) from command rooms\n"
            "- `!ticket command_room list` - List all command rooms\n"
            "- `!ticket command_room enable` - Enable this command room\n"
            "- `!ticket command_room disable` - Disable this command room\n"
        )

    @command_room.subcommand("add", help="Mark this room as a command room")
    async def admin_command_add(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command add command triggered: {evt.content.body}")
        self.log.info("Admin command add starting processing")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        await evt.reply("🔄 Processing admin command add command...")

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        # Check if room already registered as command room
        existing_by_room = self.db.get_command_room(evt.room_id)
        if existing_by_room:
            await evt.reply("❌ This room is already registered as a command room.")
            return

        # Check if room is already a New Ticket Notification room (prevent dual registration)
        existing_intake_room = self.db.get_intake_room(evt.room_id)
        if existing_intake_room:
            await evt.reply("❌ This room is already registered as a New Ticket Notification room. A room cannot be both a command room and a New Ticket Notification room.")
            return

        # Add command room (no category association)
        name = "Command Room"
        success = self.db.add_command_room(evt.room_id, name)
        if success:
            await evt.reply(
                f"✅ This room has been registered as a command room with name: {name}. "
                f"Staff-related ticket commands can be executed here."
            )
            # Send a test notification
            try:
                content = TextMessageEventContent(
                    msgtype=MessageType.TEXT,
                    body="🎫 **Test notification**: This room will allow ticket command execution for staff-related commands"
                )
                await self.client.send_message(evt.room_id, content)
            except Exception as e:
                self.log.error(f"Failed to send test notification: {e}")
                await evt.reply("⚠️ Note: I couldn't send a test message to this room. Make sure I have permission to send messages here.")
        else:
            await evt.reply("❌ Failed to register command room. It may already exist.")

    @command_room.subcommand("remove", help="Remove this room (or all) from command rooms")
    @command.argument("scope", label="scope", required=False)
    async def admin_command_remove(self, evt: MessageEvent, scope: Optional[str] = None) -> None:
        self.log.info(f"Admin command remove command triggered: {evt.content.body}")

        is_all_operation = scope and scope.lower() == "all"

        # For "all" operation, allow from any room type
        if not is_all_operation:
            # Block if in New Ticket Notification room (except allowed commands)
            if await self._block_if_ntn_room(evt):
                return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # For "all" operation, allow from ticket rooms too
        if not is_all_operation:
            # Ensure this is not a ticket room
            ticket = await self._get_ticket_for_room(evt.room_id)
            if ticket:
                await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
                return

        if not await self.ensure_admin(evt):
            return

        if is_all_operation:
            # Remove all command rooms
            removed_count = self.db.remove_all_command_rooms()
            await evt.reply(f"✅ Removed all command rooms ({removed_count} rooms removed).")
        else:
            # Remove only this room
            success = self.db.remove_command_room(evt.room_id)
            if success:
                await evt.reply("✅ This room has been removed from command rooms.")
            else:
                await evt.reply("❌ This room is not registered as a command room.")

    @command_room.subcommand("list", help="List all command rooms")
    async def admin_command_list(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command list command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        rooms = self.db.get_all_command_rooms()
        if not rooms:
            await evt.reply("No command rooms registered.")
            return

        response = "## Command Rooms:\n\n"
        for room in rooms:
            status = "✅ Enabled" if room["enabled"] else "❌ Disabled"
            response += f"- **{room['name']}** ({status})\n"
            response += f"  Room ID: `{room['room_id']}`\n"
            response += f"  Purpose: Staff ticket commands\n\n"

        await evt.reply(response)

    @command_room.subcommand("enable", help="Enable this command room")
    async def admin_command_enable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command enable command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        success = self.db.set_command_room_enabled(evt.room_id, True)
        if success:
            await evt.reply("✅ This command room has been enabled.")
        else:
            await evt.reply("❌ This room is not registered as a command room.")

    @command_room.subcommand("disable", help="Disable this command room")
    async def admin_command_disable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command disable command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return

        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return

        if not await self.ensure_admin(evt):
            return

        success = self.db.set_command_room_enabled(evt.room_id, False)
        if success:
            await evt.reply("✅ This command room has been disabled. Users cannot execute ticket commands here.")
        else:
            await evt.reply("❌ This room is not registered as a command room.")

    @TicketsHandlerBase.ticket_command.subcommand("category", help="Manage ticket categories (admin only)")
    async def category(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin category command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        await evt.reply(
            "## Category Commands (Admin only - power level 100):\n\n"
            "- `!ticket category add <category_id> <name> [description]` - Add a new category\n"
            "- `!ticket category remove <category_id>` - Remove a category\n"
            "- `!ticket category list` - List all categories\n"
        )

    @category.subcommand("add", help="Add a new category")
    @command.argument("details", label="category_id name [description]", required=True, pass_raw=True)
    async def admin_category_add(self, evt: MessageEvent, details: str) -> None:
        self.log.info(f"Admin category add command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return
        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return
        if not await self.ensure_admin(evt):
            return

        # Parse details: category_id name [description]
        parts = details.split(None, 2)
        if len(parts) < 2:
            await evt.reply("Please provide both category ID and name. Example: `!ticket category add tech Technical Issues`")
            return
        category_id = parts[0]
        name = parts[1]
        description = parts[2] if len(parts) > 2 else None

        # Reserve 'all' as special category ID for New Ticket Notification rooms
        if category_id == "all":
            await evt.reply("❌ Category ID 'all' is reserved for New Ticket Notification rooms that receive notifications for all categories.")
            return

        # Check if category already exists
        if self.db.category_exists(category_id):
            await evt.reply(f"❌ Category '{category_id}' already exists.")
            return
        # Add category
        success = self.db.add_category(category_id, name, description)
        if success:
            await evt.reply(f"✅ Category '{category_id}' added successfully.")
        else:
            await evt.reply(f"❌ Failed to add category '{category_id}'.")

    @category.subcommand("remove", help="Remove a category")
    @command.argument("category_id", label="category_id", required=True)
    async def admin_category_remove(self, evt: MessageEvent, category_id: str) -> None:
        self.log.info(f"Admin category remove command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return
        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return
        if not await self.ensure_admin(evt):
            return
        # Check if category exists
        if not self.db.category_exists(category_id):
            await evt.reply(f"❌ Category '{category_id}' does not exist.")
            return
        # Remove category
        success = self.db.remove_category(category_id)
        if success:
            await evt.reply(f"✅ Category '{category_id}' removed successfully.")
        else:
            await evt.reply(f"❌ Failed to remove category '{category_id}'.")

    @category.subcommand("list", help="List all categories")
    async def admin_category_list(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin category list command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return
        # Ensure this is not a ticket room
        ticket = await self._get_ticket_for_room(evt.room_id)
        if ticket:
            await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
            return
        if not await self.ensure_admin(evt):
            return
        categories = self.db.get_all_categories()
        if not categories:
            await evt.reply("No categories defined.")
            return
        response = "## Categories:\n\n"
        for cat in categories:
            status = "✅ Enabled" if cat["enabled"] else "❌ Disabled"
            response += f"- **{cat['name']}** (`{cat['category_id']}`) - {status}\n"
            if cat["description"]:
                response += f"  {cat['description']}\n"
            response += "\n"
        await evt.reply(response)

    @TicketsHandlerBase.ticket_command.subcommand("delete", help="Delete a ticket (admin only)")
    @command.argument("ticket_number", label="TICKET_ID", required=False)
    async def ticket_delete(self, evt: MessageEvent, ticket_number: Optional[str] = None) -> None:
        self.log.info(f"Ticket delete command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        ticket = None
        is_dm = await self._is_direct_message(evt)

        if ticket_number:
            # DM mode with ticket number provided
            if not is_dm:
                await evt.reply("❌ Ticket number can only be specified in a direct message with the bot.")
                return

            # Look up ticket by number
            ticket = self.db.get_ticket_by_number(ticket_number)
            if not ticket:
                await evt.reply(f"❌ Ticket {ticket_number} not found.")
                return

            # Check if user is admin in any command room
            if not await self._is_admin_in_any_command_room(evt.sender):
                await evt.reply("❌ You must be an administrator in any enabled command room to delete this ticket.")
                return
        else:
            # Ticket room mode (no ticket number specified)
            ticket = await self._ensure_ticket_room(evt)
            if not ticket:
                # Not in a ticket room, check if DM
                if is_dm:
                    await evt.reply("❌ Please specify a ticket number in direct messages: `!ticket delete TICKET-0001`")
                else:
                    await evt.reply("❌ This command can only be used in a ticket room or direct message with the bot.")
                return

            # Check admin permissions in current room
            if not await self.ensure_admin(evt):
                return

        # At this point we have a valid ticket and admin permissions
        ticket_id = ticket["id"]
        ticket_room_id = ticket["ticket_room_id"]
        ticket_num = ticket["ticket_number"]
        ticket_title = ticket["title"]

        # Delete ticket from database
        success = self.db.delete_ticket(ticket_id)
        if not success:
            await evt.reply("❌ Failed to delete ticket from database.")
            return

        # Notify intake room
        notification_msg = f"🗑️ Ticket **{ticket_num}** \"{ticket_title}\" deleted"
        await self._notify_ntn_room(ticket, notification_msg, mention_staff=False)

        # Try to leave/clean up the ticket room (optional)
        try:
            # We could leave the room, but deleting might require admin privileges
            # For now, just log
            self.log.info(f"Ticket {ticket_num} deleted, room {ticket_room_id} remains")
        except Exception as e:
            self.log.warning(f"Note: Could not clean up ticket room {ticket_room_id}: {e}")

        await evt.reply(f"✅ Ticket **{ticket_num}** has been deleted.")



