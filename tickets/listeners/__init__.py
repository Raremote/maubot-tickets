"""
Tickets handler modules combined into a single class.
"""
from .base import TicketsHandlerBase
from .admin import TicketsHandlerAdmin
from .staff import TicketsHandlerStaff
from .ticket_room import TicketsHandlerTicketRoom
from .user import TicketsHandlerUser
from .events import TicketsHandlerEvents
from .notifications import TicketsHandlerNotifications


class TicketsHandler(
    TicketsHandlerAdmin,
    TicketsHandlerStaff,
    TicketsHandlerTicketRoom,
    TicketsHandlerUser,
    TicketsHandlerEvents,
    TicketsHandlerNotifications,
):
    """
    Combined tickets handler class that includes all functionality.
    This class inherits from all modular handler classes.
    """
    pass