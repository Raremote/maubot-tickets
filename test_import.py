import sys
sys.path.insert(0, '.')

try:
    from tickets.listener import TicketsHandler
    print("✓ TicketsHandler imported successfully")
except Exception as e:
    print(f"✗ Failed to import TicketsHandler: {e}")

try:
    from tickets.databases import Database
    print("✓ Database imported successfully")
except Exception as e:
    print(f"✗ Failed to import Database: {e}")

try:
    from tickets.tickets import TicketsPlugin
    print("✓ TicketsPlugin imported successfully")
except Exception as e:
    print(f"✗ Failed to import TicketsPlugin: {e}")