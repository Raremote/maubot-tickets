# Matrix Ticket System Plugin

## Overview

The Matrix Ticket System Plugin is a comprehensive support ticket management solution for Matrix communities. It provides a structured workflow for users to create support tickets, staff to manage them, and administrators to configure the system. Each ticket gets its own dedicated room with appropriate permissions, and the system supports categorization, assignment, notes, and integration with Matrix spaces.

## Features

### Core Functionality
- **Dedicated Ticket Rooms**: Each ticket creates a private room with appropriate permissions
- **Ticket Categorization**: Support for multiple ticket categories with filtering
- **Staff Assignment**: Tickets can be assigned to staff members
- **Ticket Notes**: Internal notes for staff communication
- **Status Tracking**: Open, in-progress, closed, resolved states
- **Automatic Cleanup**: Auto-closes old open tickets (runs every 30 minutes, closes tickets older than 3 hours)

### Room Type Management
- **New Ticket Notification (NTN) Rooms**: Dedicated rooms for receiving new ticket notifications
- **Command Rooms**: Designated rooms where staff can execute ticket management commands
- **Ticket Rooms**: Individual rooms created for each support ticket

### Advanced Features
- **Matrix Space Integration**: Ticket rooms can be automatically added to parent spaces
- **Power Level Management**: Automatic permission configuration in ticket rooms
- **Direct Message Detection**: Intelligent detection of user-to-bot direct messages
- **Comprehensive Search**: Filter tickets by status, assignee, creator, category, or text
- **Role-Based Access Control**: Different command permissions for users, staff, and administrators

## Installation

### Prerequisites
- A running [Maubot](https://github.com/maubot/maubot) instance
- Python 3.7+ and SQLite (or other supported database via SQLAlchemy)

### Building the Plugin

1. **Clone the repository**:
   ```bash
   git clone https://github.com/raremote/maubot-tickets.git
   cd maubot-tickets
   ```

2. **Build using mbc**:
   ```bash
   mbc build
   ```
   This creates a `.mbp` file in the current directory.

### Installation via Maubot Client

1. **Upload the plugin**:
   - Open your Maubot client web interface
   - Navigate to the plugins section
   - Click "Upload plugin" and select the `.mbp` file

2. **Create an instance**:
   - Click "Create instance" on the uploaded plugin
   - Configure the database connection (SQLite is recommended for simplicity)
   - Start the instance

### Alternative: Manual Installation
```bash
# Copy the built plugin to your maubot instance
cp me.raremote.tickets-v1.0.0.mbp /path/to/maubot/plugins/
```

## Configuration

### Initial Setup

After installation, you need to configure three types of rooms:

1. **New Ticket Notification (NTN) Rooms**:
   - These rooms receive notifications when new tickets are created
   - Configure with `!ticket ntn_room add` (admin only)

2. **Command Rooms**:
   - These rooms allow staff to execute ticket management commands
   - Configure with `!ticket command_room add` (admin only)

3. **Ticket Categories**:
   - Define categories for organizing tickets
   - Configure with `!ticket category add <id> <name> [description]` (admin only)

### Database

The plugin uses SQLAlchemy and supports multiple database backends:
- **SQLite** (default, recommended for most deployments)
- **PostgreSQL**
- **MySQL**

Database tables are automatically created on first run.

## Command Reference

### Command Context Rules
- **Direct Messages**: User commands (`create`, `my`) work only in DMs with the bot
- **Command Rooms**: Staff commands (`assign`, `unassign`, `search`) work only in enabled command rooms
- **Ticket Rooms**: Ticket-specific commands (`close`, `note`, `info`) work only in ticket rooms
- **NTN Rooms**: NTN management commands work in NTN rooms or regular rooms
- **Any Room**: Help command works anywhere

### Administrator Commands (Power Level ≥ 100)
| Command | Description | Context |
|---------|-------------|---------|
| `!ticket ntn_room` | Manage New Ticket Notification rooms | NTN rooms or regular rooms |
| `!ticket command_room` | Manage command rooms | Regular rooms |
| `!ticket category` | Manage ticket categories | Regular rooms |
| `!ticket delete [TICKET_ID]` | Delete a ticket | In ticket room: no args (admin in room)<br>In DM: with ticket number (admin in any command room) |

#### NTN Room Subcommands
- `add` - Add current room as a New Ticket Notification room
- `remove [all]` - Remove current (or all) NTN rooms
- `list` - List all NTN rooms (works in command rooms too)
- `enable` - Enable current NTN room
- `disable` - Disable current NTN room
- `tris [set\|unset\|unset all\|debug\|fix]` - Configure Ticket Room Intake Space (space where new ticket rooms are added)

### Staff Commands (Power Level ≥ 50)
| Command | Description | Context |
|---------|-------------|---------|
| `!ticket debug` | Show debug information (database stats, schema) | Any room (except NTN rooms) |
| `!ticket search [filters]` | Search/filter tickets with clickable room links | Command rooms |

#### Search Filters
- `status=open\|in_progress\|closed\|resolved`
- `assignee=@user`
- `creator=@user`
- `category=id`
- `search=text`

### Command Room Commands (Require Enabled Command Room)
| Command | Description | Permissions |
|---------|-------------|-------------|
| `!ticket assign TICKET-XXXX [@user]` | Assign ticket | Self (moderator+) or specific user (admin) |
| `!ticket unassign TICKET-XXXX [@user\|all]` | Unassign ticket | Self (moderator+), specific user (admin), or all (admin) |

### Ticket Management Commands (Must be in Ticket Room)
| Command | Description | Permissions |
|---------|-------------|-------------|
| `!ticket close` | Close the ticket | Admin/moderator/creator |
| `!ticket resolve` | Resolve the ticket | Admin/moderator/creator |
| `!ticket reopen` | Reopen a closed ticket | Admin/moderator only |
| `!ticket note <text>` | Add a note to the ticket | Admin/moderator only |

### Ticket Information Commands (Must be in Ticket Room)
| Command | Description | Permissions |
|---------|-------------|-------------|
| `!ticket info` | Show ticket information | Any participant |
| `!ticket notes` | Show all notes for this ticket | Any participant |

### User Commands (Direct Message with Bot Only)
| Command | Description |
|---------|-------------|
| `!ticket create <title> \| <description>` | Create a new support ticket |
| `!ticket my [status]` | List your tickets (status: open, in_progress, closed, resolved, all) |
| `!ticket help` | Show help (works anywhere) |

## Direct Message Detection

The plugin uses a heuristic to determine if a room is a direct message between a user and the bot:

### Detection Logic
1. **Exclusion Checks**: Rooms are NOT considered DMs if they are:
   - Ticket rooms
   - New Ticket Notification rooms  
   - Command rooms

2. **Member Count**:
   - If room has >2 members → NOT DM (group chat)
   - If room has exactly 2 members (bot + sender):
     - If user is admin (power level ≥ 100) → NOT DM (treated as regular admin room)
     - If user not admin:
       - Check room create event for `is_direct` flag → DM if true
       - Check `m.direct` account data → DM if marked
       - Default to DM (safe assumption)
   - Otherwise → NOT DM

### Rationale
Matrix doesn't provide a definitive API to detect direct messages. The plugin uses a heuristic that represents the best available approach given Matrix's API limitations:

- **Matrix API Limitations**: There's no definitive `is_direct_message` API in Matrix
- **Admin Room Distinction**: Rooms created for bot administration (often with admin users) are treated as regular rooms
- **Member Count as Primary Signal**: Rooms with >2 members are always group chats
- **Two-Member Room Ambiguity**: Rooms with exactly 2 members (bot + user) require additional checks:
  - Admin power level check to exclude admin configuration rooms
  - `is_direct` flag in room creation event (when available)
  - `m.direct` account data (official Matrix DM markers)
- **Conservative Fallback**: When indicators are ambiguous, two-member non-admin rooms are treated as DMs to ensure users can create tickets (As long as one of those members are the Ticket Bot)

## Room Types

### 1. New Ticket Notification (NTN) Rooms
- **Purpose**: Receive notifications when users create new tickets
- **Configuration**: One or more rooms can be NTN rooms
- **Commands**: Only NTN management commands and help are allowed
- **Notifications**: Include ticket details and clickable room links

### 2. Command Rooms
- **Purpose**: Execute staff ticket management commands
- **Configuration**: Must be explicitly enabled as command rooms
- **Commands**: Staff commands (`assign`, `unassign`, `search`, `debug`)
- **Restrictions**: NTN management commands are blocked (except `list`)

### 3. Ticket Rooms
- **Purpose**: Dedicated room for each support ticket
- **Creation**: Automatically created when users create tickets
- **Permissions**:
  - Creator: Invited with appropriate permissions
  - Staff: Can be assigned and gain moderator permissions
  - Bot: Maintains administrative control
- **Features**: Knock/invite join rules, space integration, power level management

## Database Schema

### Tables
1. **`intake_rooms`** (NTN rooms)
   - `room_id`, `name`, `category_id`, `space_id`, `enabled`

2. **`command_rooms`**
   - `room_id`, `name`, `enabled`

3. **`tickets`**
   - `ticket_number`, `creator`, `ticket_room_id`, `title`, `description`
   - `category_id`, `status`, `assignee`, `ticket_space_id`

4. **`ticket_notes`**
   - `ticket_id`, `author`, `content`

5. **`categories`**
   - `category_id`, `name`, `description`, `enabled`

### Automatic Schema Migration
The plugin includes automatic schema migration for backward compatibility.

## Development

### Project Structure
```
maubot-tickets/
├── maubot.yaml          # Plugin metadata
├── tickets/             # Main plugin module
│   ├── __init__.py      # Plugin export
│   ├── tickets.py       # Main plugin class
│   ├── databases.py     # Database models and operations
│   └── listeners/       # Modular command handlers
│       ├── __init__.py  # Combined TicketsHandler
│       ├── base.py      # Base handler with shared functionality
│       ├── admin.py     # Administrator commands
│       ├── staff.py     # Staff commands
│       ├── user.py      # User DM commands
│       ├── ticket_room.py # Ticket room commands
│       ├── events.py    # Event handlers
│       └── notifications.py # Notification handlers
└── .github/workflows/   # CI/CD pipelines
```

### Building and Testing
```bash
# Install development dependencies
pip install maubot

# Build plugin
mbc build

# Run tests (if available)
python -m pytest tests/
```

### Modular Architecture
The plugin uses a modular handler architecture:
- **Base Handler** (`TicketsHandlerBase`): Core functionality and shared methods
- **Specialized Handlers**: Inherit from base, handle specific command categories
- **Combined Handler** (`TicketsHandler`): Inherits from all specialized handlers

This architecture improves maintainability and allows for easier extension.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with appropriate tests
4. Ensure code follows existing style
5. Submit a pull request

## License

GNU General Public License v3.0 (GPLv3) - see LICENSE file for details.

## Support

- **Issue Tracker**: [GitHub Issues](https://github.com/raremote/maubot-tickets/issues)

---

*Note: This plugin is under active development. Features and commands may evolve.*
