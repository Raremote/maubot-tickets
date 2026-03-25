import asyncio
from typing import Optional
from maubot import Plugin
from .databases import Database
from .listener import TicketsHandler


class TicketsPlugin(Plugin):
    """Main plugin class for the ticket system."""
    
    db: Database
    _auto_close_task: Optional[asyncio.Task] = None
    
    async def _run_auto_close(self) -> None:
        """Background task to automatically close old open tickets."""
        self.log.info("Auto-close task started")
        try:
            while True:
                # Wait for the interval
                await asyncio.sleep(30 * 60)  # 30 minutes
                
                try:
                    # Close tickets older than 3 hours
                    closed_count = self.db.auto_close_old_tickets(hours=3)
                    if closed_count > 0:
                        self.log.info(f"Auto-closed {closed_count} open ticket(s) older than 3 hours")
                    # else: log debug maybe
                except Exception as e:
                    self.log.error(f"Error in auto-close task: {e}")
        except asyncio.CancelledError:
            self.log.info("Auto-close task cancelled")
            raise
        except Exception as e:
            self.log.error(f"Auto-close task failed: {e}")
    
    async def start(self) -> None:
        """Initialize the plugin."""
        self.log.info("Starting Ticket System plugin...")
        
        # Initialize database
        self.db = Database(self.database, self)
        self.log.info("Database initialized")
        
        # Register command and event handlers
        self.register_handler_class(TicketsHandler(self.db, self))
        
        # Start background task for auto-closing old tickets
        self._auto_close_task = asyncio.create_task(self._run_auto_close())
        self.log.info("Auto-close task scheduled (runs every 30 minutes)")
        
        self.log.info("Ticket System plugin started successfully")
    
    async def stop(self) -> None:
        """Clean up plugin resources."""
        self.log.info("Stopping Ticket System plugin...")
        
        # Cancel auto-close background task
        if self._auto_close_task and not self._auto_close_task.done():
            self._auto_close_task.cancel()
            try:
                await self._auto_close_task
            except asyncio.CancelledError:
                pass
            self.log.info("Auto-close task cancelled")