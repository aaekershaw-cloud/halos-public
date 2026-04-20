"""Backup and restore commands"""

import os
import glob
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from kb.config import get_project_root, get_db_path
from kb.db import get_connection

logger = logging.getLogger(__name__)


def cmd_backup(max_backups: int = 10):
    """
    Create timestamped SQLite backup and enforce rotation policy.

    Args:
        max_backups: Maximum number of backups to keep (default 10)
    """
    root = get_project_root()
    backup_dir = root / '.kb' / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Create backup filename
    timestamp = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    backup_path = backup_dir / f"kb-{timestamp}.sql"

    print(f"Creating backup: {backup_path}")

    # Dump database to SQL
    conn = get_connection()
    with open(backup_path, 'w') as f:
        for line in conn.iterdump():
            f.write(f'{line}\n')

    print(f"✓ Backup created: {backup_path}")

    # Enforce rotation - keep only N most recent backups
    enforce_backup_rotation(backup_dir, max_backups)


def enforce_backup_rotation(backup_dir: Path, max_backups: int):
    """Delete old backups, keep only N most recent"""
    backups = sorted(backup_dir.glob('kb-*.sql'))

    if len(backups) > max_backups:
        old_backups = backups[:-max_backups]
        for old_backup in old_backups:
            os.remove(old_backup)
            print(f"Deleted old backup: {old_backup.name}")


def cmd_restore(backup_path: str, dry_run: bool = False):
    """
    Restore from SQL backup with validation.

    Args:
        backup_path: Path to .sql backup file
        dry_run: If True, validate but don't apply
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    print(f"Validating backup: {backup_path}")

    if dry_run:
        # Test restore to temporary database
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            tmp_path = tmp.name

        try:
            test_conn = sqlite3.connect(tmp_path)
            with open(backup_path, 'r') as f:
                test_conn.executescript(f.read())
            test_conn.close()
            print("✓ Backup validation passed")
        except sqlite3.Error as e:
            print(f"✗ Backup validation failed: {e}")
            raise
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return

    # Actual restore
    print(f"Restoring from: {backup_path}")

    # Backup current DB first
    print("Creating backup of current database...")
    cmd_backup()

    # Close existing connection
    db_path = get_db_path()
    from kb.db import close_connection
    close_connection()

    # Remove current DB
    if db_path.exists():
        os.remove(db_path)

    # Restore from SQL
    new_conn = sqlite3.connect(str(db_path))
    with open(backup_path, 'r') as f:
        new_conn.executescript(f.read())
    new_conn.close()

    print(f"✓ Restored from {backup_path}")
