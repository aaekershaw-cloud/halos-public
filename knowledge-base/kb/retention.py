"""Retention policies and data lifecycle management"""

import os
import json
import logging
import shutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

from kb.db import get_connection
from kb.config import load_config
from kb.errors import PermanentError

logger = logging.getLogger(__name__)


class RetentionAction(Enum):
    """Actions that can be taken on articles"""
    ARCHIVE = "archive"
    SOFT_DELETE = "soft_delete"
    HARD_DELETE = "hard_delete"
    KEEP = "keep"


@dataclass
class RetentionRule:
    """Represents a retention rule"""
    name: str
    action: RetentionAction
    age_days: Optional[int] = None
    tags: Optional[List[str]] = None
    classification: Optional[str] = None
    priority: int = 0  # Higher priority rules apply first


class RetentionPolicy:
    """Manages retention rules and applies them to articles"""

    def __init__(self):
        self.rules: List[RetentionRule] = []
        self.load_rules()

    def load_rules(self):
        """Load retention rules from config"""
        config = load_config()
        retention_config = config.get('knowledge_base', {}).get('retention', {})

        # Default rules
        default_rules = [
            RetentionRule(
                name='archive_old_public',
                action=RetentionAction.ARCHIVE,
                age_days=365,
                classification='public',
                priority=10
            ),
            RetentionRule(
                name='delete_old_temp',
                action=RetentionAction.SOFT_DELETE,
                age_days=30,
                tags=['temp', 'draft'],
                priority=20
            )
        ]

        # Load custom rules from config
        custom_rules = retention_config.get('rules', [])

        for rule_config in custom_rules:
            rule = RetentionRule(
                name=rule_config['name'],
                action=RetentionAction[rule_config['action'].upper()],
                age_days=rule_config.get('age_days'),
                tags=rule_config.get('tags'),
                classification=rule_config.get('classification'),
                priority=rule_config.get('priority', 0)
            )
            self.rules.append(rule)

        # Use defaults if no custom rules
        if not self.rules:
            self.rules = default_rules

        # Sort by priority (descending)
        self.rules.sort(key=lambda r: r.priority, reverse=True)

        logger.info(f"Loaded {len(self.rules)} retention rules")

    def evaluate_article(self, article: Dict) -> Optional[RetentionAction]:
        """
        Evaluate which retention action should be taken for an article.

        Args:
            article: Article dictionary with metadata

        Returns:
            RetentionAction or None if no rule applies
        """
        # Parse article metadata
        created_at = datetime.fromisoformat(article['created_at'])
        age = datetime.now() - created_at
        age_days = age.days

        tags = []
        if article.get('tags'):
            try:
                tags = json.loads(article['tags'])
            except:
                pass

        classification = article.get('classification', 'internal')

        # Apply rules in priority order
        for rule in self.rules:
            matches = True

            # Check age
            if rule.age_days is not None and age_days < rule.age_days:
                matches = False

            # Check tags
            if rule.tags is not None:
                if not any(tag in tags for tag in rule.tags):
                    matches = False

            # Check classification
            if rule.classification is not None and classification != rule.classification:
                matches = False

            if matches:
                logger.debug(
                    f"Article {article['id']} matches rule '{rule.name}': "
                    f"action={rule.action.value}"
                )
                return rule.action

        return None


def archive_article(article_id: str) -> bool:
    """
    Archive an article to separate storage.

    Moves content file to archive directory and marks in database.

    Args:
        article_id: Article ID

    Returns:
        True if archived successfully
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, title, slug, content_path, classification
        FROM articles
        WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        logger.error(f"Article not found: {article_id}")
        return False

    content_path = row['content_path']

    if not os.path.exists(content_path):
        logger.error(f"Content file not found: {content_path}")
        return False

    # Create archive directory
    import pathlib
    kb_dir = os.environ.get('KB_DIR', os.path.expanduser('~/.kb'))
    archive_dir = os.path.join(kb_dir, 'archive', row['classification'])
    os.makedirs(archive_dir, exist_ok=True)

    # Archive filename with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_filename = f"{row['slug']}_{timestamp}.md"
    archive_path = os.path.join(archive_dir, archive_filename)

    try:
        # Copy to archive
        shutil.copy2(content_path, archive_path)

        # Update database
        conn.execute("BEGIN")
        conn.execute("""
            UPDATE articles
            SET archived_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (article_id,))

        # Update raw_files if linked
        conn.execute("""
            UPDATE raw_files
            SET archived_at = CURRENT_TIMESTAMP,
                archive_path = ?
            WHERE id = (SELECT source_file FROM articles WHERE id = ?)
        """, (archive_path, article_id))

        conn.execute("COMMIT")

        logger.info(f"Archived article {article_id} to {archive_path}")
        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to archive article {article_id}: {e}")
        return False


def soft_delete_article(article_id: str, grace_period_days: int = 30) -> bool:
    """
    Soft delete an article (mark for deletion with grace period).

    Args:
        article_id: Article ID
        grace_period_days: Days before hard delete

    Returns:
        True if soft deleted successfully
    """
    conn = get_connection()

    # Archive first
    if not archive_article(article_id):
        logger.error(f"Failed to archive before soft delete: {article_id}")
        return False

    try:
        conn.execute("BEGIN")

        # Calculate hard delete date
        hard_delete_date = datetime.now() + timedelta(days=grace_period_days)

        # Mark as deleted
        conn.execute("""
            UPDATE articles
            SET deleted_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (article_id,))

        # Remove from FTS index
        from kb.search import delete_article_fts
        delete_article_fts(conn, article_id)

        conn.execute("COMMIT")

        logger.info(
            f"Soft deleted article {article_id} "
            f"(hard delete after {hard_delete_date.date()})"
        )
        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to soft delete article {article_id}: {e}")
        return False


def hard_delete_article(article_id: str) -> bool:
    """
    Permanently delete an article and all associated data.

    Args:
        article_id: Article ID

    Returns:
        True if deleted successfully
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT content_path FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        logger.warning(f"Article not found for hard delete: {article_id}")
        return False

    content_path = row['content_path']

    try:
        conn.execute("BEGIN")

        # Delete from database
        conn.execute("DELETE FROM links WHERE source_id = ? OR target_id = ?",
                     (article_id, article_id))
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))

        # Delete FTS entry (already done in soft delete, but just in case)
        from kb.search import delete_article_fts
        delete_article_fts(conn, article_id)

        conn.execute("COMMIT")

        # Delete content file AFTER commit succeeds
        if os.path.exists(content_path):
            os.remove(content_path)
            logger.info(f"Deleted content file: {content_path}")

        logger.info(f"Hard deleted article {article_id}")
        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to hard delete article {article_id}: {e}")
        return False


def recover_article(article_id: str) -> bool:
    """
    Recover a soft-deleted article.

    Args:
        article_id: Article ID

    Returns:
        True if recovered successfully
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, deleted_at FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        logger.error(f"Article not found: {article_id}")
        return False

    if not row['deleted_at']:
        logger.info(f"Article {article_id} is not deleted")
        return True

    try:
        conn.execute("BEGIN")

        # Clear deletion timestamp
        conn.execute("""
            UPDATE articles
            SET deleted_at = NULL
            WHERE id = ?
        """, (article_id,))

        # Restore to FTS index
        from kb.search import update_article_fts
        update_article_fts(conn, article_id)

        conn.execute("COMMIT")

        logger.info(f"Recovered article {article_id}")
        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to recover article {article_id}: {e}")
        return False


def apply_retention_policies(dry_run: bool = True) -> Dict[str, Any]:
    """
    Apply retention policies to all articles.

    Args:
        dry_run: If True, don't actually apply actions

    Returns:
        Summary of actions taken
    """
    conn = get_connection()
    policy = RetentionPolicy()

    # Get all non-deleted articles
    cursor = conn.execute("""
        SELECT id, title, slug, created_at, updated_at, classification, tags
        FROM articles
        WHERE deleted_at IS NULL
    """)

    articles = []
    for row in cursor:
        articles.append({
            'id': row['id'],
            'title': row['title'],
            'slug': row['slug'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'classification': row['classification'],
            'tags': row['tags']
        })

    logger.info(f"Evaluating {len(articles)} articles against retention policies...")

    actions_by_type = {
        RetentionAction.ARCHIVE: [],
        RetentionAction.SOFT_DELETE: [],
        RetentionAction.HARD_DELETE: [],
        RetentionAction.KEEP: []
    }

    for article in articles:
        action = policy.evaluate_article(article)

        if action is None:
            action = RetentionAction.KEEP

        actions_by_type[action].append(article['id'])

    # Apply actions if not dry run
    results = {
        'dry_run': dry_run,
        'total_articles': len(articles),
        'actions': {
            'archive': len(actions_by_type[RetentionAction.ARCHIVE]),
            'soft_delete': len(actions_by_type[RetentionAction.SOFT_DELETE]),
            'hard_delete': len(actions_by_type[RetentionAction.HARD_DELETE]),
            'keep': len(actions_by_type[RetentionAction.KEEP])
        },
        'success': 0,
        'failed': 0
    }

    if not dry_run:
        # Archive
        for article_id in actions_by_type[RetentionAction.ARCHIVE]:
            if archive_article(article_id):
                results['success'] += 1
            else:
                results['failed'] += 1

        # Soft delete
        for article_id in actions_by_type[RetentionAction.SOFT_DELETE]:
            if soft_delete_article(article_id):
                results['success'] += 1
            else:
                results['failed'] += 1

        # Hard delete (requires prior soft delete with grace period expired)
        for article_id in actions_by_type[RetentionAction.HARD_DELETE]:
            if hard_delete_article(article_id):
                results['success'] += 1
            else:
                results['failed'] += 1

    return results


def cleanup_expired_soft_deletes(grace_period_days: int = 30) -> int:
    """
    Permanently delete articles past their grace period.

    Args:
        grace_period_days: Grace period in days

    Returns:
        Number of articles hard deleted
    """
    conn = get_connection()

    # Find soft-deleted articles past grace period
    cutoff_date = datetime.now() - timedelta(days=grace_period_days)

    cursor = conn.execute("""
        SELECT id, title, deleted_at
        FROM articles
        WHERE deleted_at IS NOT NULL
          AND deleted_at < ?
    """, (cutoff_date.isoformat(),))

    expired_articles = [(row['id'], row['title']) for row in cursor]

    logger.info(f"Found {len(expired_articles)} articles past grace period")

    deleted_count = 0

    for article_id, title in expired_articles:
        if hard_delete_article(article_id):
            logger.info(f"Hard deleted expired article: {title} ({article_id})")
            deleted_count += 1

    return deleted_count
