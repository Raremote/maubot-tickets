from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy import inspect, exc
from maubot.plugin_base import Plugin


class Database:
    """Database manager for the ticket system."""
    
    def __init__(self, engine: Engine, plugin: Plugin):
        self.engine = engine
        self.plugin = plugin
        
        self.metadata = sa.MetaData()
        
        # Support intake rooms table
        self.intake_rooms = sa.Table(
            "intake_rooms",
            self.metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("room_id", sa.String(255), nullable=False, unique=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("category_id", sa.String(100), nullable=True),
            sa.Column("space_id", sa.String(255), nullable=True),
            sa.Column("enabled", sa.Boolean, nullable=False, default=True),
            sa.Column("created_at", sa.DateTime, nullable=False, default=datetime.utcnow),
        )
        
        # Support command rooms table
        self.command_rooms = sa.Table(
            "command_rooms",
            self.metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("room_id", sa.String(255), nullable=False, unique=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False, default=True),
            sa.Column("space_id", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False, default=datetime.utcnow),
        )
        
        # Tickets table
        self.tickets = sa.Table(
            "tickets",
            self.metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("ticket_number", sa.String(50), nullable=False, unique=True),
            sa.Column("creator", sa.String(255), nullable=False),
            sa.Column("intake_room_id", sa.String(255), nullable=False),
            sa.Column("ticket_room_id", sa.String(255), nullable=False, unique=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("description", sa.Text, nullable=False),
            sa.Column("category_id", sa.String(100), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, default="open"),
            sa.Column("assignee", sa.String(255), nullable=True),
            sa.Column("ticket_space_id", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False, default=datetime.utcnow),
            sa.Column("updated_at", sa.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow),
        )
        
        # Ticket notes table
        self.notes = sa.Table(
            "ticket_notes",
            self.metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("ticket_id", sa.Integer, sa.ForeignKey("tickets.id"), nullable=False),
            sa.Column("author", sa.String(255), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False, default=datetime.utcnow),
        )
        # Categories table
        self.categories = sa.Table(
            "categories",
            self.metadata,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("category_id", sa.String(100), nullable=False, unique=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("enabled", sa.Boolean, nullable=False, default=True),
            sa.Column("created_at", sa.DateTime, nullable=False, default=datetime.utcnow),
        )
        
        # Create tables
        self.metadata.create_all(self.engine)
        
        # Migrate schema if needed
        self._migrate_schema()
    def _migrate_schema(self) -> None:
        """Migrate database schema to add missing columns."""
        inspector = inspect(self.engine)
        
        # Check intake_rooms table
        if 'intake_rooms' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('intake_rooms')]
            if 'category_id' not in columns:
                self.plugin.log.info("Adding category_id column to intake_rooms table")
                try:
                    with self.engine.begin() as conn:
                        # SQLite doesn't support IF NOT EXISTS for columns
                        conn.execute(sa.text('ALTER TABLE intake_rooms ADD COLUMN category_id VARCHAR(100)'))
                        self.plugin.log.info("Added category_id column to intake_rooms")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to add category_id column to intake_rooms: {e}")
        
        # Check tickets table
        if 'tickets' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('tickets')]
            if 'category_id' not in columns:
                self.plugin.log.info("Adding category_id column to tickets table")
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sa.text('ALTER TABLE tickets ADD COLUMN category_id VARCHAR(100)'))
                        self.plugin.log.info("Added category_id column to tickets")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to add category_id column to tickets: {e}")
            if 'command_room_id' in columns:
                self.plugin.log.info("Dropping command_room_id column from tickets table")
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sa.text('ALTER TABLE tickets DROP COLUMN command_room_id'))
                        self.plugin.log.info("Dropped command_room_id column from tickets")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to drop command_room_id column from tickets: {e}")
        
        # Remove category_id column from command_rooms (no longer used for association)
        if 'command_rooms' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('command_rooms')]
            if 'category_id' in columns:
                self.plugin.log.info("Attempting to drop category_id column from command_rooms table")
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sa.text('ALTER TABLE command_rooms DROP COLUMN category_id'))
                        self.plugin.log.info("Dropped category_id column from command_rooms")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to drop category_id column from command_rooms: {e}")
                    # Fallback: clear category_id values
                    try:
                        with self.engine.begin() as conn:
                            conn.execute(sa.text('UPDATE command_rooms SET category_id = NULL WHERE category_id IS NOT NULL'))
                            self.plugin.log.info("Cleared category_id values from command_rooms")
                    except Exception as e2:
                        self.plugin.log.warning(f"Failed to clear category_id values: {e2}")
        
        # Add space_id column to command_rooms if missing
        if 'command_rooms' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('command_rooms')]
            if 'space_id' not in columns:
                self.plugin.log.info("Adding space_id column to command_rooms table")
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sa.text('ALTER TABLE command_rooms ADD COLUMN space_id VARCHAR(255)'))
                        self.plugin.log.info("Added space_id column to command_rooms")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to add space_id column to command_rooms: {e}")
        
        # Add space_id column to intake_rooms if missing
        if 'intake_rooms' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('intake_rooms')]
            if 'space_id' not in columns:
                self.plugin.log.info("Adding space_id column to intake_rooms table")
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sa.text('ALTER TABLE intake_rooms ADD COLUMN space_id VARCHAR(255)'))
                        self.plugin.log.info("Added space_id column to intake_rooms")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to add space_id column to intake_rooms: {e}")
        
        # Add ticket_space_id column to tickets if missing
        if 'tickets' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('tickets')]
            if 'ticket_space_id' not in columns:
                self.plugin.log.info("Adding ticket_space_id column to tickets table")
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sa.text('ALTER TABLE tickets ADD COLUMN ticket_space_id VARCHAR(255)'))
                        self.plugin.log.info("Added ticket_space_id column to tickets")
                except Exception as e:
                    self.plugin.log.warning(f"Failed to add ticket_space_id column to tickets: {e}")
    
    # Support intake room methods
    def add_intake_room(self, room_id: str, name: str, category_id: Optional[str] = None) -> bool:
        """Add a new support intake room."""
        with self.engine.begin() as conn:
            # Check if room already exists
            stmt = sa.select([self.intake_rooms]).where(self.intake_rooms.c.room_id == room_id)
            existing = conn.execute(stmt).fetchone()
            if existing:
                return False
            
            
            conn.execute(
                self.intake_rooms.insert().values(room_id=room_id, name=name, category_id=category_id, enabled=True)
            )
            return True
    
    def remove_intake_room(self, room_id: str) -> bool:
        """Remove a support intake room."""
        with self.engine.begin() as conn:
            stmt = self.intake_rooms.delete().where(self.intake_rooms.c.room_id == room_id)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def remove_all_intake_rooms(self) -> int:
        """Remove all intake rooms."""
        with self.engine.begin() as conn:
            stmt = self.intake_rooms.delete()
            result = conn.execute(stmt)
            self.plugin.log.info(f"remove_all_intake_rooms: removed {result.rowcount} intake rooms")
            return result.rowcount
    
    def get_intake_room(self, room_id: str) -> Optional[Dict[str, Any]]:
        """Get intake room by room ID."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.intake_rooms]).where(self.intake_rooms.c.room_id == room_id)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_intake_room_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get intake room by name."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.intake_rooms]).where(self.intake_rooms.c.name == name)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_all_intake_rooms(self) -> List[Dict[str, Any]]:
        """Get all intake rooms."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.intake_rooms]).order_by(self.intake_rooms.c.name)
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def get_intake_rooms_for_category(self, category_id: str) -> List[Dict[str, Any]]:
        """Get intake rooms that should receive notifications for a given category.
        
        Returns intake rooms where category_id matches the given category_id,
        or category_id is 'all', or category_id is NULL (treat NULL as 'all').
        """
        with self.engine.begin() as conn:
            stmt = sa.select([self.intake_rooms]).where(
                sa.and_(
                    sa.or_(
                        self.intake_rooms.c.category_id == category_id,
                        self.intake_rooms.c.category_id == "all",
                        self.intake_rooms.c.category_id.is_(None)
                    ),
                    self.intake_rooms.c.enabled == True
                )
            ).order_by(self.intake_rooms.c.name)
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def set_intake_room_enabled(self, room_id: str, enabled: bool) -> bool:
        """Enable or disable an intake room."""
        with self.engine.begin() as conn:
            stmt = self.intake_rooms.update().where(self.intake_rooms.c.room_id == room_id).values(enabled=enabled)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def set_intake_room_space(self, room_id: str, space_id: Optional[str]) -> bool:
        """Set the space ID for an intake room."""
        with self.engine.begin() as conn:
            stmt = self.intake_rooms.update().where(self.intake_rooms.c.room_id == room_id).values(space_id=space_id)
            result = conn.execute(stmt)
            if result.rowcount > 0:
                self.plugin.log.info(f"set_intake_room_space: set space {space_id} for intake room {room_id}")
            return result.rowcount > 0
    
    def get_intake_room_space(self, room_id: str) -> Optional[str]:
        """Get the space ID for a specific intake room."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.intake_rooms.c.space_id]).where(self.intake_rooms.c.room_id == room_id)
            row = conn.execute(stmt).fetchone()
            return row[0] if row else None
    
    def clear_all_intake_room_spaces(self) -> int:
        """Clear space_id from all intake rooms."""
        with self.engine.begin() as conn:
            stmt = self.intake_rooms.update().values(space_id=None)
            result = conn.execute(stmt)
            self.plugin.log.info(f"clear_all_intake_room_spaces: cleared spaces from {result.rowcount} intake rooms")
            return result.rowcount
    
    # Command room methods
    def add_command_room(self, room_id: str, name: str) -> bool:
        """Add a new command room."""
        with self.engine.begin() as conn:
            # Check if room already exists
            stmt = sa.select([self.command_rooms]).where(self.command_rooms.c.room_id == room_id)
            existing = conn.execute(stmt).fetchone()
            if existing:
                return False
            
            conn.execute(
                self.command_rooms.insert().values(room_id=room_id, name=name, enabled=True)
            )
            return True
    
    def remove_command_room(self, room_id: str) -> bool:
        """Remove a command room."""
        with self.engine.begin() as conn:
            stmt = self.command_rooms.delete().where(self.command_rooms.c.room_id == room_id)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def remove_all_command_rooms(self) -> int:
        """Remove all command rooms."""
        with self.engine.begin() as conn:
            stmt = self.command_rooms.delete()
            result = conn.execute(stmt)
            self.plugin.log.info(f"remove_all_command_rooms: removed {result.rowcount} command rooms")
            return result.rowcount
    
    def get_command_room(self, room_id: str) -> Optional[Dict[str, Any]]:
        """Get command room by room ID."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.command_rooms]).where(self.command_rooms.c.room_id == room_id)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_command_room_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get command room by name."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.command_rooms]).where(self.command_rooms.c.name == name)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_all_command_rooms(self) -> List[Dict[str, Any]]:
        """Get all command rooms."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.command_rooms]).order_by(self.command_rooms.c.name)
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    

    def set_command_room_enabled(self, room_id: str, enabled: bool) -> bool:
        """Enable or disable a command room."""
        with self.engine.begin() as conn:
            stmt = self.command_rooms.update().where(self.command_rooms.c.room_id == room_id).values(enabled=enabled)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def set_command_room_space(self, room_id: str, space_id: Optional[str]) -> bool:
        """Set the space ID for a command room."""
        with self.engine.begin() as conn:
            stmt = self.command_rooms.update().where(self.command_rooms.c.room_id == room_id).values(space_id=space_id)
            result = conn.execute(stmt)
            if result.rowcount > 0:
                self.plugin.log.info(f"set_command_room_space: set space {space_id} for command room {room_id}")
            return result.rowcount > 0
    
    def get_command_room_space(self, room_id: str) -> Optional[str]:
        """Get the space ID for a specific command room."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.command_rooms.c.space_id]).where(self.command_rooms.c.room_id == room_id)
            row = conn.execute(stmt).fetchone()
            return row[0] if row else None
    
    def clear_all_command_room_spaces(self) -> int:
        """Clear space_id from all command rooms (for rooms that no longer exist)."""
        with self.engine.begin() as conn:
            stmt = self.command_rooms.update().values(space_id=None)
            result = conn.execute(stmt)
            self.plugin.log.info(f"clear_all_command_room_spaces: cleared spaces from {result.rowcount} command rooms")
            return result.rowcount
    
    def get_ticket_space_id(self) -> Optional[str]:
        """Get the ticket space ID from any enabled command room."""
        with self.engine.begin() as conn:
            # First check how many command rooms have spaces configured
            count_stmt = sa.select([sa.func.count()]).where(
                sa.and_(
                    self.command_rooms.c.enabled == True,
                    self.command_rooms.c.space_id.isnot(None)
                )
            )
            count_result = conn.execute(count_stmt).fetchone()
            space_count = count_result[0] if count_result else 0
            
            if space_count > 1:
                self.plugin.log.warning(f"get_ticket_space_id: {space_count} command rooms have spaces configured, selecting most recently created")
                self.plugin.log.warning(f"To avoid confusion, consider clearing space configuration from unused command rooms with: !ticket admin command space clear (or clear all to clear from all command rooms)")
                # List all spaces for debugging with creation dates
                list_stmt = sa.select([self.command_rooms.c.space_id, self.command_rooms.c.room_id, self.command_rooms.c.created_at]).where(
                    sa.and_(
                        self.command_rooms.c.enabled == True,
                        self.command_rooms.c.space_id.isnot(None)
                    )
                ).order_by(self.command_rooms.c.created_at.desc())
                rows = conn.execute(list_stmt).fetchall()
                for space_id, room_id, created_at in rows:
                    self.plugin.log.warning(f"  - Command room {room_id}: space {space_id} (created: {created_at})")
            
            stmt = sa.select([self.command_rooms.c.space_id, self.command_rooms.c.room_id]).where(
                sa.and_(
                    self.command_rooms.c.enabled == True,
                    self.command_rooms.c.space_id.isnot(None)
                )
            ).order_by(self.command_rooms.c.created_at.desc()).limit(1)
            row = conn.execute(stmt).fetchone()
            if row:
                space_id, room_id = row
                self.plugin.log.info(f"get_ticket_space_id: returning space {space_id} from command room {room_id}")
                return space_id
            self.plugin.log.info("get_ticket_space_id: no space configured")
            return None
    
    # Category methods
    def add_category(self, category_id: str, name: str, description: Optional[str] = None) -> bool:
        """Add a new category."""
        if category_id == "all":
            return False
        with self.engine.begin() as conn:
            # Check if category already exists
            stmt = sa.select([self.categories]).where(self.categories.c.category_id == category_id)
            existing = conn.execute(stmt).fetchone()
            if existing:
                return False
            conn.execute(
                self.categories.insert().values(
                    category_id=category_id,
                    name=name,
                    description=description,
                    enabled=True
                )
            )
            return True

    def remove_category(self, category_id: str) -> bool:
        """Remove a category."""
        with self.engine.begin() as conn:
            stmt = self.categories.delete().where(self.categories.c.category_id == category_id)
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_category(self, category_id: str) -> Optional[Dict[str, Any]]:
        """Get category by ID."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.categories]).where(self.categories.c.category_id == category_id)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None

    def get_all_categories(self) -> List[Dict[str, Any]]:
        """Get all categories."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.categories]).order_by(self.categories.c.name)
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]

    def category_exists(self, category_id: str) -> bool:
        """Check if a category exists."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.categories]).where(self.categories.c.category_id == category_id)
            row = conn.execute(stmt).fetchone()
            return row is not None

    # Ticket methods
    def create_ticket(
        self,
        ticket_number: str,
        creator: str,
        intake_room_id: str,
        ticket_room_id: str,
        title: str,
        description: str,
        category_id: Optional[str] = None,
        ticket_space_id: Optional[str] = None
    ) -> int:
        """Create a new ticket."""
        with self.engine.begin() as conn:
            stmt = self.tickets.insert().values(
                ticket_number=ticket_number,
                creator=creator,
                intake_room_id=intake_room_id,
                ticket_room_id=ticket_room_id,
                title=title,
                description=description,
                category_id=category_id,
                ticket_space_id=ticket_space_id,
                status="open"
            )
            result = conn.execute(stmt)
            if result.inserted_primary_key:
                return result.inserted_primary_key[0]
            raise Exception("Failed to get inserted ticket ID")
    
    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        """Get ticket by ID."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets]).where(self.tickets.c.id == ticket_id)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_ticket_by_number(self, ticket_number: str) -> Optional[Dict[str, Any]]:
        """Get ticket by ticket number."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets]).where(self.tickets.c.ticket_number == ticket_number)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_ticket_by_room(self, ticket_room_id: str) -> Optional[Dict[str, Any]]:
        """Get ticket by ticket room ID."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets]).where(self.tickets.c.ticket_room_id == ticket_room_id)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_tickets_by_creator(self, creator: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get tickets created by a user, optionally filtered by status."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets]).where(self.tickets.c.creator == creator)
            if status:
                stmt = stmt.where(self.tickets.c.status == status)
            stmt = stmt.order_by(self.tickets.c.created_at.desc())
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def get_tickets_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all tickets with a specific status."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets]).where(self.tickets.c.status == status).order_by(self.tickets.c.created_at.desc())
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def get_all_tickets(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all tickets."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets]).order_by(self.tickets.c.created_at.desc()).limit(limit)
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def search_tickets(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        creator: Optional[str] = None,
        category_id: Optional[str] = None,
        search_term: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Search tickets with various filters."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets])
            
            # Apply filters
            if status:
                stmt = stmt.where(self.tickets.c.status == status)
            if assignee:
                # Search for assignee in comma-separated list
                # Handle NULL assignee column
                stmt = stmt.where(self.tickets.c.assignee.isnot(None))
                # Use LIKE with wildcards to match within comma-separated string
                # Note: assignee strings don't contain wildcards
                pattern = f"%{assignee}%"
                stmt = stmt.where(self.tickets.c.assignee.like(pattern))
            if creator:
                stmt = stmt.where(self.tickets.c.creator == creator)
            if category_id:
                stmt = stmt.where(self.tickets.c.category_id == category_id)
            if search_term:
                # Search in title and description
                search_pattern = f"%{search_term}%"
                stmt = stmt.where(
                    sa.or_(
                        self.tickets.c.title.ilike(search_pattern),
                        self.tickets.c.description.ilike(search_pattern),
                        self.tickets.c.ticket_number.ilike(search_pattern)
                    )
                )
            
            # Order by creation date (newest first) and limit
            stmt = stmt.order_by(self.tickets.c.created_at.desc()).limit(limit)
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def get_open_tickets_older_than(self, hours: float) -> List[Dict[str, Any]]:
        """Get open tickets older than specified hours."""
        with self.engine.begin() as conn:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)
            stmt = sa.select([self.tickets]).where(
                sa.and_(
                    self.tickets.c.status == "open",
                    self.tickets.c.created_at < cutoff_time
                )
            ).order_by(self.tickets.c.created_at.asc())
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def auto_close_old_tickets(self, hours: float) -> int:
        """Automatically close open tickets older than specified hours.
        
        Returns:
            Number of tickets closed.
        """
        tickets = self.get_open_tickets_older_than(hours)
        closed_count = 0
        for ticket in tickets:
            if self.close_ticket(ticket["id"]):
                closed_count += 1
        return closed_count
    
    def update_ticket_status(self, ticket_id: int, status: str) -> bool:
        """Update ticket status."""
        with self.engine.begin() as conn:
            stmt = self.tickets.update().where(self.tickets.c.id == ticket_id).values(status=status)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def update_ticket_space_id(self, ticket_id: int, space_id: Optional[str]) -> bool:
        """Update ticket space ID."""
        with self.engine.begin() as conn:
            stmt = self.tickets.update().where(self.tickets.c.id == ticket_id).values(ticket_space_id=space_id)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def _parse_assignees(self, assignee_str: Optional[str]) -> List[str]:
        """Parse comma-separated assignee string into list."""
        if not assignee_str:
            return []
        # Split by comma and strip whitespace
        return [a.strip() for a in assignee_str.split(",") if a.strip()]

    def _format_assignees(self, assignees: List[str]) -> Optional[str]:
        """Format list of assignees into comma-separated string."""
        if not assignees:
            return None
        return ",".join(assignees)

    def get_assignees(self, ticket_id: int) -> List[str]:
        """Get list of assignees for a ticket."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets.c.assignee]).where(self.tickets.c.id == ticket_id)
            result = conn.execute(stmt).fetchone()
            current_assignee_str = result[0] if result else None
            return self._parse_assignees(current_assignee_str)

    def add_assignee(self, ticket_id: int, assignee: str) -> bool:
        """Add an assignee to a ticket if not already assigned."""
        with self.engine.begin() as conn:
            # Get current assignees
            stmt = sa.select([self.tickets.c.assignee]).where(self.tickets.c.id == ticket_id)
            result = conn.execute(stmt).fetchone()
            current_assignee_str = result[0] if result else None
            assignees = self._parse_assignees(current_assignee_str)
            
            if assignee in assignees:
                return False  # Already assigned
            
            assignees.append(assignee)
            new_assignee_str = self._format_assignees(assignees)
            
            # Update ticket with new assignee list and set status to in_progress
            update_stmt = self.tickets.update().where(self.tickets.c.id == ticket_id).values(
                assignee=new_assignee_str,
                status="in_progress"
            )
            result = conn.execute(update_stmt)
            return result.rowcount > 0

    def remove_assignee(self, ticket_id: int, assignee: str) -> bool:
        """Remove a specific assignee from a ticket."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.tickets.c.assignee]).where(self.tickets.c.id == ticket_id)
            result = conn.execute(stmt).fetchone()
            current_assignee_str = result[0] if result else None
            assignees = self._parse_assignees(current_assignee_str)
            
            if assignee not in assignees:
                return False  # Not assigned
            
            assignees.remove(assignee)
            new_assignee_str = self._format_assignees(assignees)
            
            # Determine new status: open if no assignees, otherwise in_progress
            new_status = "open" if not assignees else "in_progress"
            update_stmt = self.tickets.update().where(self.tickets.c.id == ticket_id).values(
                assignee=new_assignee_str,
                status=new_status
            )
            result = conn.execute(update_stmt)
            return result.rowcount > 0

    def assign_ticket(self, ticket_id: int, assignee: str) -> bool:
        """Assign a ticket to a user (adds to assignee list)."""
        return self.add_assignee(ticket_id, assignee)

    def unassign_ticket(self, ticket_id: int) -> bool:
        """Unassign all assignees from a ticket."""
        with self.engine.begin() as conn:
            # Set assignee to None and status to open
            stmt = self.tickets.update().where(self.tickets.c.id == ticket_id).values(
                assignee=None,
                status="open"
            )
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def close_ticket(self, ticket_id: int) -> bool:
        """Close a ticket."""
        with self.engine.begin() as conn:
            stmt = self.tickets.update().where(self.tickets.c.id == ticket_id).values(status="closed")
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def delete_ticket(self, ticket_id: int) -> bool:
        """Delete a ticket."""
        with self.engine.begin() as conn:
            stmt = self.tickets.delete().where(self.tickets.c.id == ticket_id)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    # Ticket note methods
    def add_note(self, ticket_id: int, author: str, content: str) -> int:
        """Add a note to a ticket."""
        with self.engine.begin() as conn:
            stmt = self.notes.insert().values(ticket_id=ticket_id, author=author, content=content)
            result = conn.execute(stmt)
            if result.inserted_primary_key:
                return result.inserted_primary_key[0]
            raise Exception("Failed to get inserted note ID")
    
    def get_note(self, note_id: int) -> Optional[Dict[str, Any]]:
        """Get a note by ID."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.notes]).where(self.notes.c.id == note_id)
            row = conn.execute(stmt).fetchone()
            return dict(row) if row else None
    
    def get_notes_for_ticket(self, ticket_id: int) -> List[Dict[str, Any]]:
        """Get all notes for a ticket."""
        with self.engine.begin() as conn:
            stmt = sa.select([self.notes]).where(self.notes.c.ticket_id == ticket_id).order_by(self.notes.c.created_at.asc())
            rows = conn.execute(stmt).fetchall()
            return [dict(row) for row in rows]
    
    def delete_note(self, note_id: int) -> bool:
        """Delete a note."""
        with self.engine.begin() as conn:
            stmt = self.notes.delete().where(self.notes.c.id == note_id)
            result = conn.execute(stmt)
            return result.rowcount > 0
    
    def get_next_ticket_number(self) -> str:
        """Generate the next ticket number (e.g., TICKET-001, TICKET-002)."""
        with self.engine.begin() as conn:
            # Get the highest ticket number
            result = conn.execute(sa.text("SELECT MAX(id) FROM tickets")).fetchone()
            if result is None or result[0] is None:
                next_id = 1
            else:
                next_id = result[0] + 1
            return f"TICKET-{next_id:04d}"