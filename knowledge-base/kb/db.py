"""Database management and schema"""

import sqlite3
import logging
from pathlib import Path
from typing import Optional
from kb.config import get_db_path, get_project_root

logger = logging.getLogger(__name__)

_conn = None


def get_connection() -> sqlite3.Connection:
    """
    Get SQLite database connection (singleton pattern).

    Returns connection with row_factory set to sqlite3.Row for dict-like access.
    """
    global _conn

    if _conn is not None:
        return _conn

    db_path = get_db_path()

    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Connect with row factory
    _conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,  # Allow multi-threaded access
        isolation_level=None  # Autocommit mode (we manage transactions explicitly)
    )
    _conn.row_factory = sqlite3.Row

    # Enable foreign keys
    _conn.execute("PRAGMA foreign_keys = ON")

    # Enable WAL mode for better concurrency
    _conn.execute("PRAGMA journal_mode = WAL")

    logger.info(f"Connected to database: {db_path}")

    return _conn


def close_connection():
    """Close database connection"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
        logger.info("Database connection closed")


def init_database():
    """
    Initialize database with schema.

    Creates all tables if they don't exist.
    For production use, prefer migrations via kb migrate.
    """
    conn = get_connection()

    # Check if database is already initialized
    cursor = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='articles'
    """)

    if cursor.fetchone():
        logger.info("Database already initialized")
        return

    logger.info("Initializing database schema...")

    # Load and execute schema from migrations
    migrations_dir = Path(__file__).parent / 'migrations'
    schema_file = migrations_dir / '001_initial.sql'

    if not schema_file.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_file}")

    with open(schema_file, 'r') as f:
        schema_sql = f.read()

    # Execute schema
    conn.executescript(schema_sql)

    # Create migrations_log and record this migration
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migrations_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            migration_name TEXT UNIQUE NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        INSERT INTO migrations_log (migration_name)
        VALUES ('001_initial.sql')
    """)

    conn.commit()

    logger.info("Database initialized successfully")


def apply_migrations():
    """
    Apply pending database migrations.

    Tracks applied migrations in migrations_log table.
    """
    conn = get_connection()

    # Create migrations_log table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migrations_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            migration_name TEXT UNIQUE NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # Get applied migrations
    cursor = conn.execute("SELECT migration_name FROM migrations_log")
    applied = {row['migration_name'] for row in cursor}

    # Find migration files
    migrations_dir = Path(__file__).parent / 'migrations'

    if not migrations_dir.exists():
        logger.warning(f"Migrations directory not found: {migrations_dir}")
        return

    migration_files = sorted(migrations_dir.glob('*.sql'))

    if not migration_files:
        logger.info("No migrations found")
        return

    # Apply pending migrations
    for migration_file in migration_files:
        migration_name = migration_file.name

        if migration_name in applied:
            logger.debug(f"Migration already applied: {migration_name}")
            continue

        logger.info(f"Applying migration: {migration_name}")

        with open(migration_file, 'r') as f:
            migration_sql = f.read()

        try:
            conn.executescript(migration_sql)

            # Record migration
            conn.execute("""
                INSERT INTO migrations_log (migration_name)
                VALUES (?)
            """, (migration_name,))

            conn.commit()

            logger.info(f"✓ Migration applied: {migration_name}")

        except Exception as e:
            conn.rollback()
            logger.error(f"✗ Migration failed: {migration_name}: {e}")
            raise


def get_schema_version() -> int:
    """Get current schema version (count of applied migrations)"""
    conn = get_connection()

    # Check if migrations_log exists
    cursor = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='migrations_log'
    """)

    if not cursor.fetchone():
        return 0

    cursor = conn.execute("SELECT COUNT(*) as count FROM migrations_log")
    row = cursor.fetchone()
    return row['count'] if row else 0
