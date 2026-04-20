"""Review queue management for human-in-the-loop approval"""

import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from kb.db import get_connection
from kb.compile import apply_compilation_result
from kb.jobs import requeue_approved_job
from kb.errors import PermanentError

logger = logging.getLogger(__name__)


def list_pending_reviews(limit: int = 20) -> List[Dict[str, Any]]:
    """
    List all pending reviews.

    Args:
        limit: Maximum number of reviews to return

    Returns:
        List of review dictionaries with summary info
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT
            rq.id,
            rq.raw_file_id,
            rq.changes_summary,
            rq.created_at,
            rq.job_id,
            rf.path,
            rf.classification
        FROM review_queue rq
        JOIN raw_files rf ON rf.id = rq.raw_file_id
        WHERE rq.status = 'pending'
        ORDER BY rq.created_at DESC
        LIMIT ?
    """, (limit,))

    reviews = []
    for row in cursor:
        reviews.append({
            'id': row['id'],
            'raw_file_id': row['raw_file_id'],
            'summary': row['changes_summary'],
            'created_at': row['created_at'],
            'job_id': row['job_id'],
            'source_file': row['path'],
            'classification': row['classification']
        })

    return reviews


def get_review_details(review_id: str) -> Optional[Dict[str, Any]]:
    """
    Get full details for a review entry.

    Args:
        review_id: Review entry ID

    Returns:
        Full review details including parsed changes, or None if not found
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT
            rq.id,
            rq.raw_file_id,
            rq.changes_summary,
            rq.proposed_changes,
            rq.status,
            rq.created_at,
            rq.reviewed_at,
            rq.reviewer_notes,
            rq.job_id,
            rf.path,
            rf.classification
        FROM review_queue rq
        JOIN raw_files rf ON rf.id = rq.raw_file_id
        WHERE rq.id = ?
    """, (review_id,))

    row = cursor.fetchone()
    if not row:
        return None

    # Parse changes JSON
    changes_data = json.loads(row['proposed_changes']) if row['proposed_changes'] else {}

    return {
        'id': row['id'],
        'raw_file_id': row['raw_file_id'],
        'summary': row['changes_summary'],
        'changes': changes_data,
        'status': row['status'],
        'created_at': row['created_at'],
        'reviewed_at': row['reviewed_at'],
        'reviewer_notes': row['reviewer_notes'],
        'job_id': row['job_id'],
        'source_file': row['path'],
        'classification': row['classification']
    }


def approve_review(
    review_id: str,
    reviewer_notes: Optional[str] = None,
    approved_changes: Optional[Dict] = None
) -> str:
    """
    Approve a review and apply the changes.

    Args:
        review_id: Review entry ID
        reviewer_notes: Optional notes from reviewer
        approved_changes: Optional modified changes (default: use original)

    Returns:
        Article ID of applied changes

    Raises:
        PermanentError: If review not found or already processed
    """
    conn = get_connection()

    # Get review details
    review = get_review_details(review_id)
    if not review:
        raise PermanentError(f"Review not found: {review_id}")

    if review['status'] != 'pending':
        raise PermanentError(
            f"Review {review_id} already processed (status: {review['status']})"
        )

    # Use approved_changes if provided, otherwise use original
    if approved_changes is None:
        approved_changes = review['changes']['full_output']

    # Apply compilation result
    article_id = apply_compilation_result(
        raw_file_id=review['raw_file_id'],
        compilation_result=approved_changes,
        auto_approve=True
    )

    # Update review status
    conn.execute("BEGIN")

    try:
        conn.execute("""
            UPDATE review_queue
            SET status = 'approved',
                reviewed_at = CURRENT_TIMESTAMP,
                reviewer_notes = ?
            WHERE id = ?
        """, (reviewer_notes, review_id))

        # If this was part of a job, re-queue it with approved changes
        if review['job_id']:
            requeue_approved_job(review['job_id'], approved_changes)

        conn.execute("COMMIT")

        logger.info(
            f"Approved review {review_id} -> article {article_id}"
        )

        return article_id

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def reject_review(review_id: str, reason: str) -> bool:
    """
    Reject a review entry.

    Args:
        review_id: Review entry ID
        reason: Reason for rejection

    Returns:
        True if rejected, False if not found or already processed

    Raises:
        PermanentError: If review already processed
    """
    conn = get_connection()

    # Check review exists and is pending
    review = get_review_details(review_id)
    if not review:
        return False

    if review['status'] != 'pending':
        raise PermanentError(
            f"Review {review_id} already processed (status: {review['status']})"
        )

    conn.execute("BEGIN")

    try:
        # Update status
        conn.execute("""
            UPDATE review_queue
            SET status = 'rejected',
                reviewed_at = CURRENT_TIMESTAMP,
                reviewer_notes = ?
            WHERE id = ?
        """, (reason, review_id))

        conn.execute("COMMIT")

        logger.info(f"Rejected review {review_id}: {reason}")

        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        raise


def get_review_stats() -> Dict[str, Any]:
    """
    Get review queue statistics.

    Returns:
        {
            'pending': int,
            'approved': int,
            'rejected': int,
            'oldest_pending': str (ISO timestamp or None)
        }
    """
    conn = get_connection()

    # Count by status
    cursor = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM review_queue
        GROUP BY status
    """)

    stats = {
        'pending': 0,
        'approved': 0,
        'rejected': 0
    }

    for row in cursor:
        stats[row['status']] = row['count']

    # Get oldest pending review timestamp
    cursor = conn.execute("""
        SELECT MIN(created_at) as oldest
        FROM review_queue
        WHERE status = 'pending'
    """)

    row = cursor.fetchone()
    stats['oldest_pending'] = row['oldest'] if row and row['oldest'] else None

    return stats


def auto_approve_all(limit: int = 100) -> Dict[str, Any]:
    """
    Auto-approve all pending reviews (for testing or batch operations).

    Args:
        limit: Maximum number of reviews to approve

    Returns:
        {
            'approved': int,
            'failed': int,
            'article_ids': List[str]
        }
    """
    reviews = list_pending_reviews(limit=limit)

    approved = 0
    failed = 0
    article_ids = []

    for review in reviews:
        try:
            article_id = approve_review(
                review['id'],
                reviewer_notes='Auto-approved by batch operation'
            )
            article_ids.append(article_id)
            approved += 1

            logger.info(f"Auto-approved review {review['id']} -> {article_id}")

        except Exception as e:
            logger.error(f"Failed to auto-approve review {review['id']}: {e}")
            failed += 1

    summary = {
        'approved': approved,
        'failed': failed,
        'article_ids': article_ids
    }

    logger.info(
        f"Auto-approve batch complete: {approved} approved, {failed} failed"
    )

    return summary


def display_review(review_id: str) -> str:
    """
    Generate a human-readable display of a review for CLI output.

    Args:
        review_id: Review entry ID

    Returns:
        Formatted string for CLI display
    """
    review = get_review_details(review_id)
    if not review:
        return f"Review {review_id} not found"

    changes = review['changes']
    full_output = changes.get('full_output', {})
    structural_changes = full_output.get('structural_changes', {})

    lines = []
    lines.append(f"Review ID: {review['id']}")
    lines.append(f"Status: {review['status']}")
    lines.append(f"Created: {review['created_at']}")
    lines.append(f"Source: {review['source_file']}")
    lines.append("")
    lines.append(f"Title: {full_output.get('title', 'N/A')}")
    lines.append(f"Slug: {full_output.get('slug', 'N/A')}")
    lines.append(f"Summary: {full_output.get('summary', 'N/A')}")
    lines.append("")
    lines.append("Tags:")
    for tag in full_output.get('tags', []):
        lines.append(f"  - {tag}")
    lines.append("")
    lines.append("Structural Changes:")
    lines.append(f"  Requires Review: {structural_changes.get('requires_review', False)}")
    lines.append(f"  Reason: {structural_changes.get('reason', 'N/A')}")
    lines.append(f"  New Article: {structural_changes.get('new_article', False)}")
    lines.append(f"  Merge With: {structural_changes.get('merge_with', 'None')}")

    new_links = structural_changes.get('new_links', [])
    if new_links:
        lines.append("  New Links:")
        for link in new_links:
            lines.append(f"    - {link}")

    lines.append("")
    lines.append("Content Preview:")
    content = full_output.get('content', '')
    lines.append(content[:500] + ('...' if len(content) > 500 else ''))

    return '\n'.join(lines)
