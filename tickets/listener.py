from typing import Optional, Dict, List, Any
import json, html, time, re
from maubot.plugin_base import Plugin
from maubot.handlers import command, event
from maubot.matrix import MaubotMatrixClient
from mautrix.errors import MNotFound, MLimitExceeded, MForbidden, MatrixBadRequest
from mautrix.types import EventType, MessageEvent, StateEvent, Membership, RoomCreateStateEventContent, PowerLevelStateEventContent, SpaceParentStateEventContent, RoomDirectoryVisibility, RoomCreatePreset, TextMessageEventContent, Format, MessageType

from .databases import Database


class TicketsHandler:
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
        1. If room is ticket, intake, or command room → NOT DM
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
            # First check if this room is a ticket, intake, or command room - these are not DMs
            ticket = await self._get_ticket_for_room(evt.room_id)
            if ticket:
                self.log.info(f"Room {evt.room_id} is a ticket room, not DM")
                return False
            intake_room = self.db.get_intake_room(evt.room_id)
            if intake_room:
                self.log.info(f"Room {evt.room_id} is an intake room, not DM")
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
        """Check if user can manage ticket (admin or ticket creator)."""
        # Check if user is admin
        if await self.has_admin_power(evt):
            return True
        # Check if user is ticket creator
        if evt.sender == ticket["creator"]:
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
        
        # Get ticket space ID (prefer stored ticket_space_id, then space from intake room, then configured space from command rooms)
        parent_space_id = ticket.get("ticket_space_id")
        if parent_space_id:
            self.log.info(f"Using stored ticket space {parent_space_id} from ticket record, ticket status: {new_status}")
        else:
            # Try to get space from intake room
            parent_space_id = self.db.get_intake_room_space(intake_room_id)
            if parent_space_id:
                self.log.info(f"Using configured ticket space {parent_space_id} from intake room {intake_room_id} (no space stored in ticket), ticket status: {new_status}")
            else:
                # Fallback to global ticket space from command rooms (for backward compatibility)
                parent_space_id = self.db.get_ticket_space_id()
                if parent_space_id:
                    self.log.info(f"Using configured ticket space {parent_space_id} from command room (no space stored in ticket or intake room), ticket status: {new_status}")
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
    
    async def _notify_intake_room(self, ticket: Dict[str, Any], message: str, mention_staff: bool = False, html_message: Optional[str] = None) -> bool:
        """Send a notification to the ticket's intake room.
        
        Args:
            ticket: Ticket dictionary
            message: Plain text message
            mention_staff: Whether to mention staff users
            html_message: Optional HTML formatted message. If provided, will be used as formatted_body.
        """
        intake_room_id = ticket.get("intake_room_id")
        if not intake_room_id:
            self.log.warning(f"Ticket {ticket['ticket_number']} has no intake room ID")
            return False
        self.log.debug(f"Notifying intake room {intake_room_id} for ticket {ticket['ticket_number']}: {message[:100]}...")
        
        # Check if intake room still exists and is enabled
        intake_room = self.db.get_intake_room(intake_room_id)
        if not intake_room or not intake_room.get("enabled", True):
            self.log.warning(f"Intake room {intake_room_id} not found or disabled")
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
            
            self.log.info(f"Notification sent to intake room {intake_room_id}")
            return True
        except Exception as e:
            self.log.error(f"Failed to send notification to intake room {intake_room_id}: {e}")
            return False

    async def _block_if_intake_room(self, evt: MessageEvent) -> bool:
        """Check if command should be blocked because it's in an intake room.
        
        Returns True if command should be blocked (i.e., not allowed in intake room).
        Only admin intake commands and help are allowed in intake rooms.
        """
        # Check if this room is an intake room
        intake_room = self.db.get_intake_room(evt.room_id)
        if not intake_room:
            return False  # Not an intake room, no blocking
        
        command_text = evt.content.body.strip()
        
        # List of allowed command patterns in intake rooms
        allowed_patterns = [
            "!ticket admin intake add",
            "!ticket admin intake remove", 
            "!ticket admin intake list",
            "!ticket admin intake enable",
            "!ticket admin intake disable",
            "!ticket admin intake space",
            "!ticket help",
        ]
        
        # Also allow the parent command "!ticket admin intake" (without subcommand)
        if command_text == "!ticket admin intake":
            return False
        
        # Check if command starts with any allowed pattern
        for pattern in allowed_patterns:
            if command_text.startswith(pattern):
                return False  # Allowed
        
        # Block all other commands in intake rooms
        self.log.info(f"Blocking command '{command_text}' in intake room {evt.room_id}")
        await evt.reply(
            "❌ This command cannot be used in an intake room.\n\n"
            "Intake rooms are only for receiving ticket notifications. "
            "Staff-related commands must be used in command rooms, "
            "and user commands must be used in direct messages with the bot."
        )
        return True

    async def _block_if_command_room_for_intake_commands(self, evt: MessageEvent) -> bool:
        """Check if intake admin commands should be blocked because they're in a command room.
        
        Returns True if command should be blocked (i.e., intake admin commands not allowed in command rooms).
        Intake room management should be done in intake rooms or regular rooms, not command rooms.
        """
        # Check if this room is a command room
        command_room = self.db.get_command_room(evt.room_id)
        if not command_room:
            return False  # Not a command room, no blocking
        
        command_text = evt.content.body.strip()
        
        # List of intake admin command patterns to block in command rooms
        intake_command_patterns = [
            "!ticket admin intake add",
            "!ticket admin intake remove",
            "!ticket admin intake list",
            "!ticket admin intake enable",
            "!ticket admin intake disable",
            "!ticket admin intake space",
        ]
        
        # Also block the parent command "!ticket admin intake" (without subcommand)
        if command_text == "!ticket admin intake":
            self.log.info(f"Blocking intake admin command '{command_text}' in command room {evt.room_id}")
            await evt.reply(
                "❌ Intake room management commands cannot be used in a command room.\n\n"
                "Intake rooms are for receiving ticket notifications only. "
                "Command rooms are for staff-related ticket commands only. "
                "A room cannot be both a command room and an intake room.\n\n"
                "Please use intake room management commands in the intake room itself or in a regular room."
            )
            return True
        
        # Check if command starts with any intake admin pattern
        for pattern in intake_command_patterns:
            if command_text.startswith(pattern):
                self.log.info(f"Blocking intake admin command '{command_text}' in command room {evt.room_id}")
                await evt.reply(
                    "❌ Intake room management commands cannot be used in a command room.\n\n"
                    "Intake rooms are for receiving ticket notifications only. "
                    "Command rooms are for staff-related ticket commands only. "
                    "A room cannot be both a command room and an intake room.\n\n"
                    "Please use intake room management commands in the intake room itself or in a regular room."
                )
                return True
        
        return False  # Not an intake admin command, allow it

    # Admin commands
    
    @command.new("ticket", require_subcommand=True)
    async def ticket_command(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
            return
    
    # Admin command hierarchy
    @ticket_command.subcommand("admin", help="Admin commands")
    async def admin(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
            return
        
        await evt.reply(
            "Admin commands:\n"
            "• `!ticket admin intake` - Manage intake rooms\n"
            "• `!ticket admin command` - Manage command rooms\n"
            "• `!ticket admin category` - Manage ticket categories\n"
        )
    admin_command = admin
    
    @admin_command.subcommand("intake", help="Manage intake rooms")
    async def admin_intake(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin intake command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
            return
        
        await evt.reply(
            "Intake room management commands:\n"
            "• `!ticket admin intake add` - Add this room as a support intake room\n"
            "• `!ticket admin intake remove [all]` - Remove this room (or all) from support intake rooms\n"
            "• `!ticket admin intake list` - List all support intake rooms\n"
            "• `!ticket admin intake enable` - Enable this intake room\n"
            "• `!ticket admin intake disable` - Disable this intake room\n"
            "• `!ticket admin intake space [set|unset|unset all|debug|fix]` - Configure ticket space for this intake room (set/unset/unset all/debug/fix/show)\n"
        )
    @admin_intake.subcommand("add", help="Add this room as a support intake room")
    async def admin_add(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin add command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
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
        
        # Check if room already registered as intake room
        existing_by_room = self.db.get_intake_room(evt.room_id)
        if existing_by_room:
            await evt.reply("❌ This room is already registered as a support intake room.")
            return
        
        # Check if room is already a command room (prevent dual registration)
        existing_command_room = self.db.get_command_room(evt.room_id)
        if existing_command_room:
            await evt.reply("❌ This room is already registered as a command room. A room cannot be both a command room and an intake room.")
            return
        
        # Get enabled categories
        all_categories = self.db.get_all_categories()
        categories = [cat for cat in all_categories if cat.get("enabled", True)]
        if not categories:
            await evt.reply(
                "❌ No enabled ticket categories exist. Please create and enable at least one category first.\n"
                "Use: `!ticket admin category add <category_id> <name> [description]`"
            )
            return
        
        # Store pending intake room data (name will be generated after category selection)
        pending_data = {
            "type": "intake_room",
            "room_id": evt.room_id,
        }
        self._set_pending_ticket(evt, pending_data)
        
        # List categories for user to choose (including "all" option)
        category_list = "## Select a category for this intake room:\n\n"
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
            f"If you don't respond within {self.pending_timeout} seconds, the intake room creation will be cancelled."
        )
        
        await evt.reply(category_list)
    
    @admin_intake.subcommand("remove", help="Remove this room (or all) from support intake rooms")
    @command.argument("scope", label="scope", required=False)
    async def admin_remove(self, evt: MessageEvent, scope: Optional[str] = None) -> None:
        self.log.info(f"Admin remove command triggered: {evt.content.body}")
        
        is_all_operation = scope and scope.lower() == "all"
        
        # For "all" operation, allow from any room type
        if not is_all_operation:
            # Block if in intake room (except allowed commands)
            if await self._block_if_intake_room(evt):
                return
            
            # Block intake admin commands in command rooms
            if await self._block_if_command_room_for_intake_commands(evt):
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
            # Remove all intake rooms and their spaces
            removed_count = self.db.remove_all_intake_rooms()
            # Also clear all intake room spaces
            cleared_count = self.db.clear_all_intake_room_spaces()
            await evt.reply(f"✅ Removed all intake rooms ({removed_count} rooms removed) and cleared {cleared_count} space configurations.")
        else:
            # Remove only this room
            success = self.db.remove_intake_room(evt.room_id)
            if success:
                await evt.reply("✅ This room has been removed from support intake rooms.")
            else:
                await evt.reply("❌ This room is not registered as a support intake room.")
    
    @admin_intake.subcommand("list", help="List all support intake rooms")
    async def admin_list(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin list command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
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
            await evt.reply("No support intake rooms registered.")
            return
        
        response = "## Support Intake Rooms:\n\n"
        for room in rooms:
            status = "✅ Enabled" if room["enabled"] else "❌ Disabled"
            category = room.get("category_id") or "all"
            response += f"- **{room['name']}** ({status})\n"
            response += f"  Room ID: `{room['room_id']}`\n"
            response += f"  Category: `{category}`\n\n"
        
        await evt.reply(response)
    
    @admin_intake.subcommand("enable", help="Enable this intake room")
    async def admin_enable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin enable command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
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
            await evt.reply("✅ This intake room has been enabled.")
        else:
            await evt.reply("❌ This room is not registered as an intake room.")
    
    @admin_intake.subcommand("disable", help="Disable this intake room")
    async def admin_disable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin disable command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
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
            await evt.reply("✅ This intake room has been disabled. Users cannot create tickets here.")
        else:
            await evt.reply("❌ This room is not registered as an intake room.")
    
    @admin_intake.subcommand("space", help="Set, unset, unset all, debug, fix, or show the ticket space for this intake room")
    @command.argument("action", label="action", required=False, pass_raw=True)
    async def admin_intake_space(self, evt: MessageEvent, action: Optional[str] = None) -> None:
        self.log.info(f"Admin intake space command triggered: {evt.content.body}")
        
        # Parse action early to check if it's "unset all"
        action_parts = action.split() if action else []
        subaction = action_parts[0].lower() if action_parts else None
        subaction_arg = " ".join(action_parts[1:]) if len(action_parts) > 1 else None
        action_lower = subaction
        is_unset_all = (action_lower == "unset" and subaction_arg == "all")
        
        # For "unset all" operation, allow from any room type
        if not is_unset_all:
            # Block if in command room (intake admin commands not allowed in command rooms)
            if await self._block_if_command_room_for_intake_commands(evt):
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
            # Check if this room is an intake room
            intake_room = self.db.get_intake_room(evt.room_id)
            if not intake_room:
                await evt.reply("❌ This room is not registered as an intake room.")
                return
        else:
            # For "unset all", we still need to get intake rooms for the operation
            intake_room = self.db.get_intake_room(evt.room_id)
        
        if action_lower == "unset" and subaction_arg == "all":
            # Unset spaces from ALL intake rooms (including those bot has left)
            cleared_count = self.db.clear_all_intake_room_spaces()
            response = f"✅ Unset ticket spaces from {cleared_count} intake room(s)."
            response += "\nAll intake rooms now have no space configured."
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
                response = "✅ Ticket space unset for this intake room."
                if other_rooms_with_space:
                    # Get the category name for this intake room for context
                    category_id = intake_room.get("category_id")
                    category = self.db.get_category(category_id) if category_id else None
                    category_name = category.get("name") if category else category_id or "unknown"
                    
                    response += f"\n\n⚠️ **Note:** {len(other_rooms_with_space)} other intake room(s) still have spaces configured."
                    response += f"\nNew tickets created from those intake rooms will still be added to their spaces."
                    response += "\nTo unset all spaces, use `!ticket admin intake space unset all` or run `!ticket admin intake space unset` in those rooms."
                else:
                    response += "\nNew tickets will NOT be added to any space (no other intake rooms have spaces configured)."
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
                
                response = "❌ This intake room is not a space and not in a space.\n\n"
                response += diagnostic + "\n"
                response += "**Options:**\n"
                response += "1. Add this room to a space (as a child room)\n"
                response += "2. Make this room a space (change room type to space)\n"
                if all_parents and not any(p[2] for p in all_parents):
                    response += "\n**If room already has SPACE_PARENT events:**\n"
                    response += "The via field may be empty. You may need to re-add the room to the space.\n"
                response += "\nThen try again: `!ticket admin intake space set`"
                
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
                response += "New tickets created in this intake room will be added to this space.\n"
                
                if manual_space:
                    response += f"\n**Selection reason:** Manually specified space ID."
                elif is_room_space:
                    response += f"\n**Selection reason:** This intake room is a space (room type: `m.space`)."
                elif len(valid_parents) == 1:
                    response += f"\n**Selection reason:** This intake room is in exactly one space."
                elif len(valid_parents) > 1:
                    parent_ids = [p[0] for p in valid_parents]
                    response += f"\n**Selection reason:** This intake room is in {len(valid_parents)} spaces: {', '.join([f'`{pid}`' for pid in parent_ids])}.\n"
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
                response += "3. Test by creating a new ticket from this intake room\n"
                response += "4. Use `!ticket admin intake space debug` to verify configuration\n"
                
                await evt.reply(response)
            else:
                await evt.reply("❌ Failed to set ticket space.")
            return
        elif action_lower == "debug":
            # Show detailed debugging information about the ticket space for THIS intake room
            current_ticket_space = intake_room.get("space_id")
            
            response = "## Ticket Space Debug Information (Intake Room)\n\n"
            
            if not current_ticket_space:
                response += "❌ No ticket space configured for this intake room.\n"
                # Show category info
                category_id = intake_room.get("category_id")
                if category_id:
                    category = self.db.get_category(category_id)
                    category_name = category.get("name") if category else category_id
                    response += f"\n**Category:** {category_name} (ID: {category_id})\n"
                await evt.reply(response)
                return
            
            response += f"**Configured ticket space for this intake room:** `{current_ticket_space}`\n\n"
            
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
            
            response += "\n**Recent tickets from this intake room (last 5):**\n"
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
                response += "No category associated with this intake room.\n"
            
            await evt.reply(response)
            return
        elif action_lower == "fix":
            # Fix SPACE_PARENT events for all open/in_progress tickets from this intake room's category
            current_ticket_space = intake_room.get("space_id")
            if not current_ticket_space:
                await evt.reply("❌ No ticket space configured for this intake room. Use `!ticket admin intake space set` first.")
                return
            
            category_id = intake_room.get("category_id")
            if not category_id:
                await evt.reply("❌ This intake room has no category associated. Cannot identify which tickets to fix.")
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
            response += "3. Use `!ticket admin intake space debug` to verify configuration\n"
            
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
                response += f"**This intake room's configured space:** `{this_intake_room_space}`\n"
                if this_room_has_space:
                    response += "    This intake room's space is active and will be used for new tickets.\n"
                else:
                    response += "    Note: This intake room has a space configured but is not enabled.\n"
                if rooms_with_space:
                    room_names = [f"\"{r['name']}\" (`{r['room_id']}`)" for r in rooms_with_space]
                    response += f"Other intake rooms with spaces: {', '.join(room_names)}\n"
                    if len(rooms_with_space) > 1:
                        response += f"\n⚠️ **Note:** {len(rooms_with_space)} intake rooms have spaces configured.\n"
                        response += "Each intake room's space is used for tickets created in that room.\n"
                response += "\n"
            else:
                response += "**This intake room's configured space:** None\n"
                response += "    New tickets created here will NOT be added to any space.\n"
                if rooms_with_space:
                    response += f"\n⚠️ **Note:** {len(rooms_with_space)} other intake room(s) have spaces configured.\n"
                    response += "Tickets created in those rooms will be added to their respective spaces.\n"
                response += "\n"
            
            if this_space:
                if is_room_space:
                    response += f"**This intake room is a space:** `{this_space}`\n"
                    response += "You can set this space as the ticket space with: `!ticket admin intake space set`\n"
                else:
                    response += f"**This intake room is in space:** `{this_space}`\n"
                    response += "You can set this space as the ticket space with: `!ticket admin intake space set`\n"
            else:
                response += "**This intake room is not a space and not in a space.**\n"
                response += "**Options to set a ticket space:**\n"
                response += "1. Add this room to a space (as a child room)\n"
                response += "2. Make this room a space (change room type to space)\n"
                response += "Then use: `!ticket admin intake space set`\n"
            
            if this_intake_room_space:
                response += "\nTo unset the ticket space: `!ticket admin intake space unset` (or `unset all` to unset from all intake rooms)"
            
            await evt.reply(response)
            return
        else:
            await evt.reply("❌ Unknown action. Use `set`, `unset`, `unset all`, `debug`, `fix`, or no action to show current configuration.")
    
    @admin_command.subcommand("command", help="Manage command rooms")
    async def admin_command_room(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command room command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        await evt.reply(
            "Command room management commands:\n"
            "• `!ticket admin command add` - Mark this room as a command room\n"
            "• `!ticket admin command remove [all]` - Remove this room (or all) from command rooms\n"
            "• `!ticket admin command list` - List all command rooms\n"
            "• `!ticket admin command enable` - Enable this command room\n"
            "• `!ticket admin command disable` - Disable this command room\n"
            "• `!ticket admin command space [set|clear|clear all]` - Configure ticket space (set/clear/clear all/show)\n"
        )
    
    @admin_command_room.subcommand("add", help="Mark this room as a command room")
    async def admin_command_add(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command add command triggered: {evt.content.body}")
        self.log.info("Admin command add starting processing")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Block intake admin commands in command rooms
        if await self._block_if_command_room_for_intake_commands(evt):
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
        
        # Check if room is already an intake room (prevent dual registration)
        existing_intake_room = self.db.get_intake_room(evt.room_id)
        if existing_intake_room:
            await evt.reply("❌ This room is already registered as an intake room. A room cannot be both a command room and an intake room.")
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
    
    @admin_command_room.subcommand("remove", help="Remove this room (or all) from command rooms")
    @command.argument("scope", label="scope", required=False)
    async def admin_command_remove(self, evt: MessageEvent, scope: Optional[str] = None) -> None:
        self.log.info(f"Admin command remove command triggered: {evt.content.body}")
        
        is_all_operation = scope and scope.lower() == "all"
        
        # For "all" operation, allow from any room type
        if not is_all_operation:
            # Block if in intake room (except allowed commands)
            if await self._block_if_intake_room(evt):
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
    
    @admin_command_room.subcommand("list", help="List all command rooms")
    async def admin_command_list(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command list command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
    
    @admin_command_room.subcommand("enable", help="Enable this command room")
    async def admin_command_enable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command enable command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
    
    @admin_command_room.subcommand("disable", help="Disable this command room")
    async def admin_command_disable(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin command disable command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
    
    @admin_command_room.subcommand("space", help="Set, clear, unset, clear all, or debug the ticket space for this command room")
    @command.argument("action", label="action", required=False, pass_raw=True)
    async def admin_command_space(self, evt: MessageEvent, action: Optional[str] = None) -> None:
        self.log.info(f"Admin command space command triggered: {evt.content.body}")
        
        # Parse action early to check if it's "clear all"
        action_parts = action.split() if action else []
        subaction = action_parts[0].lower() if action_parts else None
        subaction_arg = " ".join(action_parts[1:]) if len(action_parts) > 1 else None
        action_lower = subaction
        is_clear_all = (action_lower == "clear" and subaction_arg == "all")
        
        # For "clear all" operation, allow from any room type
        if not is_clear_all:
            # Block if in intake room (except allowed commands)
            if await self._block_if_intake_room(evt):
                return
        
        # Check if this is a direct message (admin commands should not work in DMs)
        if await self._is_direct_message(evt):
            await evt.reply("❌ Admin commands cannot be used in direct messages. Please use this command in a regular room.")
            return
        
        # For "clear all" operation, allow from ticket rooms too
        if not is_clear_all:
            # Ensure this is not a ticket room
            ticket = await self._get_ticket_for_room(evt.room_id)
            if ticket:
                await evt.reply("❌ Admin commands cannot be used in ticket rooms. Please use this command in a regular room.")
                return
        
        if not await self.ensure_admin(evt):
            return
        
        # For "clear all" operation, don't require the room to be a command room
        if not is_clear_all:
            # Check if this room is a command room
            command_room = self.db.get_command_room(evt.room_id)
            if not command_room:
                await evt.reply("❌ This room is not registered as a command room.")
                return
        else:
            # For "clear all", we still need to get command room for the operation (may be None)
            command_room = self.db.get_command_room(evt.room_id)
        
        if action_lower == "clear" and subaction_arg == "all":
            # Clear spaces from ALL command rooms (including those bot has left)
            cleared_count = self.db.clear_all_command_room_spaces()
            response = f"✅ Cleared ticket spaces from {cleared_count} command room(s)."
            response += "\nAll command rooms now have no space configured."
            response += "\nNew tickets will NOT be added to any space."
            await evt.reply(response)
            return
        elif action_lower in ("clear", "unset"):
            success = self.db.set_command_room_space(evt.room_id, None)
            if success:
                # Check if other command rooms have spaces configured
                all_command_rooms = self.db.get_all_command_rooms()
                other_rooms_with_space = [
                    r for r in all_command_rooms 
                    if r.get("space_id") and r.get("enabled", True) and r["room_id"] != evt.room_id
                ]
                response = "✅ Ticket space cleared for this command room."
                if other_rooms_with_space:
                    global_space = self.db.get_ticket_space_id()
                    response += f"\n\n⚠️ **Note:** {len(other_rooms_with_space)} other command room(s) still have spaces configured."
                    if global_space:
                        response += f"\nThe current ticket space is still set to `{global_space}` (from other command rooms)."
                    response += "\nNew tickets created from those command rooms will still be added to their spaces."
                    response += "\nTo clear all spaces, use `!ticket admin command space clear all` or run `!ticket admin command space clear` in those rooms."
                else:
                    response += "\nNew tickets will NOT be added to any space (no other command rooms have spaces configured)."
                await evt.reply(response)
            else:
                await evt.reply("❌ Failed to clear ticket space.")
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
                # Auto-detect: Get space for this command room (could be the room itself if it's a space, or its parent space)
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
                
                response = "❌ This command room is not a space and not in a space.\n\n"
                response += diagnostic + "\n"
                response += "**Options:**\n"
                response += "1. Add this room to a space (as a child room)\n"
                response += "2. Make this room a space (change room type to space)\n"
                if all_parents and not any(p[2] for p in all_parents):
                    response += "\n**If room already has SPACE_PARENT events:**\n"
                    response += "The via field may be empty. You may need to re-add the room to the space.\n"
                response += "\nThen try again: `!ticket admin command space set`"
                
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
            
            # Set the detected space as ticket space for this command room
            success = self.db.set_command_room_space(evt.room_id, space_id)
            if success:
                response = f"✅ Ticket space set to `{space_id}` ({space_type}). New tickets will be added to this space.\n"
                
                if manual_space:
                    response += f"\n**Selection reason:** Manually specified space ID."
                elif is_room_space:
                    response += f"\n**Selection reason:** This command room is a space (room type: `m.space`)."
                elif len(valid_parents) == 1:
                    response += f"\n**Selection reason:** This command room is in exactly one space."
                elif len(valid_parents) > 1:
                    parent_ids = [p[0] for p in valid_parents]
                    response += f"\n**Selection reason:** This command room is in {len(valid_parents)} spaces: {', '.join([f'`{pid}`' for pid in parent_ids])}.\n"
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
                response += "3. Test by creating a new ticket\n"
                response += "4. Use `!ticket admin command space debug` to verify configuration\n"
                
                await evt.reply(response)
            else:
                await evt.reply("❌ Failed to set ticket space.")
            return
        elif action_lower == "debug":
            # Show detailed debugging information about the ticket space for THIS command room
            current_ticket_space = command_room.get("space_id")
            
            response = "## Ticket Space Debug Information\n\n"
            
            if not current_ticket_space:
                response += "❌ No ticket space configured for this command room.\n"
                # Also show global ticket space for reference
                global_ticket_space = self.db.get_ticket_space_id()
                if global_ticket_space and global_ticket_space != current_ticket_space:
                    response += f"\n⚠️ **Note:** Another command room has space `{global_ticket_space}` configured.\n"
                    response += "The bot will use that space for ticket creation if this command room has no space.\n"
                await evt.reply(response)
                return
            
            response += f"**Configured ticket space for this command room:** `{current_ticket_space}`\n\n"
            
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
            
            response += "\n**Recent ticket rooms (last 5):**\n"
            recent_tickets = self.db.get_all_tickets(limit=5)
            for ticket in recent_tickets:
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
            
            await evt.reply(response)
            return
        elif action_lower == "fix":
            # Fix SPACE_PARENT events for all open/in_progress tickets
            current_ticket_space = command_room.get("space_id")
            if not current_ticket_space:
                await evt.reply("❌ No ticket space configured for this command room. Use `!ticket admin command space set` first.")
                return
            
            await evt.reply(f"🔧 Fixing SPACE_PARENT events for all open/in_progress tickets in space `{current_ticket_space}`...")
            
            # Get all open and in_progress tickets
            open_tickets = self.db.get_tickets_by_status("open")
            in_progress_tickets = self.db.get_tickets_by_status("in_progress")
            all_tickets = open_tickets + in_progress_tickets
            
            if not all_tickets:
                await evt.reply("No open or in_progress tickets found.")
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
                # 2. Otherwise use current command room's space
                target_space_id = ticket.get("ticket_space_id")
                using_stored_space = True
                if not target_space_id:
                    target_space_id = current_ticket_space
                    using_stored_space = False
                    self.log.info(f"Ticket {ticket_number} has no stored space, using command room space {target_space_id}")
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
            response += "3. Use `!ticket admin command space debug` to verify configuration\n"
            
            await evt.reply(response)
            return
        elif action_lower is None:
            # Show current ticket space configuration
            current_ticket_space = self.db.get_ticket_space_id()
            
            # Get all command rooms with space_id set
            all_command_rooms = self.db.get_all_command_rooms()
            rooms_with_space = [r for r in all_command_rooms if r.get("space_id") and r.get("enabled", True)]
            
            # Get space for this command room (could be the room itself if it's a space, or its parent space)
            this_space = await self._get_space_for_room(evt.room_id)
            is_room_space = await self._is_space_room(evt.room_id)
            
            response = "## Ticket Space Configuration\n\n"
            
            if current_ticket_space:
                response += f"**Current ticket space:** `{current_ticket_space}`\n"
                # Show this command room's configured space
                this_command_room_space = self.db.get_command_room_space(evt.room_id)
                this_room_has_space = any(r['room_id'] == evt.room_id for r in rooms_with_space)
                if this_command_room_space:
                    response += f"**This command room's configured space:** `{this_command_room_space}`\n"
                    if this_room_has_space:
                        response += "    This command room contributes to the current ticket space.\n"
                    else:
                        response += "    Note: This command room has a space configured but is not in the list of enabled command rooms with spaces.\n"
                else:
                    response += "**This command room's configured space:** None\n"
                    if current_ticket_space:
                        response += "    The current ticket space is configured by other command rooms.\n"
                if rooms_with_space:
                    room_names = [f"\"{r['name']}\" (`{r['room_id']}`)" for r in rooms_with_space]
                    response += f"Configured by command room(s): {', '.join(room_names)}\n"
                    if len(rooms_with_space) > 1:
                        response += f"\n⚠️ **Warning:** {len(rooms_with_space)} command rooms have spaces configured.\n"
                        response += "The bot uses the most recently created command room's space for new tickets.\n"
                        response += "To avoid confusion, clear spaces from unused command rooms: `!ticket admin command space clear` (or `clear all` to clear from all command rooms)\n"
                response += "\n"
            else:
                response += "**No ticket space configured.**\n"
                response += "New tickets will NOT be added to any space.\n\n"
            
            if this_space:
                if is_room_space:
                    response += f"**This command room is a space:** `{this_space}`\n"
                    response += "You can set this space as the ticket space with: `!ticket admin command space set`\n"
                else:
                    response += f"**This command room is in space:** `{this_space}`\n"
                    response += "You can set this space as the ticket space with: `!ticket admin command space set`\n"
            else:
                response += "**This command room is not a space and not in a space.**\n"
                response += "**Options to set a ticket space:**\n"
                response += "1. Add this room to a space (as a child room)\n"
                response += "2. Make this room a space (change room type to space)\n"
                response += "Then use: `!ticket admin command space set`\n"
            
            if current_ticket_space:
                response += "\nTo clear the ticket space: `!ticket admin command space clear` (or `clear all` to clear from all command rooms)"
            
            await evt.reply(response)
            return
        else:
            await evt.reply("❌ Unknown action. Use `set`, `clear`, `clear all`, `debug`, `fix`, or no action to show current configuration.")
    
    @admin_command.subcommand("category", help="Manage ticket categories")
    async def admin_category(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin category command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        await evt.reply(
            "Category commands:\n"
            "• `!ticket admin category add <category_id> <name> [description]` - Add a new category\n"
            "• `!ticket admin category remove <category_id>` - Remove a category\n"
            "• `!ticket admin category list` - List all categories\n"
        )

    @admin_category.subcommand("add", help="Add a new category")
    @command.argument("details", label="category_id name [description]", required=True, pass_raw=True)
    async def admin_category_add(self, evt: MessageEvent, details: str) -> None:
        self.log.info(f"Admin category add command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
            await evt.reply("Please provide both category ID and name. Example: `!ticket admin category add tech Technical Issues`")
            return
        category_id = parts[0]
        name = parts[1]
        description = parts[2] if len(parts) > 2 else None
        
        # Reserve 'all' as special category ID for intake rooms
        if category_id == "all":
            await evt.reply("❌ Category ID 'all' is reserved for intake rooms that receive notifications for all categories.")
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

    @admin_category.subcommand("remove", help="Remove a category")
    @command.argument("category_id", label="category_id", required=True)
    async def admin_category_remove(self, evt: MessageEvent, category_id: str) -> None:
        self.log.info(f"Admin category remove command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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

    @admin_category.subcommand("list", help="List all categories")
    async def admin_category_list(self, evt: MessageEvent) -> None:
        self.log.info(f"Admin category list command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
    
    @admin_command.subcommand("search", help="Search/filter tickets with clickable room links")
    @command.argument("filters", label="filters", required=False, pass_raw=True)
    async def admin_search(self, evt: MessageEvent, filters: Optional[str] = None) -> None:
        self.log.info(f"Admin search command triggered: {evt.content.body}")
        
        # Deprecation notice - redirect to new ticket search
        await evt.reply("⚠️ **Deprecated**: `!ticket admin search` is deprecated. Please use `!ticket search` instead (available to moderators and admins).")
        
        # Call the new ticket_search method
        await self.ticket_search(evt, filters)

    @ticket_command.subcommand("search", help="Search/filter tickets with clickable room links (moderator+)")
    @command.argument("filters", label="filters", required=False, pass_raw=True)
    async def ticket_search(self, evt: MessageEvent, filters: Optional[str] = None) -> None:
        self.log.info(f"Ticket search command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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

    @ticket_command.subcommand("create", help="Create a new support ticket (title | description)")
    @command.argument("details", label="title | description", required=True, pass_raw=True)
    async def ticket_create(self, evt: MessageEvent, details: str) -> None:
        self.log.info(f"Ticket create command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
        
        # Ensure this is not an intake room
        intake_room = self.db.get_intake_room(evt.room_id)
        if intake_room:
            await evt.reply("❌ This command cannot be used in an intake room. Please use a direct message with the bot.")
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
        
        # Check that at least one enabled intake room exists
        try:
            intake_rooms = self.db.get_all_intake_rooms()
            enabled_intake_rooms = [r for r in intake_rooms if r.get("enabled", True)]
            
            if not enabled_intake_rooms:
                await evt.reply(
                    "❌ No intake rooms are configured for ticket notifications.\n\n"
                    "**An admin needs to:**\n"
                    "1. Go to a support room\n"
                    "2. Run: `!ticket admin intake add`\n"
                    "3. Ensure the room is enabled with: `!ticket admin intake enable`"
                )
                return
        except Exception as e:
            self.log.error(f"Database error getting intake rooms: {e}")
            await evt.reply("❌ Database error. Please try again later.")
            return
        
        # Store pending ticket data (intake room will be selected after category selection)
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
    
    @ticket_command.subcommand("my", help="List your tickets (optional status: open, in_progress, closed, resolved, all). If you are not in a ticket room, an invite will be sent.")
    @command.argument("status", label="status", required=False)
    async def ticket_my(self, evt: MessageEvent, status: Optional[str] = None) -> None:
        self.log.info(f"Ticket my command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
        
        # Ensure this is not an intake room
        intake_room = self.db.get_intake_room(evt.room_id)
        if intake_room:
            await evt.reply("❌ This command cannot be used in an intake room. Please use a direct message with the bot.")
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
    
    @ticket_command.subcommand("mine", help="Alias for '!ticket my' - list your tickets")
    @command.argument("status", label="status", required=False)
    async def ticket_mine(self, evt: MessageEvent, status: Optional[str] = None) -> None:
        await self.ticket_my(evt, status)
    

    @ticket_command.subcommand("assign", help="Assign ticket to a user. Usage: !ticket assign TICKET-XXXX [@user] (must be used in any enabled command room)")
    @command.argument("args", label="TICKET-XXXX [@user]", required=True, pass_raw=True)
    async def ticket_assign(self, evt: MessageEvent, args: str) -> None:
        self.log.info(f"Ticket assign command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Parse arguments
        ticket_number, user = self._parse_assign_args(evt)
        if not ticket_number:
            await evt.reply("❌ Invalid command syntax. Usage:\n"
                           "`!ticket assign TICKET-XXXX [@user]` (must be used in any enabled command room)\n"
                           "• `!ticket assign TICKET-XXXX` - assign yourself\n"
                           "• `!ticket assign TICKET-XXXX @user` - assign specific user")
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
                await self._notify_intake_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)
            
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
    
    @ticket_command.subcommand("unassign", help="Unassign the ticket (or specific user). Usage: !ticket unassign TICKET-XXXX [@user|all] (must be used in any enabled command room)")
    @command.argument("args", label="TICKET-XXXX [@user]", required=True, pass_raw=True)
    async def ticket_unassign(self, evt: MessageEvent, args: str) -> None:
        self.log.info(f"Ticket unassign command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        # Parse arguments
        ticket_number, user = self._parse_assign_args(evt)
        if not ticket_number:
            await evt.reply("❌ Invalid command syntax. Usage:\n"
                           "`!ticket unassign TICKET-XXXX [@user|all]` (must be used in any enabled command room)\n"
                           "• `!ticket unassign TICKET-XXXX` - unassign yourself\n"
                           "• `!ticket unassign TICKET-XXXX @user` - unassign specific user\n"
                           "• `!ticket unassign TICKET-XXXX all` - unassign all assignees")
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
                    await self._notify_intake_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)
                
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
                    await self._notify_intake_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)
                
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
                    await self._notify_intake_room(ticket, intake_plain_msg, mention_staff=False, html_message=intake_html_msg)
                
                # Send reply in current room (if not already covered by notifications)
                if in_ticket_room:
                    # Already notified ticket room, skip reply to avoid duplicate
                    pass
                else:
                    await evt.reply(reply_msg)
            else:
                await evt.reply("❌ Failed to unassign ticket.")
    
    @ticket_command.subcommand("close", help="Close the ticket")
    async def ticket_close(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket close command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._can_manage_ticket(evt, ticket):
            await evt.reply("❌ You must be an admin or the ticket creator to close this ticket.")
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
            await self._notify_intake_room(ticket, notification_msg, mention_staff=False)
        else:
            await evt.reply("❌ Failed to close ticket.")
    
    @ticket_command.subcommand("reopen", help="Reopen a closed ticket")
    async def ticket_reopen(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket reopen command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._can_manage_ticket(evt, ticket):
            await evt.reply("❌ You must be an admin or the ticket creator to reopen this ticket.")
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
            await self._notify_intake_room(ticket, notification_msg, mention_staff=False)
        else:
            await evt.reply("❌ Failed to reopen ticket.")
    
    @ticket_command.subcommand("note", help="Add a note to the ticket")
    @command.argument("text", label="note text", required=True, pass_raw=True)
    async def ticket_note(self, evt: MessageEvent, text: str) -> None:
        self.log.info(f"Ticket note command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        ticket = await self._ensure_ticket_room(evt)
        if not ticket:
            return
        if not await self._can_manage_ticket(evt, ticket):
            await evt.reply("❌ You must be an admin or the ticket creator to add notes.")
            return
        
        # Save note to database
        try:
            note_id = self.db.add_note(ticket["id"], evt.sender, text)
            self.log.info(f"Note added to ticket {ticket['ticket_number']} (note ID: {note_id}): {text}")
            await evt.reply(f"✅ Note added: {text}")
        except Exception as e:
            self.log.error(f"Failed to save note for ticket {ticket['id']}: {e}")
            await evt.reply("❌ Failed to save note. Please try again.")
    
    @ticket_command.subcommand("info", help="Show ticket information")
    async def ticket_info(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket info command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
    
    @ticket_command.subcommand("notes", help="Show all notes for this ticket")
    async def ticket_notes(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket notes command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
    
    @ticket_command.subcommand("delete", help="Delete a ticket (admin only)")
    @command.argument("ticket_number", label="TICKET_ID", required=False)
    async def ticket_delete(self, evt: MessageEvent, ticket_number: Optional[str] = None) -> None:
        self.log.info(f"Ticket delete command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
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
        await self._notify_intake_room(ticket, notification_msg, mention_staff=False)
        
        # Try to leave/clean up the ticket room (optional)
        try:
            # We could leave the room, but deleting might require admin privileges
            # For now, just log
            self.log.info(f"Ticket {ticket_num} deleted, room {ticket_room_id} remains")
        except Exception as e:
            self.log.warning(f"Note: Could not clean up ticket room {ticket_room_id}: {e}")
        
        await evt.reply(f"✅ Ticket **{ticket_num}** has been deleted.")
     
    @ticket_command.subcommand("debug", help="Debug command (admin only)")
    async def ticket_debug(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket debug command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        if not await self.ensure_admin(evt):
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
        """Handle pending ticket or intake room category selection."""
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
                    await self._notify_intake_room(ticket, plain_msg, mention_staff=False, html_message=html_msg)
                    
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
                    await self._notify_intake_room(ticket, plain_msg, mention_staff=False, html_message=html_msg)
                    
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
                "❌ No intake rooms are configured to receive notifications for this category.\n"
                "Please notify an administrator to add an intake room for category "
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
        """Create intake room from pending data (after category selection)."""
        room_id = pending["room_id"]
        category_id = pending["category_id"]
        
        # Generate a name based on category
        if category_id == "all":
            name = f"Intake Room (all categories)"
        else:
            # Try to get category name for display
            category = self.db.get_category(category_id)
            if category:
                name = f"Intake Room ({category['name']})"
            else:
                name = f"Intake Room ({category_id})"
        
        try:
            success = self.db.add_intake_room(room_id, name, category_id)
            if success:
                await evt.reply(
                    f"✅ This room has been registered as a support intake room with name: {name} "
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
                await evt.reply("❌ Failed to register intake room. It may already exist.")
        except Exception as e:
            self.log.error(f"Error creating intake room from pending: {e}")
            await evt.reply("❌ Failed to create intake room. Please try again later.")
    

    
    @ticket_command.subcommand("help", help="Show help for ticket commands")
    async def ticket_help(self, evt: MessageEvent) -> None:
        self.log.info(f"Ticket help command triggered: {evt.content.body}")
        
        # Block if in intake room (except allowed commands)
        if await self._block_if_intake_room(evt):
            return
        
        response = (
            "## Ticket System Commands:\n\n"
            "**Admin commands:** (require power level 100)\n"
            "- `!ticket admin intake` - Manage support intake rooms (add, remove, list, enable, disable)\n"
            "- `!ticket admin category` - Manage ticket categories\n"
            "- `!ticket search [filters]` - Search/filter tickets with clickable room links (moderator+)\n"
            "- `!ticket debug` - Show debug information\n"
            "- `!ticket delete [TICKET_ID]` - Delete a ticket (admin only, DM or ticket room)\n\n"
            "**User commands:** (direct message with bot only)\n"
            "- `!ticket create <title> | <description>` - Create a new ticket\n"
            "- `!ticket my` or `!ticket mine [status]` - List your tickets (optional: open, in_progress, closed, resolved, all)\n"
            "- `!ticket help` - Show this help\n\n"
             "**Ticket room and command room commands:** (some commands require specific room)\n"
             "- `!ticket assign TICKET-XXXX [@user]` - Assign ticket to a user (command room; self: moderator+, others: admin)\n"
             "- `!ticket unassign TICKET-XXXX [@user|all]` - Unassign ticket or user (command room; self: moderator+, others/all: admin)\n"
            "- `!ticket close` - Close the ticket (admin or creator)\n"
            "- `!ticket reopen` - Reopen a closed ticket (admin or creator)\n"
            "- `!ticket note <text>` - Add a note to the ticket\n"
            "- `!ticket info` - Show ticket information\n"
            "- `!ticket notes` - Show all notes for this ticket\n"
        )
        await evt.reply(response)