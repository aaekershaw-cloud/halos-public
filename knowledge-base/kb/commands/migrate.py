"""Database migration command"""

import logging
from kb.db import apply_migrations, get_schema_version

logger = logging.getLogger(__name__)


def cmd_migrate():
    """Run pending database migrations"""
    print("Checking for pending migrations...\n")

    version_before = get_schema_version()
    print(f"Current schema version: {version_before}")

    try:
        apply_migrations()
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        raise

    version_after = get_schema_version()

    if version_after > version_before:
        print(f"\n✓ Migrations applied successfully")
        print(f"Schema version: {version_before} → {version_after}")
    else:
        print(f"\n✓ No pending migrations")
