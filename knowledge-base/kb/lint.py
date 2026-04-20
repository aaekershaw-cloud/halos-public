"""Integrity checks and linting for knowledge base"""

import os
import json
import hashlib
import logging
from typing import Dict, List, Any, Set, Optional
from pathlib import Path
import frontmatter
from kb.db import get_connection
from kb.errors import PermanentError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_content_path(content_path: str) -> str:
    """Resolve content_path (stored relative) against the KB project root."""
    if os.path.isabs(content_path):
        return content_path
    return os.path.join(_PROJECT_ROOT, content_path)


class IntegrityIssue:
    """Represents an integrity issue found during checks"""

    def __init__(
        self,
        check_type: str,
        severity: str,
        message: str,
        details: Optional[Dict] = None,
        fixable: bool = False
    ):
        self.check_type = check_type
        self.severity = severity  # error, warning, info
        self.message = message
        self.details = details or {}
        self.fixable = fixable

    def __repr__(self):
        return f"<IntegrityIssue {self.severity.upper()}: {self.check_type} - {self.message}>"


def check_orphaned_raw_files() -> List[IntegrityIssue]:
    """
    Find raw files with no corresponding compiled articles.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    # Find raw files without articles referencing them
    cursor = conn.execute("""
        SELECT rf.id, rf.filename, rf.path
        FROM raw_files rf
        LEFT JOIN articles a ON a.source_file = rf.id
        WHERE a.id IS NULL
    """)

    for row in cursor:
        issues.append(IntegrityIssue(
            check_type='orphaned_raw_file',
            severity='warning',
            message=f"Raw file has no compiled article: {row['filename']}",
            details={
                'raw_file_id': row['id'],
                'filename': row['filename'],
                'path': row['path']
            },
            fixable=True  # Safe to delete orphaned records and files
        ))

    return issues


def check_missing_content_files() -> List[IntegrityIssue]:
    """
    Find articles in database with missing content files.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, title, slug, content_path
        FROM articles
    """)

    for row in cursor:
        content_path = _resolve_content_path(row['content_path'])

        if not os.path.exists(content_path):
            issues.append(IntegrityIssue(
                check_type='missing_content_file',
                severity='error',
                message=f"Article content file missing: {row['title']}",
                details={
                    'article_id': row['id'],
                    'title': row['title'],
                    'slug': row['slug'],
                    'expected_path': content_path
                },
                fixable=False  # Can't fix without source
            ))

    return issues


def check_checksum_mismatches() -> List[IntegrityIssue]:
    """
    Find articles where content checksum doesn't match database.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, title, slug, content_path, checksum
        FROM articles
    """)

    for row in cursor:
        content_path = _resolve_content_path(row['content_path'])

        if not os.path.exists(content_path):
            continue  # Handled by missing_content_file check

        try:
            with open(content_path, 'r') as f:
                # Parse frontmatter to get just content
                doc = frontmatter.load(f)
                content = doc.content

            # Calculate checksum
            actual_checksum = hashlib.sha256(content.encode()).hexdigest()
            expected_checksum = row['checksum']

            if actual_checksum != expected_checksum:
                issues.append(IntegrityIssue(
                    check_type='checksum_mismatch',
                    severity='warning',
                    message=f"Content checksum mismatch: {row['title']}",
                    details={
                        'article_id': row['id'],
                        'title': row['title'],
                        'slug': row['slug'],
                        'expected': expected_checksum,
                        'actual': actual_checksum
                    },
                    fixable=True  # Can update database checksum
                ))
        except Exception as e:
            issues.append(IntegrityIssue(
                check_type='checksum_error',
                severity='error',
                message=f"Failed to verify checksum for {row['title']}: {e}",
                details={
                    'article_id': row['id'],
                    'title': row['title'],
                    'error': str(e)
                },
                fixable=False
            ))

    return issues


def check_duplicate_slugs() -> List[IntegrityIssue]:
    """
    Find duplicate article slugs.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT slug, COUNT(*) as count, GROUP_CONCAT(id) as article_ids
        FROM articles
        GROUP BY slug
        HAVING count > 1
    """)

    for row in cursor:
        issues.append(IntegrityIssue(
            check_type='duplicate_slug',
            severity='error',
            message=f"Duplicate slug found: {row['slug']}",
            details={
                'slug': row['slug'],
                'count': row['count'],
                'article_ids': row['article_ids'].split(',')
            },
            fixable=False  # Requires manual intervention
        ))

    return issues


def check_invalid_frontmatter() -> List[IntegrityIssue]:
    """
    Find articles with invalid or missing frontmatter.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, title, slug, content_path
        FROM articles
    """)

    required_fields = {'id', 'title', 'slug', 'classification'}

    for row in cursor:
        content_path = _resolve_content_path(row['content_path'])

        if not os.path.exists(content_path):
            continue  # Handled by missing_content_file check

        try:
            with open(content_path, 'r') as f:
                doc = frontmatter.load(f)

            # Check required fields
            missing_fields = required_fields - set(doc.metadata.keys())

            if missing_fields:
                issues.append(IntegrityIssue(
                    check_type='invalid_frontmatter',
                    severity='warning',
                    message=f"Missing frontmatter fields in {row['title']}: {', '.join(missing_fields)}",
                    details={
                        'article_id': row['id'],
                        'title': row['title'],
                        'missing_fields': list(missing_fields)
                    },
                    fixable=True  # Can populate from database
                ))

            # Check ID match
            if 'id' in doc.metadata and doc.metadata['id'] != row['id']:
                issues.append(IntegrityIssue(
                    check_type='id_mismatch',
                    severity='error',
                    message=f"Article ID mismatch: {row['title']}",
                    details={
                        'database_id': row['id'],
                        'frontmatter_id': doc.metadata['id'],
                        'title': row['title']
                    },
                    fixable=True  # Can update frontmatter
                ))

        except Exception as e:
            issues.append(IntegrityIssue(
                check_type='frontmatter_error',
                severity='error',
                message=f"Failed to parse frontmatter for {row['title']}: {e}",
                details={
                    'article_id': row['id'],
                    'title': row['title'],
                    'error': str(e)
                },
                fixable=False
            ))

    return issues


def check_broken_links() -> List[IntegrityIssue]:
    """
    Find broken links between articles.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    # Check links table references
    cursor = conn.execute("""
        SELECT l.source_id, l.target_id, a_src.title as source_title, a_tgt.id as target_exists
        FROM links l
        JOIN articles a_src ON a_src.id = l.source_id
        LEFT JOIN articles a_tgt ON a_tgt.id = l.target_id
        WHERE a_tgt.id IS NULL
    """)

    for row in cursor:
        issues.append(IntegrityIssue(
            check_type='broken_link',
            severity='warning',
            message=f"Broken link in {row['source_title']}: target article not found",
            details={
                'source_id': row['source_id'],
                'source_title': row['source_title'],
                'target_id': row['target_id']
            },
            fixable=True  # Can remove broken link
        ))

    return issues


def check_fts_index_sync() -> List[IntegrityIssue]:
    """
    Check if FTS index is in sync with articles table.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    # Count articles
    cursor = conn.execute("SELECT COUNT(*) as count FROM articles")
    article_count = cursor.fetchone()['count']

    # Count FTS entries
    cursor = conn.execute("SELECT COUNT(*) as count FROM articles_fts")
    fts_count = cursor.fetchone()['count']

    if article_count != fts_count:
        issues.append(IntegrityIssue(
            check_type='fts_out_of_sync',
            severity='warning',
            message=f"FTS index out of sync: {article_count} articles, {fts_count} FTS entries",
            details={
                'article_count': article_count,
                'fts_count': fts_count,
                'diff': abs(article_count - fts_count)
            },
            fixable=True  # Can rebuild FTS index
        ))

    return issues


def fix_orphaned_raw_files(dry_run: bool = True) -> int:
    """
    Fix orphaned raw files by deleting DB records and disk files.

    These are raw_files entries with no corresponding compiled article.
    Safe to delete — they represent ingests that were never compiled or
    where compilation failed permanently.

    Args:
        dry_run: If True, don't actually fix

    Returns:
        Number of issues fixed
    """
    issues = check_orphaned_raw_files()

    if dry_run or not issues:
        return len(issues)

    conn = get_connection()
    root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    fixed = 0

    for issue in issues:
        raw_file_id = issue.details['raw_file_id']
        file_path = issue.details.get('path', '')

        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM raw_files WHERE id = ?", (raw_file_id,))
            conn.execute("COMMIT")

            # Remove disk file if it exists
            if file_path:
                full_path = root / file_path
                if full_path.exists():
                    full_path.unlink()
                    logger.info(f"Deleted orphaned raw file: {file_path}")

            logger.info(f"Removed orphaned raw_files record: {raw_file_id}")
            fixed += 1

        except Exception as e:
            conn.execute("ROLLBACK")
            logger.error(f"Failed to fix orphaned raw file {raw_file_id}: {e}")

    return fixed


def fix_invalid_frontmatter(dry_run: bool = True) -> int:
    """
    Fix articles with missing frontmatter by populating from database values.

    Args:
        dry_run: If True, don't actually fix

    Returns:
        Number of issues fixed
    """
    issues = check_invalid_frontmatter()
    fixable = [i for i in issues if i.check_type == 'invalid_frontmatter']

    if dry_run or not fixable:
        return len(fixable)

    conn = get_connection()
    fixed = 0

    for issue in fixable:
        article_id = issue.details['article_id']

        try:
            cursor = conn.execute("""
                SELECT id, title, slug, classification, content_path
                FROM articles WHERE id = ?
            """, (article_id,))
            row = cursor.fetchone()
            if not row:
                continue

            content_path = _resolve_content_path(row['content_path'])
            if not os.path.exists(content_path):
                continue

            with open(content_path, 'r') as f:
                doc = frontmatter.load(f)

            # Populate missing fields from DB
            if 'id' not in doc.metadata:
                doc.metadata['id'] = row['id']
            if 'title' not in doc.metadata:
                doc.metadata['title'] = row['title']
            if 'slug' not in doc.metadata:
                doc.metadata['slug'] = row['slug']
            if 'classification' not in doc.metadata:
                doc.metadata['classification'] = row['classification']

            with open(content_path, 'w') as f:
                f.write(frontmatter.dumps(doc))

            logger.info(f"Fixed frontmatter for article {article_id}")
            fixed += 1

        except Exception as e:
            logger.error(f"Failed to fix frontmatter for {article_id}: {e}")

    return fixed


def fix_checksum_mismatches(dry_run: bool = True) -> int:
    """
    Fix checksum mismatches by updating database.

    Args:
        dry_run: If True, don't actually fix

    Returns:
        Number of issues fixed
    """
    issues = check_checksum_mismatches()
    fixed = 0

    if dry_run:
        return len([i for i in issues if i.check_type == 'checksum_mismatch'])

    conn = get_connection()

    for issue in issues:
        if issue.check_type != 'checksum_mismatch':
            continue

        article_id = issue.details['article_id']
        new_checksum = issue.details['actual']

        conn.execute("BEGIN")
        try:
            conn.execute("""
                UPDATE articles
                SET checksum = ?
                WHERE id = ?
            """, (new_checksum, article_id))
            conn.execute("COMMIT")

            logger.info(f"Fixed checksum for article {article_id}")
            fixed += 1

        except Exception as e:
            conn.execute("ROLLBACK")
            logger.error(f"Failed to fix checksum for {article_id}: {e}")

    return fixed


def fix_broken_links(dry_run: bool = True) -> int:
    """
    Fix broken links by removing them from links table.

    Args:
        dry_run: If True, don't actually fix

    Returns:
        Number of issues fixed
    """
    issues = check_broken_links()

    if dry_run or not issues:
        return len(issues)

    conn = get_connection()
    fixed = 0

    conn.execute("BEGIN")
    try:
        for issue in issues:
            source_id = issue.details['source_id']
            target_id = issue.details['target_id']

            conn.execute("""
                DELETE FROM links
                WHERE source_id = ? AND target_id = ?
            """, (source_id, target_id))

            logger.info(f"Removed broken link: {source_id} -> {target_id}")
            fixed += 1

        conn.execute("COMMIT")

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to fix broken links: {e}")
        return 0

    return fixed


def rebuild_fts_index() -> int:
    """
    Rebuild FTS index from articles table.

    Returns:
        Number of articles indexed
    """
    from kb.search import rebuild_fts_index
    return rebuild_fts_index()


def check_low_confidence_articles() -> List[IntegrityIssue]:
    """
    Find articles with low confidence scores.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT id, title, slug, confidence_score
        FROM articles
        WHERE deleted_at IS NULL
          AND confidence_score < 0.4
        ORDER BY confidence_score ASC
    """)

    for row in cursor:
        issues.append(IntegrityIssue(
            check_type='low_confidence',
            severity='warning',
            message=f"Article '{row['title']}' has low confidence score: {row['confidence_score']:.2f}",
            details={
                'article_id': row['id'],
                'slug': row['slug'],
                'confidence_score': row['confidence_score']
            },
            fixable=False
        ))

    return issues


def check_stale_articles() -> List[IntegrityIssue]:
    """
    Find articles that haven't been confirmed recently.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    # Articles with NULL last_confirmed_at or > 180 days old
    cursor = conn.execute("""
        SELECT id, title, slug, last_confirmed_at,
               julianday('now') - julianday(last_confirmed_at) as days_since_confirmed
        FROM articles
        WHERE deleted_at IS NULL
          AND (last_confirmed_at IS NULL OR julianday('now') - julianday(last_confirmed_at) > 180)
        ORDER BY last_confirmed_at ASC NULLS FIRST
    """)

    for row in cursor:
        if row['last_confirmed_at'] is None:
            msg = f"Article '{row['title']}' has never been confirmed"
        else:
            days = int(row['days_since_confirmed'])
            msg = f"Article '{row['title']}' hasn't been confirmed in {days} days"

        issues.append(IntegrityIssue(
            check_type='stale_article',
            severity='info',
            message=msg,
            details={
                'article_id': row['id'],
                'slug': row['slug'],
                'last_confirmed_at': row['last_confirmed_at'],
                'days_since_confirmed': row['days_since_confirmed']
            },
            fixable=False
        ))

    return issues


def check_superseded_articles() -> List[IntegrityIssue]:
    """
    Find articles that have been superseded.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT a.id, a.title, a.slug, a.superseded_by,
               n.title as new_title, n.slug as new_slug
        FROM articles a
        LEFT JOIN articles n ON a.superseded_by = n.id
        WHERE a.deleted_at IS NULL
          AND a.superseded_by IS NOT NULL
    """)

    for row in cursor:
        issues.append(IntegrityIssue(
            check_type='superseded_article',
            severity='info',
            message=f"Article '{row['title']}' has been superseded by '{row['new_title']}'",
            details={
                'article_id': row['id'],
                'slug': row['slug'],
                'superseded_by': row['superseded_by'],
                'new_slug': row['new_slug']
            },
            fixable=False
        ))

    return issues


def check_orphaned_supersessions() -> List[IntegrityIssue]:
    """
    Find articles with superseded_by pointing to nonexistent articles.

    Returns:
        List of integrity issues
    """
    issues = []
    conn = get_connection()

    cursor = conn.execute("""
        SELECT a.id, a.title, a.slug, a.superseded_by
        FROM articles a
        LEFT JOIN articles n ON a.superseded_by = n.id
        WHERE a.deleted_at IS NULL
          AND a.superseded_by IS NOT NULL
          AND n.id IS NULL
    """)

    for row in cursor:
        issues.append(IntegrityIssue(
            check_type='orphaned_supersession',
            severity='error',
            message=f"Article '{row['title']}' superseded_by points to nonexistent article '{row['superseded_by']}'",
            details={
                'article_id': row['id'],
                'slug': row['slug'],
                'superseded_by': row['superseded_by']
            },
            fixable=True
        ))

    return issues


def run_all_checks() -> Dict[str, List[IntegrityIssue]]:
    """
    Run all integrity checks.

    Returns:
        Dictionary mapping check name to list of issues
    """
    checks = {
        'orphaned_raw_files': check_orphaned_raw_files,
        'missing_content_files': check_missing_content_files,
        'checksum_mismatches': check_checksum_mismatches,
        'duplicate_slugs': check_duplicate_slugs,
        'invalid_frontmatter': check_invalid_frontmatter,
        'broken_links': check_broken_links,
        'fts_index_sync': check_fts_index_sync,
        'low_confidence_articles': check_low_confidence_articles,
        'stale_articles': check_stale_articles,
        'superseded_articles': check_superseded_articles,
        'orphaned_supersessions': check_orphaned_supersessions
    }

    results = {}

    for check_name, check_func in checks.items():
        logger.info(f"Running check: {check_name}")
        try:
            issues = check_func()
            results[check_name] = issues

            if issues:
                logger.warning(f"  Found {len(issues)} issue(s)")
            else:
                logger.info(f"  ✓ No issues found")

        except Exception as e:
            logger.error(f"  ✗ Check failed: {e}")
            results[check_name] = [IntegrityIssue(
                check_type=check_name,
                severity='error',
                message=f"Check failed: {e}",
                fixable=False
            )]

    return results


def get_summary(results: Dict[str, List[IntegrityIssue]]) -> Dict[str, Any]:
    """
    Get summary statistics from check results.

    Args:
        results: Check results from run_all_checks()

    Returns:
        Summary dictionary
    """
    total_issues = sum(len(issues) for issues in results.values())

    by_severity = {'error': 0, 'warning': 0, 'info': 0}
    fixable_count = 0

    for issues in results.values():
        for issue in issues:
            by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
            if issue.fixable:
                fixable_count += 1

    return {
        'total_issues': total_issues,
        'by_severity': by_severity,
        'fixable': fixable_count,
        'checks_run': len(results)
    }
