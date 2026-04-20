"""Initialize knowledge base"""

import os
import logging
from pathlib import Path
from kb.config import get_project_root
from kb.db import get_connection, init_database

logger = logging.getLogger(__name__)


def cmd_init():
    """
    Initialize knowledge base.

    Creates:
    - Directory structure (wiki/, raw/, .kb/, outputs/)
    - SQLite database with schema
    - Initial git repository
    """
    root = get_project_root()

    print(f"Initializing knowledge base at: {root}\n")

    # Create directory structure
    directories = [
        'wiki/concepts',
        'wiki/projects',
        'wiki/agents',
        'wiki/sessions',
        'raw/public',
        'raw/internal',
        'raw/confidential',
        'outputs/answers',
        '.kb/backups',
        '.kb/jobs',
        '.kb/review-queue',
        '.kb/archive',
    ]

    for dir_path in directories:
        full_path = root / dir_path
        full_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Created directory: {dir_path}")

    print("✓ Created directory structure")

    # Initialize database
    try:
        init_database()
        print("✓ Initialized database")
    except Exception as e:
        print(f"✗ Database initialization failed: {e}")
        raise

    # Create .gitkeep files in empty directories
    for dir_path in ['wiki/concepts', 'wiki/projects', 'wiki/agents', 'wiki/sessions']:
        gitkeep = root / dir_path / '.gitkeep'
        gitkeep.touch()

    print("✓ Created .gitkeep files")

    # Initialize git if not already a repo
    git_dir = root / '.git'
    if not git_dir.exists():
        import subprocess
        try:
            subprocess.run(
                ['git', 'init'],
                cwd=str(root),
                check=True,
                capture_output=True
            )
            print("✓ Initialized git repository")

            # Initial commit
            subprocess.run(
                ['git', 'add', '.'],
                cwd=str(root),
                check=True,
                capture_output=True
            )
            subprocess.run(
                ['git', 'commit', '-m', 'Initial commit: knowledge base structure'],
                cwd=str(root),
                check=True,
                capture_output=True
            )
            print("✓ Created initial commit")

        except subprocess.CalledProcessError as e:
            logger.warning(f"Git initialization failed: {e}")
            print("⚠ Git initialization failed (git may not be installed)")
    else:
        print("✓ Git repository already exists")

    print(f"\n✓ Knowledge base initialized successfully at {root}")
    print("\nNext steps:")
    print("  1. kb ingest file <path> - Add source materials")
    print("  2. kb search <query> - Search wiki")
    print("  3. kb compile - Compile raw files (Phase 2)")
