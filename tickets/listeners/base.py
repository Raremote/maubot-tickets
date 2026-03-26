from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from ..databases import Database


class TicketsHandlerBase:
    """Main handler for ticket system commands and events."""

    def __init__(self, db: Database, plugin: Plugin):
        self.db = db
        self.plugin = plugin
        self.client: MaubotMatrixClient = plugin.client
        self.log = plugin.log
        # Pending ticket creation state
        self.pending_tickets = {}  # key: (user_id, room_id) -> dict with title, description, intake_room_id, etc.
        self.pending_timeout = 30  # seconds
        # Invite attempt tracking to prevent rate limiting
        self.invite_attempts = {}  # key: (user_id, room_id) -> timestamp of last invite attempt
        self.invite_cooldown = 300  # seconds (5 minutes)
        # Global rate limiting tracking
        self.last_rate_limit_time = 0  # timestamp of last rate limit error
        self.rate_limit_cooldown = 30  # seconds (30 seconds)
        # Invite attempts cleanup
        self.invite_cleanup_age = 86400  # 24 hours
        self.invite_cleanup_threshold = 1000  # Max entries before cleanup

    def _get_room_link(self, room_id: str) -> str:
        """Get a clickable link for a room ID.

        Uses matrix.to service which provides clickable room links.
        """
        return f"https://matrix.to/#/{room_id}"

    def _format_ticket_card(self, ticket: dict, include_staff_mentions: bool = False, staff_users: Optional[List[str]] = None, footer_text: Optional[str] = None) -> tuple[str, Optional[str]]:
        """Format a ticket as a card with plain text and HTML versions.

        Args:
            ticket: Ticket dictionary from database
            include_staff_mentions: Whether to include staff mentions
            staff_users: List of staff user IDs to mention (or None)
            footer_text: Custom footer text (defaults to assign instruction)

        Returns:
            Tuple of (plain_text, html_text). html_text may be None if no HTML formatting.
        """
        staff_users = staff_users or []
        ticket_number = ticket["ticket_number"]
        title = ticket["title"]
        description = ticket["description"]
        creator = ticket["creator"]
        category_id = ticket.get("category_id", "Uncategorized")
        ticket_room_id = ticket["ticket_room_id"]
        status = ticket.get("status", "open")

        # Status emoji mapping
        status_emoji = {
            "open": "🟢",
            "in_progress": "🟡",
            "closed": "🔴",
            "resolved": "✅"
        }.get(status, "⚪")

        room_link = self._get_room_link(ticket_room_id)

        # Plain text version
        plain_text = (
            f"{status_emoji} **Ticket: {ticket_number}**\n\n"
            f"**Title:** {title}\n"
            f"**Description:** {description[:200]}...\n"
            f"**Creator:** {creator}\n"
            f"**Category:** {category_id}\n"
            f"**Status:** {status}\n"
            f"**Room:** {ticket_room_id}\n\n"
        )

        # HTML version
        html_text = (
            f"{status_emoji} <strong>Ticket: {ticket_number}</strong><br><br>"
            f"<strong>Title:</strong> {html.escape(title)}<br>"
            f"<strong>Description:</strong> {html.escape(description[:200])}...<br>"
            f"<strong>Creator:</strong> {html.escape(creator)}<br>"
            f"<strong>Category:</strong> {html.escape(category_id)}<br>"
            f"<strong>Status:</strong> {html.escape(status)}<br>"
            f"<strong>Room:</strong> <a href='{room_link}'>{html.escape(ticket_room_id)}</a><br><br>"
        )

        # Add staff mentions if requested
        if include_staff_mentions and staff_users:
            mention_text = " ".join(staff_users)
            plain_text += f"**Staff notified:** {mention_text}\n\n"
            mention_html = " ".join(
                f'<a href="https://matrix.to/#/{user}">{html.escape(user)}</a>'
                for user in staff_users
            )
            html_text += f"<strong>Staff notified:</strong> {mention_html}<br><br>"

        # Add footer text
        if footer_text is None:
            footer_text = "Use `!ticket assign TICKET-XXXX @user` in any enabled command room to assign staff."
        plain_text += footer_text
        if footer_text:
            html_text += f"<br>{html.escape(footer_text)}"

        return plain_text, html_text

    def _has_recent_invite_attempt(self, user_id: str, room_id: str) -> bool:
        """Check if there's been a recent invite attempt for this user/room."""
        key = (user_id, room_id)
        if key not in self.invite_attempts:
            return False
        elapsed = time.time() - self.invite_attempts[key]
        return elapsed < self.invite_cooldown

    def _cleanup_old_invite_attempts(self) -> None:
        """Remove old invite attempt entries to prevent memory growth."""
        if len(self.invite_attempts) <= self.invite_cleanup_threshold:
            return
        now = time.time()
        to_delete = []
        for key, timestamp in self.invite_attempts.items():
            if now - timestamp > self.invite_cleanup_age:
                to_delete.append(key)
        for key in to_delete:
            del self.invite_attempts[key]
        if to_delete:
            self.log.debug(f"Cleaned up {len(to_delete)} old invite attempts")

    def _record_invite_attempt(self, user_id: str, room_id: str) -> None:
        """Record an invite attempt timestamp."""
        self._cleanup_old_invite_attempts()
        self.invite_attempts[(user_id, room_id)] = time.time()

    async def _attempt_invite_with_backoff(self, user_id: str, room_id: str) -> bool:
        """Attempt to invite user to room with rate limiting and backoff."""
        # Check global rate limiting
        if time.time() - self.last_rate_limit_time < self.rate_limit_cooldown:
            self.log.debug(f"Skipping invite due to global rate limit cooldown")
            return False

        # Check if we recently attempted an invite for this specific room
        if self._has_recent_invite_attempt(user_id, room_id):
            self.log.debug(f"Skipping invite for {user_id} to {room_id} - recent attempt")
            return False

        try:
            await self.client.invite_user(room_id, user_id)
            self._record_invite_attempt(user_id, room_id)
            self.log.info(f"Invited {user_id} to room {room_id}")
            return True
        except MLimitExceeded as e:
            # Matrix server rate limiting - set global cooldown
            self.last_rate_limit_time = time.time()
            self.log.warning(f"Rate limited inviting {user_id} to {room_id}: {e}")
            self._record_invite_attempt(user_id, room_id)  # Record attempt even if failed due to rate limiting
            return False
        except MForbidden as e:
            # Bot lacks permission to invite users to this room
            self.log.warning(f"Bot lacks permission to invite {user_id} to {room_id}: {e}")
            self._record_invite_attempt(user_id, room_id)  # Record attempt to prevent repeated permission errors
            return False
        except MatrixBadRequest as e:
            # Invalid user ID or room ID
            self.log.warning(f"Invalid user/room ID inviting {user_id} to {room_id}: {e}")
            return False
        except MNotFound as e:
            # Room not found or user not found
            self.log.warning(f"Room or user not found inviting {user_id} to {room_id}: {e}")
            return False
        except Exception as e:
            # Other errors (user already invited, network errors, etc.)
            self.log.warning(f"Failed to invite {user_id} to {room_id}: {e}")
            return False

    def _get_pending_key(self, evt: MessageEvent) -> tuple:
        """Get key for pending tickets dict from event."""
        return (evt.sender, evt.room_id)

    def _set_pending_ticket(self, evt: MessageEvent, data: dict) -> None:
        """Store pending ticket data for user."""
        key = self._get_pending_key(evt)
        data["timestamp"] = time.time()
        self.pending_tickets[key] = data

    def _get_pending_ticket(self, evt: MessageEvent) -> Optional[dict]:
        """Get pending ticket data for user, cleaning up expired entries first."""
        self._cleanup_pending_tickets()
        key = self._get_pending_key(evt)
        return self.pending_tickets.get(key)

    def _clear_pending_ticket(self, evt: MessageEvent) -> None:
        """Clear pending ticket data for user."""
        key = self._get_pending_key(evt)
        self.pending_tickets.pop(key, None)

    def _cleanup_pending_tickets(self) -> None:
        """Remove expired pending tickets."""
        now = time.time()
        expired = []
        for key, data in self.pending_tickets.items():
            if now - data["timestamp"] > self.pending_timeout:
                expired.append(key)
        for key in expired:
            del self.pending_tickets[key]
        if expired:
            self.log.info(f"Cleaned up {len(expired)} expired pending tickets")

    async def has_admin_power(self, evt: MessageEvent) -> bool:
        """Check if user has admin power (power level >= 100)."""
        level = await self._get_user_power_level(evt.sender, evt.room_id)
        is_admin = level >= 100
        self.log.info(f"Power level check: user {evt.sender} has level {level} in room {evt.room_id} -> {'admin' if is_admin else 'not admin'}")
        return is_admin

    async def has_moderator_power(self, evt: MessageEvent) -> bool:
        """Check if user has moderator power (power level >= 50)."""
        level = await self._get_user_power_level(evt.sender, evt.room_id)
        is_moderator = level >= 50
        self.log.info(f"Moderator power check: user {evt.sender} has level {level} in room {evt.room_id} -> {'moderator' if is_moderator else 'not moderator'}")
        return is_moderator

    async def ensure_moderator(self, evt: MessageEvent) -> bool:
        if not await self.has_moderator_power(evt):
            await evt.reply("You must have power level 50 (moderator) or higher to use this command.")
            return False
        return True

    async def ensure_admin(self, evt: MessageEvent) -> bool:
        if not await self.has_admin_power(evt):
            await evt.reply("You must have power level 100 (administrator) to use this command.")
            return False
        return True

    async def _get_all_space_parent_details(self, room_id: str) -> List[tuple[str, dict, bool]]:
        """Get detailed information about all parent spaces.

        Returns:
            List of tuples (space_id, content_dict, has_valid_via)
            - space_id: parent space room ID
            - content_dict: the SPACE_PARENT content dict
            - has_valid_via: True if SPACE_PARENT has non-empty via list
        """
        results = []
        try:
            states = await self.client.get_state(room_id)
            self.log.info(f"Retrieved {len(states)} state events for room {room_id}")
            # Log all state event types for debugging
            for idx, state in enumerate(states):
                self.log.debug(f"State {idx}: type={state.type}, state_key={state.state_key}")

            for state in states:
                if state.type == EventType.SPACE_PARENT:
                    self.log.warning(f"FOUND SPACE_PARENT for room {room_id}: state_key={state.state_key}, type={type(state.content)}")
                    # Check if content has via (room is actually in space)
                    content = state.content
                    # Try to get content as dict
                    if hasattr(content, 'serialize'):
                        content_dict = content.serialize()
                    elif isinstance(content, dict):
                        content_dict = content
                    else:
                        content_dict = {}
                        self.log.warning(f"SPACE_PARENT content is not dict or serializable: {type(content)}")

                    self.log.warning(f"SPACE_PARENT content for room {room_id} -> space {state.state_key}: {content_dict}")

                    # Check if content has via field (even if empty list)
                    has_valid_via = False
                    if content_dict and "via" in content_dict:
                        via = content_dict.get("via")
                        if via:  # Non-empty list
                            self.log.warning(f"VALID parent space {state.state_key} for room {room_id} with via {via}")
                            has_valid_via = True
                        else:
                            self.log.warning(f"SPACE_PARENT for room {room_id} has empty via list ({via}), room may not be in space")
                    else:
                        self.log.warning(f"SPACE_PARENT for room {room_id} has no via field, room may not be in space")

                    results.append((state.state_key, content_dict, has_valid_via))
        except Exception as e:
            self.log.error(f"Error getting space parent for room {room_id}: {e}")

        if not results:
            self.log.warning(f"No SPACE_PARENT events found for room {room_id}")
        else:
            self.log.warning(f"Found {len(results)} SPACE_PARENT events for room {room_id}: {[state_key for state_key, _, _ in results]}")

        return results

    async def _get_space_parent_details(self, room_id: str) -> tuple[Optional[str], Optional[dict], bool]:
        """Get detailed information about the first valid parent space.

        Returns:
            (space_id, content_dict, has_valid_via)
            - space_id: parent space room ID if found, else None
            - content_dict: the SPACE_PARENT content dict if found, else None
            - has_valid_via: True if SPACE_PARENT exists and has non-empty via list
        """
        all_parents = await self._get_all_space_parent_details(room_id)
        # Return the first parent space with valid via, or first parent if none have valid via
        for space_id, content_dict, has_valid_via in all_parents:
            if has_valid_via:
                return space_id, content_dict, has_valid_via

        # If no parent with valid via, return the first one (if any)
        if all_parents:
            space_id, content_dict, has_valid_via = all_parents[0]
            return space_id, content_dict, has_valid_via

        return None, None, False

    async def _get_space_parent(self, room_id: str) -> Optional[str]:
        """Get the parent space room ID for a given room."""
        space_id, content, has_valid_via = await self._get_space_parent_details(room_id)
        if space_id:
            if not has_valid_via:
                self.log.warning(f"Room {room_id} has SPACE_PARENT to {space_id} but via list is empty or missing. "
                                 f"This may indicate the room is not actually in the space.")
            return space_id
        return None

    async def _does_space_contain_room(self, space_id: str, room_id: str) -> bool:
        """Check if a space contains a room by looking for SPACE_CHILD event."""
        try:
            # Get SPACE_CHILD event in the space pointing to the room
            event = await self.client.get_state_event(space_id, EventType.SPACE_CHILD, state_key=room_id)

            # Extract content
            if hasattr(event, 'serialize'):
                content = event.serialize()
            elif isinstance(event, dict):
                content = event
            else:
                content = {}
                self.log.warning(f"SPACE_CHILD content is not dict or serializable: {type(event)}")

            self.log.info(f"SPACE_CHILD event in space {space_id} for room {room_id}: {content}")

            # SPACE_CHILD event exists - room is in the space
            # Note: via field may be empty for local rooms, but event existence is authoritative
            self.log.info(f"Space {space_id} contains room {room_id} (SPACE_CHILD event exists)")
            return True
        except Exception as e:
            if "M_NOT_FOUND" in str(e):
                self.log.info(f"Space {space_id} does not have SPACE_CHILD to room {room_id} (not found)")
            else:
                self.log.warning(f"Error checking SPACE_CHILD in space {space_id} for room {room_id}: {e}")
            return False

    async def _find_subspace_containing_room(self, parent_space_id: str, room_id: str) -> Optional[str]:
        """Find a subspace of parent_space_id that contains the room."""
        try:
            # Get all SPACE_CHILD events in parent space
            space_state = await self.client.get_state(parent_space_id)
            space_child_events = [ev for ev in space_state if ev.type == EventType.SPACE_CHILD]

            self.log.warning(f"Parent space {parent_space_id} has {len(space_child_events)} SPACE_CHILD events")

            # Check each child that is a space (subspace)
            for ev in space_child_events:
                child_room_id = ev.state_key
                # Check if child is a space
                if await self._is_space_room(child_room_id):
                    self.log.warning(f"Checking subspace {child_room_id} for room {room_id}")
                    # Check if this subspace contains the room
                    if await self._does_space_contain_room(child_room_id, room_id):
                        self.log.warning(f"Found subspace {child_room_id} containing room {room_id}")
                        return child_room_id
        except Exception as e:
            self.log.warning(f"Error finding subspace in {parent_space_id} for room {room_id}: {e}")
        return None

    async def _get_best_space_parent(self, room_id: str) -> Optional[str]:
        """Get the most appropriate parent space ID for a room.

        Returns the immediate subspace if the room is in multiple spaces
        (e.g., both a subspace and its parent space).
        """
        all_parents = await self._get_all_space_parent_details(room_id)
        if not all_parents:
            self.log.warning(f"No parent spaces found for room {room_id}")
            return None

        # First pass: parents with valid via in SPACE_PARENT
        valid_parents = [(space_id, content, has_via) for space_id, content, has_via in all_parents if has_via]

        # If no parents with valid via in SPACE_PARENT, check SPACE_CHILD as fallback
        if not valid_parents:
            self.log.warning(f"Room {room_id} has {len(all_parents)} parent spaces but none have valid via field in SPACE_PARENT")
            self.log.warning(f"Checking SPACE_CHILD events as fallback...")

            # Check each candidate space for SPACE_CHILD to this room
            verified_parents = []
            for space_id, content, has_via in all_parents:
                if await self._does_space_contain_room(space_id, room_id):
                    verified_parents.append((space_id, content, True))  # Treat as valid via
                    self.log.warning(f"Space {space_id} verified via SPACE_CHILD check")
                else:
                    self.log.warning(f"Space {space_id} not verified via SPACE_CHILD check")

            if verified_parents:
                self.log.warning(f"Found {len(verified_parents)} parent spaces via SPACE_CHILD check")
                valid_parents = verified_parents
            else:
                self.log.warning(f"No parent spaces verified via SPACE_CHILD check either")
                # Return first parent anyway (though room may not actually be in space)
                space_id, content, has_via = all_parents[0]
                # Try to find a subspace of this parent that contains the room
                subspace = await self._find_subspace_containing_room(space_id, room_id)
                if subspace:
                    self.log.warning(f"Found subspace {subspace} containing room {room_id}, using it instead of parent {space_id}")
                    return subspace
                self.log.warning(f"No subspace found, using parent space {space_id}")
                return space_id

        self.log.warning(f"Room {room_id} is in {len(valid_parents)} space(s): {[space_id for space_id, _, _ in valid_parents]}")

        # If only one valid parent, return it
        if len(valid_parents) == 1:
            space_id, content, has_via = valid_parents[0]
            self.log.warning(f"Room {room_id} has single parent space: {space_id}")
            # Check if there's a subspace of this parent that contains the room
            subspace = await self._find_subspace_containing_room(space_id, room_id)
            if subspace:
                self.log.warning(f"Found subspace {subspace} containing room {room_id}, using it instead of parent {space_id}")
                return subspace
            self.log.warning(f"No subspace found, using parent space {space_id}")
            return space_id

        # Multiple valid parent spaces - try to find the most specific (subspace)
        # Build a set of parent space IDs
        parent_ids = [space_id for space_id, _, _ in valid_parents]

        # Check if any parent space is a subspace of another parent space
        # This is expensive, so limit to checking first few
        subspace_candidates = []
        for space_id, content, has_via in valid_parents:
            self.log.warning(f"Checking if parent space {space_id} is a subspace of any other parent...")
            is_subspace = False
            reason = ""

            # Method 1: Check if this space has SPACE_PARENT to any other parent in the list
            try:
                # Get all SPACE_PARENT events for this parent space
                parent_space_parents = await self._get_all_space_parent_details(space_id)
                parent_space_valid_parents = [(pid, c, hv) for pid, c, hv in parent_space_parents if hv]
                parent_space_parent_ids = [pid for pid, _, has_via2 in parent_space_valid_parents]

                self.log.warning(f"Space {space_id} is in {len(parent_space_valid_parents)} space(s) via SPACE_PARENT: {parent_space_parent_ids}")

                # Check if any of this space's parents are in our parent_ids list
                intersects = set(parent_space_parent_ids) & set(parent_ids)
                if intersects:
                    is_subspace = True
                    reason = f"has SPACE_PARENT to {intersects}"
                    self.log.warning(f"Parent space {space_id} is a subspace of {intersects} (more specific) via SPACE_PARENT")
            except Exception as e:
                self.log.warning(f"Could not check SPACE_PARENT for space {space_id}: {e}")

            # Method 2: Check if any other parent has SPACE_CHILD to this space
            if not is_subspace:
                try:
                    # Get other parent IDs (excluding current space_id)
                    other_parent_ids = [pid for pid in parent_ids if pid != space_id]
                    self.log.warning(f"Checking if any of {len(other_parent_ids)} other parents have SPACE_CHILD to {space_id}...")

                    for other_parent_id in other_parent_ids:
                        if await self._does_space_contain_room(other_parent_id, space_id):
                            is_subspace = True
                            reason = f"{other_parent_id} has SPACE_CHILD to this space"
                            self.log.warning(f"Parent space {space_id} is a subspace of {other_parent_id} (more specific) via SPACE_CHILD")
                            break  # Found hierarchy, no need to check more
                except Exception as e:
                    self.log.warning(f"Could not check SPACE_CHILD for space {space_id}: {e}")

            if is_subspace:
                self.log.warning(f"Parent space {space_id} is a subspace of another parent ({reason}), adding to candidates")
                subspace_candidates.append(space_id)
            else:
                self.log.warning(f"Parent space {space_id} is not a subspace of any other parent in the list")

        # If we found subspaces, return the first one (most specific)
        if subspace_candidates:
                self.log.warning(f"Found {len(subspace_candidates)} subspace candidate(s): {subspace_candidates}")
                return subspace_candidates[0]

        # No hierarchy detected, check bot membership in each space and prefer spaces where bot is a member
        spaces_with_bot_membership = []
        for space_id, content, has_via in valid_parents:
            try:
                members = await self.client.get_joined_members(space_id)
                is_member = self.client.mxid in members
                if is_member:
                    spaces_with_bot_membership.append(space_id)
                    self.log.warning(f"Bot is a member of space {space_id}")
                else:
                    self.log.warning(f"Bot is NOT a member of space {space_id}")
            except Exception as e:
                self.log.warning(f"Could not check bot membership in space {space_id}: {e}")
                # Assume not a member

        # If bot is only a member of one space, use that one
        if len(spaces_with_bot_membership) == 1:
                self.log.warning(f"Bot is only a member of one space: {spaces_with_bot_membership[0]}, selecting it")
                return spaces_with_bot_membership[0]

        # Prefer spaces where bot is a member (multiple)
        if spaces_with_bot_membership:
                self.log.warning(f"Preferring spaces where bot is a member: {spaces_with_bot_membership}")
                return spaces_with_bot_membership[0]

        # No clear hierarchy, return the first valid parent
        space_id, content, has_via = valid_parents[0]
        self.log.warning(f"Could not determine hierarchy, using first parent space: {space_id}")
        self.log.warning(f"All valid parents: {parent_ids}")
        return space_id

    async def _is_space_room(self, room_id: str) -> bool:
        """Check if a room is a space (m.space type)."""
        try:
            event = await self.client.get_state_event(room_id, EventType.ROOM_CREATE)
            self.log.debug(f"Room create event raw: {event}")
            # Try to extract room type from various event representations
            if isinstance(event, dict):
                content = event
            else:
                # Try to serialize to dict
                try:
                    content = event.serialize()
                except Exception:
                    content = {}

            self.log.debug(f"Room create content dict: {content}")
            # Check if room is a space
            room_type = content.get("type", "")
            is_space = room_type == "m.space"
            self.log.info(f"Room {room_id} create event type: '{room_type}', is_space: {is_space}")
            return is_space
        except Exception as e:
            self.log.warning(f"Error checking if room {room_id} is a space: {e}")
            return False

    async def _get_space_for_room(self, room_id: str) -> Optional[str]:
        """Get the space ID for a room.

        Returns:
            - If room is a space: returns the room's own ID
            - If room is in a space: returns its parent space ID
            - Otherwise: returns None
        """
        # Check if room itself is a space
        is_space = await self._is_space_room(room_id)
        if is_space:
            self.log.info(f"Room {room_id} is a space, using it as ticket space")
            return room_id

        # Room is not a space, check for parent space(s)
        parent_space = await self._get_best_space_parent(room_id)
        if parent_space:
            self.log.info(f"Room {room_id} is in parent space {parent_space} (selected as best match)")
            return parent_space

        self.log.info(f"Room {room_id} is not a space and not in a space")
        return None

    async def _is_direct_room(self, room_id: str) -> bool:
        """Check if a room is a direct message room based on room create event."""
        try:
            self.log.info(f"Checking room create event for {room_id}")
            event = await self.client.get_state_event(room_id, EventType.ROOM_CREATE)
            # Try to extract is_direct from various event representations
            if isinstance(event, dict):
                content = event
                self.log.info(f"Room create event is dict: {content}")
            else:
                # Try to serialize to dict
                try:
                    content = event.serialize()
                    self.log.info(f"Room create event serialized: {content}")
                except Exception:
                    content = {}
                    self.log.info("Room create event could not be serialized")

            # Check if room is a space (spaces are not DMs)
            if content.get("type") == "m.space":
                self.log.info(f"Room {room_id} is a space, not DM")
                return False

            # Check if is_direct is set to true
            is_direct = content.get("is_direct")
            self.log.info(f"Room {room_id} is_direct flag: {is_direct}")
            if is_direct is True:
                self.log.info(f"Room {room_id} has is_direct=True, treating as DM")
                return True
            else:
                self.log.info(f"Room {room_id} does not have is_direct=True")
        except Exception as e:
            self.log.debug(f"Could not check if room {room_id} is direct: {e}")
        self.log.info(f"Room {room_id} not determined as DM from create event")
        return False

    async def _is_direct_message(self, evt: MessageEvent) -> bool:
        """Check if the event is in a direct message with the bot.

        Detection hierarchy:
         1. If room is ticket, New Ticket Notification, or command room → NOT DM
        2. If room has >2 members → NOT DM (group chat)
        3. If room has exactly 2 members (bot + sender):
           a. If user is admin (power level >= 100) → NOT DM (regular admin room)
           b. If user not admin:
              i. Check room create event for is_direct flag → DM if true
              ii. Check m.direct account data → DM if marked
              iii. Default to DM (safe assumption)
        4. Otherwise → NOT DM
        """
        try:
            self.log.info(f"Checking if room {evt.room_id} is DM for user {evt.sender}")
            # First check if this room is a ticket, New Ticket Notification, or command room - these are not DMs
            ticket = await self._get_ticket_for_room(evt.room_id)
            if ticket:
                self.log.info(f"Room {evt.room_id} is a ticket room, not DM")
                return False
            intake_room = self.db.get_intake_room(evt.room_id)
            if intake_room:
                self.log.info(f"Room {evt.room_id} is a New Ticket Notification room, not DM")
                return False
            command_room = self.db.get_command_room(evt.room_id)
            if command_room:
                self.log.info(f"Room {evt.room_id} is a command room, not DM")
                return False

            # Get member count first - most reliable indicator
            members = await self.client.get_joined_members(evt.room_id)
            member_count = len(members)
            self.log.info(f"Room {evt.room_id} has {member_count} members")

            # If more than 2 members, definitely not a DM (could be a group chat)
            if member_count > 2:
                self.log.info(f"Room {evt.room_id} has >2 members, treating as regular room")
                return False

            # If exactly 2 members and both bot and sender are present
            if member_count == 2 and self.client.mxid in members and evt.sender in members:
                # Room has only 2 members (bot and sender)
                # This could be a DM or a small regular room (e.g., command room being set up)
                # If the user is an admin in this room, it's likely a regular room for admin purposes
                is_admin = await self._is_admin_in_room(evt.sender, evt.room_id)
                self.log.info(f"User {evt.sender} admin status in room {evt.room_id}: {is_admin}")
                if is_admin:
                    self.log.info(f"User {evt.sender} is admin in room {evt.room_id}, treating as regular room")
                    return False
                # User is not admin, check other DM indicators
                self.log.info(f"User {evt.sender} is not admin, checking other DM indicators")

                # Check room create event for is_direct flag
                is_direct_room = await self._is_direct_room(evt.room_id)
                if is_direct_room:
                    self.log.info(f"Room {evt.room_id} has is_direct flag in create event, treating as DM")
                    return True

                # Check m.direct account data (official Matrix DM marker)
                try:
                    direct_data = await self.client.get_account_data(EventType.DIRECT)
                    if direct_data and isinstance(direct_data, dict):
                        # direct_data maps user_id -> list of room_ids
                        for user_id, room_ids in direct_data.items():
                            if evt.room_id in room_ids:
                                # Room is marked as a DM with some user
                                self.log.info(f"Room {evt.room_id} found in m.direct account data for user {user_id}, treating as DM")
                                return True
                except Exception as e:
                    self.log.debug(f"Could not get m.direct account data: {e}")

                # No clear DM indicators, but user is not admin - treat as DM for safety
                self.log.info(f"Room {evt.room_id} has 2 members, user not admin, no DM flags - treating as DM")
                return True
            else:
                # Room doesn't have exactly 2 members or bot/sender not both present
                # This shouldn't happen for rooms where bot can receive messages
                self.log.info(f"Room {evt.room_id} does not have exactly 2 members or bot/sender not both present, treating as regular room")
                return False
        except Exception as e:
            self.log.error(f"Error checking if room is DM: {e}")
        self.log.info(f"Room {evt.room_id} not determined to be DM, defaulting to False")
        return False

    async def _create_ticket_room(self, ticket_number: str, title: str, description: str, creator: str, parent_space_id: Optional[str] = None) -> str:
        """Create a dedicated room for a ticket."""
        room_name = f"{ticket_number}: {title}"
        room_topic = f"Ticket {ticket_number}: {description[:200]}..."

        # Create the room
        try:
            room_id = await self.client.create_room(
                name=room_name,
                topic=room_topic,
                visibility=RoomDirectoryVisibility.PRIVATE,
                preset=RoomCreatePreset.PRIVATE,
                invitees=[creator],
            )
            # Set room to knock (users can knock to join, auto-approved for creator and admins)
            # Try knock first, fall back to invite if not supported
            try:
                join_rules_content = {"join_rule": "knock"}
                self.log.debug(f"Join rules content JSON: {json.dumps(join_rules_content)}")
                await self.client.send_state_event(
                    room_id, EventType.ROOM_JOIN_RULES, join_rules_content
                )
                self.log.info(f"Successfully set join rule to 'knock' for room {room_id}")
            except Exception as e:
                self.log.warning(f"Failed to set join rule to 'knock' (might not be supported): {e}")
                self.log.info(f"Falling back to 'invite' join rule for room {room_id}")
                join_rules_content = {"join_rule": "invite"}
                await self.client.send_state_event(
                    room_id, EventType.ROOM_JOIN_RULES, join_rules_content
                )
            # Set history visibility
            history_visibility_content = {"history_visibility": "shared"}
            self.log.debug(f"History visibility content JSON: {json.dumps(history_visibility_content)}")
            await self.client.send_state_event(
                room_id, EventType.ROOM_HISTORY_VISIBILITY, history_visibility_content
            )

        except Exception as e:
            self.log.error(f"Failed to create room: {e}")
            raise

        # Set power levels using typed content object
        try:
            self.log.info(f"Setting power levels for room {room_id}, creator: {creator} (type: {type(creator)})")
            # Ensure creator is a string for JSON serialization
            creator_str = str(creator)

            # Create typed power levels content
            power_levels = PowerLevelStateEventContent(
                users={},  # No special user powers by default
                users_default=0,
                events={
                    EventType.ROOM_NAME: 50,
                    EventType.ROOM_TOPIC: 50,
                    EventType.ROOM_POWER_LEVELS: 100,
                },
                events_default=0,
                state_default=50,
                invite=50,
                kick=50,
                ban=50,
                redact=50,
            )

            # Log the content for debugging
            self.log.info(f"Power levels content object created")
            try:
                # Try to serialize to JSON to ensure it's valid
                power_levels_dict = power_levels.serialize()
                self.log.debug(f"Power levels serialized dict: {power_levels_dict}")
                self.log.debug(f"Power levels JSON: {json.dumps(power_levels_dict)}")
            except Exception as e:
                self.log.error(f"Failed to serialize power levels: {e}")

            await self.client.send_state_event(
                room_id,
                EventType.ROOM_POWER_LEVELS,
                power_levels.serialize()
            )
            self.log.info(f"Successfully set power levels for room {room_id}")
        except Exception as e:
            self.log.error(f"Failed to set power levels: {e}")
            # Continue anyway

        # Add to parent space if specified
        if parent_space_id:
            try:
                self.log.info(f"Attempting to add ticket room {room_id} to space {parent_space_id}")

                # Verify parent_space_id is actually a space
                try:
                    is_space = await self._is_space_room(parent_space_id)
                    if not is_space:
                        self.log.error(f"Room {parent_space_id} is not a space (type not m.space). Cannot add ticket room to non-space.")
                        # Try to get room create event for debugging
                        try:
                            create_event = await self.client.get_state_event(parent_space_id, EventType.ROOM_CREATE)
                            self.log.error(f"Room {parent_space_id} create event: {create_event}")
                        except Exception as e2:
                            self.log.error(f"Could not get create event for room {parent_space_id}: {e2}")
                        return room_id
                    self.log.info(f"Room {parent_space_id} is a valid space")
                except Exception as e:
                    self.log.warning(f"Could not verify if room {parent_space_id} is a space: {e}")
                    # Continue anyway, but log warning

                # Check if bot is a member of the space
                try:
                    members = await self.client.get_joined_members(parent_space_id)
                    if self.client.mxid not in members:
                        self.log.warning(f"Bot is not a member of space {parent_space_id}, cannot set SPACE_PARENT")
                        return room_id
                    self.log.info(f"Bot is a member of space {parent_space_id}")
                except Exception as e:
                    self.log.warning(f"Could not check bot membership in space {parent_space_id}: {e}")
                    # Continue anyway, but SPACE_CHILD may fail

                mxid_parts = self.client.mxid.split(":")
                if len(mxid_parts) < 2:
                    self.log.error(f"Invalid bot MXID format: {self.client.mxid}")
                    server_name = ""
                else:
                    server_name = mxid_parts[1]

                if not server_name:
                    self.log.error(f"Cannot determine server name from MXID {self.client.mxid}, skipping space addition")
                    return room_id

                self.log.info(f"Bot MXID: {self.client.mxid}, extracted server name: {server_name}")

                self.log.info(f"Adding room {room_id} to space {parent_space_id} with via {server_name} (bot MXID: {self.client.mxid})")
                space_content = {"via": [server_name]}
                self.log.info(f"SPACE_PARENT content to send: {json.dumps(space_content)}, space ID: {parent_space_id}, ticket room ID: {room_id}")

                # Try to add SPACE_PARENT event in ticket room
                try:
                    await self.client.send_state_event(
                        room_id,
                        EventType.SPACE_PARENT,
                        space_content,
                        state_key=parent_space_id
                    )
                    self.log.info(f"SPACE_PARENT event sent to ticket room {room_id} for space {parent_space_id}")

                    # Verify the event was actually set
                    try:
                        verify_event = await self.client.get_state_event(room_id, EventType.SPACE_PARENT, state_key=parent_space_id)
                        self.log.info(f"Verified SPACE_PARENT event exists in room {room_id}: {verify_event}")
                        if verify_event.get("via") != [server_name]:
                            self.log.warning(f"SPACE_PARENT via field mismatch: expected {[server_name]}, got {verify_event.get('via')}")
                    except Exception as verify_error:
                        self.log.error(f"Failed to verify SPACE_PARENT event was set: {verify_error}")

                except Exception as e:
                    self.log.error(f"Failed to add SPACE_PARENT event: {e}")
                    import traceback
                    self.log.error(f"Traceback: {traceback.format_exc()}")
                    # Continue anyway, maybe SPACE_CHILD will still work

                # Also try to add SPACE_CHILD event in the space (optional but recommended)
                try:
                    # Check bot's power level in space before attempting SPACE_CHILD
                    try:
                        power_levels = await self.client.get_state_event(parent_space_id, EventType.ROOM_POWER_LEVELS)
                        bot_level = power_levels.get("users", {}).get(self.client.mxid, power_levels.get("users_default", 0))
                        self.log.info(f"Bot power level in space {parent_space_id}: {bot_level}")
                        if bot_level < 50:
                            self.log.warning(f"Bot has insufficient power level ({bot_level}) to send state events in space {parent_space_id}")
                    except Exception as e:
                        self.log.warning(f"Could not check bot power level in space {parent_space_id}: {e}")

                    child_content = {"via": [server_name]}
                    await self.client.send_state_event(
                        parent_space_id,
                        EventType.SPACE_CHILD,
                        child_content,
                        state_key=room_id
                    )
                    self.log.info(f"Added SPACE_CHILD event in space {parent_space_id} for room {room_id}")
                except Exception as e:
                    self.log.warning(f"Could not add SPACE_CHILD event (may lack permissions or already exists): {e}")
                    import traceback
                    self.log.debug(f"SPACE_CHILD error traceback: {traceback.format_exc()}")

            except Exception as e:
                self.log.error(f"Failed to add ticket room to space {parent_space_id}: {e}")
                import traceback
                self.log.error(f"Traceback: {traceback.format_exc()}")

        return room_id
    async def _get_ticket_for_room(self, room_id: str):
        """Get ticket associated with a room."""
        return self.db.get_ticket_by_room(room_id)

    async def _ensure_ticket_room(self, evt: MessageEvent):
        """Ensure the current room is a ticket room and return ticket data."""
        ticket = await self._get_ticket_for_room(evt.room_id)
        if not ticket:
            await evt.reply("This command can only be used in a ticket room.")
            return None
        return ticket

    def _parse_assign_args(self, evt: MessageEvent) -> tuple[Optional[str], Optional[str]]:
        """Parse ticket number and user from assign/unassign command.

        Returns:
            (ticket_number, user) where ticket_number is required, user may be None
            if parsing fails or assign/unassign with no user (assign/unassign self).
            For unassign, special keyword "all" unassigns all assignees.
        """
        # Get raw command text
        body = evt.content.body.strip()
        # Split by spaces, ignoring extra spaces
        parts = body.split()
        # Determine which command we're parsing
        if len(parts) < 2:
            return None, None
        subcommand = parts[1]  # "assign" or "unassign"
        # Skip first two tokens: "!ticket" and "assign"/"unassign"
        args = parts[2:]

        ticket_pattern = r'^TICKET-\d+$'

        # Ticket number is always required
        if len(args) == 0:
            # No arguments, invalid
            return None, None
        # First argument must be ticket number
        if not re.match(ticket_pattern, args[0].upper()):
            # First arg not ticket number, invalid
            return None, None
        ticket_number = args[0].upper()

        # Determine user
        if len(args) >= 2:
            user = args[1]
            return ticket_number, user
        else:
            # No user specified - will assign/unassign self
            return ticket_number, None

    async def _can_manage_ticket(self, evt: MessageEvent, ticket: dict) -> bool:
        """Check if user can manage ticket (admin, moderator, or ticket creator)."""
        # Check if user is admin
        if await self.has_admin_power(evt):
            return True
        # Check if user is moderator
        if await self.has_moderator_power(evt):
            return True
        # Check if user is ticket creator
        if evt.sender == ticket["creator"]:
            return True
        return False

    async def _is_staff_in_ticket_room(self, evt: MessageEvent, ticket: dict) -> bool:
        """Check if user is staff (admin or moderator) in ticket room."""
        # Check if user is admin
        if await self.has_admin_power(evt):
            return True
        # Check if user is moderator
        if await self.has_moderator_power(evt):
            return True
        return False

    async def _update_power_levels_for_user(self, room_id: str, user_id: str, level: int = 50) -> bool:
        """Update power levels for a user in a room."""
        current = await self._get_power_levels_event(room_id)
        if current is None:
            # No power levels set yet, create default
            current = PowerLevelStateEventContent()

        # Update users dict
        if current.users is None:
            current.users = {}
        current.users[user_id] = level

        # Send updated power levels
        try:
            await self.client.send_state_event(room_id, EventType.ROOM_POWER_LEVELS, current.serialize())
            return True
        except Exception as e:
            self.log.error(f"Error updating power levels: {e}")
            return False

    async def _get_power_levels_event(self, room_id: str) -> Optional[PowerLevelStateEventContent]:
        """Get power levels event for a room, or None if not set."""
        try:
            event = await self.client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
            if isinstance(event, dict):
                return PowerLevelStateEventContent.deserialize(event)
            elif isinstance(event, PowerLevelStateEventContent):
                return event
            else:
                self.log.error(f"Unexpected power levels type: {type(event)}")
                return None
        except MNotFound:
            # No power levels set yet
            return None
        except Exception as e:
            self.log.error(f"Error getting power levels for room {room_id}: {e}")
            return None

    async def _get_room_create_sender(self, room_id: str) -> Optional[str]:
        """Get the sender (creator) of the room create event, or None if not found."""
        try:
            event = await self.client.get_state_event(room_id, EventType.ROOM_CREATE)
            # Try to extract sender/creator from various event representations
            if isinstance(event, dict):
                # Check for sender field (creator of event)
                if "sender" in event:
                    return event["sender"]
                # Fallback to creator in content
                if "content" in event and isinstance(event["content"], dict):
                    return event["content"].get("creator")
                return event.get("creator")

            # Try to get sender attribute (event sender is the room creator)
            try:
                if hasattr(event, 'sender'):
                    return event.sender
            except Exception:
                pass

            # Try to get creator attribute from content
            try:
                if hasattr(event, 'content') and hasattr(event.content, 'creator'):
                    return event.content.creator
            except Exception:
                pass

            # Try to serialize to dict and get sender/creator
            try:
                serialized = event.serialize()
                if isinstance(serialized, dict):
                    # Check sender field first
                    if "sender" in serialized:
                        return serialized["sender"]
                    # Check content.creator
                    if "content" in serialized and isinstance(serialized["content"], dict):
                        return serialized["content"].get("creator")
                    return serialized.get("creator")
            except Exception:
                pass

            # Log unexpected type but don't error
            self.log.warning(f"Could not extract creator from room create event type: {type(event)}")
            return None
        except MNotFound:
            # No room create event (should not happen)
            return None
        except Exception as e:
            self.log.error(f"Error getting room create event for room {room_id}: {e}")
            return None

    async def _get_user_power_level(self, user_id: str, room_id: str) -> int:
        """Get user's power level in a room (0-100). Defaults to 0."""
        # Check if user is room creator (implicit power level 100)
        creator = await self._get_room_create_sender(room_id)
        if creator == user_id:
            return 100

        # Get power levels event
        power_levels = await self._get_power_levels_event(room_id)
        if power_levels is None:
            # No custom power levels, default is 0
            return 0

        # Get user's level from power levels
        if power_levels.users and user_id in power_levels.users:
            return power_levels.users[user_id]

        # Check if there's a users_default
        if power_levels.users_default is not None:
            return power_levels.users_default

        # Default power level for users is 0
        return 0

    async def _is_user_member_of_room(self, user_id: str, room_id: str) -> bool:
        """Check if user is currently joined to a room."""
        membership = await self._get_user_membership(user_id, room_id)
        return membership == Membership.JOIN

    async def _get_user_membership(self, user_id: str, room_id: str) -> Optional[Membership]:
        """Get user's membership state in a room."""
        try:
            member = await self.client.get_room_member(room_id, user_id)
            return member.membership
        except MLimitExceeded as e:
            # Matrix server rate limiting
            self.last_rate_limit_time = time.time()
            self.log.warning(f"Rate limited getting membership for {user_id} in {room_id}: {e}")
            return None
        except MForbidden as e:
            # Bot lacks permission to read room membership
            self.log.warning(f"Bot lacks permission to get membership for {user_id} in {room_id}: {e}")
            return None
        except MatrixBadRequest as e:
            # Invalid user ID or room ID
            self.log.warning(f"Invalid user/room ID getting membership for {user_id} in {room_id}: {e}")
            return None
        except MNotFound as e:
            # Room not found or user not found
            self.log.debug(f"Room or user not found getting membership for {user_id} in {room_id}: {e}")
            return None
        except Exception as e:
            # Other errors (network errors, etc.)
            self.log.debug(f"Failed to get membership for {user_id} in room {room_id}: {e}")
            return None

    def _format_user_mention(self, user_id: str) -> tuple[str, str]:
        """Format user mention as plain text and HTML.
        Returns: (plain_text, html_text)
        """
        plain = user_id  # MXID already includes @
        html_text = f'<a href="https://matrix.to/#/{user_id}">{html.escape(user_id)}</a>'
        return plain, html_text

    async def _update_room_topic_for_ticket(self, ticket: dict, status: str) -> bool:
        """Update room topic to reflect ticket status."""
        try:
            room_id = ticket["ticket_room_id"]
            ticket_number = ticket["ticket_number"]
            description = ticket["description"]
            # Same truncation as in _create_ticket_room
            truncated = description[:200] + "..." if len(description) > 200 else description
            topic = f"Ticket {ticket_number} [{status}]: {truncated}"
            await self.client.send_state_event(room_id, EventType.ROOM_TOPIC, {"topic": topic})
            return True
        except Exception as e:
            self.log.error(f"Failed to update room topic: {e}")
            return False

    async def _update_ticket_space_visibility(self, ticket: dict, new_status: str) -> bool:
        """Update space parent visibility based on ticket status.

        Ticket rooms should be visible in parent space only when status is 'open' or 'in_progress'.
        When status is 'closed' or 'resolved', remove the room from the space.
        """
        room_id = ticket["ticket_room_id"]
        intake_room_id = ticket["intake_room_id"]

        # Get ticket space ID (prefer stored ticket_space_id, then space from New Ticket Notification room)
        parent_space_id = ticket.get("ticket_space_id")
        if parent_space_id:
            self.log.info(f"Using stored ticket space {parent_space_id} from ticket record, ticket status: {new_status}")
        else:
            # Try to get space from New Ticket Notification room
            parent_space_id = self.db.get_intake_room_space(intake_room_id)
            if parent_space_id:
                self.log.info(f"Using configured ticket space {parent_space_id} from New Ticket Notification room {intake_room_id} (no space stored in ticket), ticket status: {new_status}")
            else:
                self.log.debug(f"No ticket space configured, skipping space visibility update")
                return False

        try:
            if new_status in ("open", "in_progress"):
                # Ensure ticket room is added to parent space
                # Check if already has SPACE_PARENT event
                try:
                    existing = await self.client.get_state_event(room_id, EventType.SPACE_PARENT, state_key=parent_space_id)
                    self.log.debug(f"Ticket room {room_id} already has SPACE_PARENT to {parent_space_id}, content: {existing}, type: {type(existing)}")

                    # Extract via field from existing event (could be dict or object)
                    existing_via = None
                    if isinstance(existing, dict):
                        existing_via = existing.get("via")
                    elif hasattr(existing, "via"):
                        existing_via = existing.via
                    elif hasattr(existing, "get"):
                        existing_via = existing.get("via")

                    self.log.debug(f"Existing SPACE_PARENT via field: {existing_via}")

                    # Check if content is empty (room removed from space)
                    if not existing or not existing_via:
                        # SPACE_PARENT exists but content empty or missing via, need to add via
                        mxid_parts = self.client.mxid.split(":")
                        if len(mxid_parts) < 2:
                            self.log.error(f"Invalid bot MXID format: {self.client.mxid}")
                            return False
                        server_name = mxid_parts[1]
                        space_content = {"via": [server_name]}
                        await self.client.send_state_event(
                            room_id,
                            EventType.SPACE_PARENT,
                            space_content,
                            state_key=parent_space_id
                        )
                        self.log.info(f"Updated ticket room {room_id} SPACE_PARENT content with via (status: {new_status})")
                        return True
                    # Already has proper SPACE_PARENT with via
                    self.log.debug(f"Ticket room {room_id} already has valid SPACE_PARENT with via: {existing_via}")
                    return True
                except MNotFound:
                    # SPACE_PARENT not found, add it
                    mxid_parts = self.client.mxid.split(":")
                    if len(mxid_parts) < 2:
                        self.log.error(f"Invalid bot MXID format: {self.client.mxid}")
                        return False
                    server_name = mxid_parts[1]
                    space_content = {"via": [server_name]}
                    await self.client.send_state_event(
                        room_id,
                        EventType.SPACE_PARENT,
                        space_content,
                        state_key=parent_space_id
                    )
                    self.log.info(f"Added ticket room {room_id} to space {parent_space_id} (status: {new_status})")
                    return True
                except Exception as e:
                    self.log.error(f"Error checking SPACE_PARENT for room {room_id}: {e}")
                    return False
            else:
                # Status is closed or resolved - remove from space
                try:
                    # Check if SPACE_PARENT event exists
                    existing = await self.client.get_state_event(room_id, EventType.SPACE_PARENT, state_key=parent_space_id)
                    self.log.debug(f"Checking SPACE_PARENT for removal: {existing}, type: {type(existing)}")

                    # Extract via field from existing event
                    existing_via = None
                    if isinstance(existing, dict):
                        existing_via = existing.get("via")
                    elif hasattr(existing, "via"):
                        existing_via = existing.via
                    elif hasattr(existing, "get"):
                        existing_via = existing.get("via")

                    self.log.debug(f"Existing SPACE_PARENT via field for removal: {existing_via}")

                    # If exists and has via field, need to remove by setting empty content
                    if existing and existing_via:
                        # Has via, need to remove by setting empty content
                        await self.client.send_state_event(
                            room_id,
                            EventType.SPACE_PARENT,
                            {},
                            state_key=parent_space_id
                        )
                        self.log.info(f"Removed ticket room {room_id} from space {parent_space_id} (status: {new_status})")
                        return True
                    else:
                        # Already empty or missing via, already removed
                        self.log.debug(f"Ticket room {room_id} already removed from space {parent_space_id} (status: {new_status})")
                        return True
                except MNotFound:
                    # SPACE_PARENT not found, already removed
                    self.log.debug(f"Ticket room {room_id} has no SPACE_PARENT to {parent_space_id}, already removed")
                    return True
                except Exception as e:
                    self.log.warning(f"Could not remove SPACE_PARENT for room {room_id}: {e}")
                    # Might already be removed or not exist
                    return False
        except Exception as e:
            self.log.error(f"Failed to update space visibility for ticket room {room_id}: {e}")
            return False

    async def _is_staff_user(self, user_id: str, room_id: str) -> bool:
        """Check if user has staff permissions (power level >= 50) in a room."""
        level = await self._get_user_power_level(user_id, room_id)
        is_staff = level >= 50
        self.log.info(f"Staff check: user {user_id} has level {level} in room {room_id} -> {'staff' if is_staff else 'not staff'}")
        return is_staff

    async def _is_admin_in_room(self, user_id: str, room_id: str) -> bool:
        """Check if user has admin permissions (power level >= 100) in a room."""
        level = await self._get_user_power_level(user_id, room_id)
        is_admin = level >= 100
        self.log.info(f"Admin check: user {user_id} has level {level} in room {room_id} -> {'admin' if is_admin else 'not admin'}")
        return is_admin

    async def _is_staff_in_any_command_room(self, user_id: str) -> bool:
        """Check if user has staff permissions in any enabled command room."""
        command_rooms = self.db.get_all_command_rooms()
        for room in command_rooms:
            if room.get("enabled", True):
                if await self._is_staff_user(user_id, room["room_id"]):
                    return True
        return False

    async def _is_admin_in_any_command_room(self, user_id: str) -> bool:
        """Check if user has admin permissions in any enabled command room."""
        command_rooms = self.db.get_all_command_rooms()
        for room in command_rooms:
            if room.get("enabled", True):
                if await self._is_admin_in_room(user_id, room["room_id"]):
                    return True
        return False

    async def _get_staff_users(self, room_id: str) -> List[str]:
        """Get list of user IDs with staff permissions (power level >= 50) in a room."""
        staff_users = []

        # Get room creator (has power level 100)
        creator = await self._get_room_create_sender(room_id)
        if creator:
            staff_users.append(creator)

        # Get power levels event
        power_levels = await self._get_power_levels_event(room_id)
        if power_levels is None:
            # No custom power levels, only creator is staff
            return staff_users

        # Add users with explicit power level >= 50
        if power_levels.users:
            for user_id, level in power_levels.users.items():
                if user_id == self.client.mxid:
                    continue  # Skip bot
                if user_id in staff_users:
                    continue  # Already added as creator
                if level >= 50:
                    staff_users.append(user_id)

        # If users_default >= 50, we need to check all joined members
        if power_levels.users_default is not None and power_levels.users_default >= 50:
            try:
                members = await self.client.get_joined_members(room_id)
                for user_id in members:
                    if user_id == self.client.mxid:
                        continue
                    if user_id in staff_users:
                        continue
                    # User not in explicit users dict, gets users_default
                    staff_users.append(user_id)
            except Exception as e:
                self.log.error(f"Error getting joined members for {room_id}: {e}")
                # Continue with staff users we already have

        # Remove duplicates
        unique_staff = list(dict.fromkeys(staff_users))
        self.log.info(f"Found {len(unique_staff)} staff users in room {room_id}")
        return unique_staff



    async def _block_if_ntn_room(self, evt: MessageEvent) -> bool:
        """Check if command should be blocked because it's in a New Ticket Notification room.

        Returns True if command should be blocked (i.e., not allowed in New Ticket Notification room).
        Only New Ticket Notification room commands and help are allowed in New Ticket Notification rooms.
        """
        # Check if this room is a New Ticket Notification room
        intake_room = self.db.get_intake_room(evt.room_id)
        if not intake_room:
            return False  # Not a New Ticket Notification room, no blocking

        command_text = evt.content.body.strip()

        # List of allowed command patterns in New Ticket Notification rooms
        allowed_patterns = [
            "!ticket ntn_room add",
            "!ticket ntn_room remove",
            "!ticket ntn_room list",
            "!ticket ntn_room enable",
            "!ticket ntn_room disable",
            "!ticket ntn_room tris",
            "!ticket help",
        ]

        # Also allow the parent command "!ticket ntn_room" (without subcommand)
        if command_text == "!ticket ntn_room":
            return False

        # Allow base command "!ticket" (shows help)
        if command_text == "!ticket":
            return False

        # Check if command starts with any allowed pattern
        for pattern in allowed_patterns:
            if command_text.startswith(pattern):
                return False  # Allowed

        # Block all other commands in New Ticket Notification rooms
        self.log.info(f"Blocking command '{command_text}' in New Ticket Notification room {evt.room_id}")
        await evt.reply(
            "❌ This command cannot be used in a New Ticket Notification room.\n\n"
            "New Ticket Notification rooms are only for receiving ticket notifications. "
            "Staff-related commands must be used in command rooms, "
            "and user commands must be used in direct messages with the bot."
        )
        return True

    async def _block_if_command_room_for_ntn_commands(self, evt: MessageEvent) -> bool:
        """Check if New Ticket Notification room admin commands should be blocked because they're in a command room.

        Returns True if command should be blocked (i.e., New Ticket Notification room admin commands not allowed in command rooms).
        New Ticket Notification room management should be done in New Ticket Notification rooms or regular rooms, not command rooms.
        """
        # Check if this room is a command room
        command_room = self.db.get_command_room(evt.room_id)
        if not command_room:
            return False  # Not a command room, no blocking

        command_text = evt.content.body.strip()

        # List of New Ticket Notification admin command patterns to block in command rooms
        ntn_command_patterns = [
            "!ticket ntn_room add",
            "!ticket ntn_room remove",
            "!ticket ntn_room enable",
            "!ticket ntn_room disable",
            "!ticket ntn_room tris",
        ]

        # Also block the parent command "!ticket ntn_room" (without subcommand)
        if command_text == "!ticket ntn_room":
            self.log.info(f"Blocking New Ticket Notification admin command '{command_text}' in command room {evt.room_id}")
            await evt.reply(
                "❌ New Ticket Notification room management commands cannot be used in a command room.\n\n"
                "New Ticket Notification rooms are for receiving ticket notifications only. "
                "Command rooms are for staff-related ticket commands only. "
                "A room cannot be both a command room and a New Ticket Notification room.\n\n"
                "Please use New Ticket Notification room management commands in the New Ticket Notification room itself or in a regular room."
            )
            return True

        # Check if command starts with any New Ticket Notification admin pattern
        for pattern in ntn_command_patterns:
            if command_text.startswith(pattern):
                self.log.info(f"Blocking New Ticket Notification admin command '{command_text}' in command room {evt.room_id}")
                await evt.reply(
                    "❌ New Ticket Notification room management commands cannot be used in a command room.\n\n"
                    "New Ticket Notification rooms are for receiving ticket notifications only. "
                    "Command rooms are for staff-related ticket commands only. "
                    "A room cannot be both a command room and a New Ticket Notification room.\n\n"
                    "Please use New Ticket Notification room management commands in the New Ticket Notification room itself or in a regular room."
                )
                return True

        return False  # Not an intake admin command, allow it

    async def _show_help(self, evt: MessageEvent) -> None:
        """Show help for ticket commands."""
        response = (
            "## Ticket System Commands:\n\n"
            "**Administrator Commands** (power level 100):\n"
            "- `!ticket ntn_room` - Manage New Ticket Notification rooms (add, remove, list, enable, disable, tris)\n"
            "- `!ticket command_room` - Manage command rooms (add, remove, list, enable, disable)\n"
            "- `!ticket category` - Manage ticket categories (add, remove, list, enable, disable)\n"
            "- `!ticket delete [TICKET_ID]` - Delete a ticket: in ticket room (admin) or in DM with ticket number (admin in any command room)\n\n"
            "**Staff Commands** (power level 50+):\n"
            "- `!ticket debug` - Show debug information\n"
            "- `!ticket search [filters]` - Search/filter tickets with clickable room links\n"
            "  Filters: `status=open|in_progress|closed|resolved`, `assignee=@user`, `creator=@user`, `category=id`, `search=text`\n\n"
            "**Command Room Commands** (require enabled command room):\n"
            "- `!ticket assign TICKET-XXXX [@user]` - Assign ticket: self (moderator+) or specific user (admin)\n"
            "- `!ticket unassign TICKET-XXXX [@user|all]` - Unassign: self (moderator+), specific user (admin), or all (admin)\n\n"
            "**Ticket Management Commands** (must be in ticket room):\n"
            "- `!ticket close` - Close the ticket (admin/moderator/creator)\n"
            "- `!ticket resolve` - Resolve the ticket (admin/moderator/creator)\n"
            "- `!ticket reopen` - Reopen a closed ticket (admin/moderator only)\n"
            "- `!ticket note <text>` - Add a note to the ticket (admin/moderator only)\n\n"
            "**Ticket Information Commands** (must be in ticket room, any participant):\n"
            "- `!ticket info` - Show ticket information\n"
            "- `!ticket notes` - Show all notes for this ticket\n\n"
            "**User Commands** (direct message with bot only):\n"
            "- `!ticket create <title> | <description>` - Create a new support ticket\n"
            "- `!ticket my [status]` - List your tickets (status: open, in_progress, closed, resolved, all)\n"
            "- `!ticket help` - Show this help (works anywhere)\n"
            "Note: `!ticket` (without subcommand) also shows this help."
        )
        await evt.reply(response)

    # Admin commands

    @command.new("ticket", require_subcommand=False, help="Show help for ticket commands")
    async def ticket_command(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket command triggered: {evt.content.body}")

        # Block if in New Ticket Notification room (except allowed commands)
        if await self._block_if_ntn_room(evt):
            return

        # Block New Ticket Notification admin commands in command rooms
        if await self._block_if_command_room_for_ntn_commands(evt):
            return

        # Show help when base command is used
        await self._show_help(evt)
