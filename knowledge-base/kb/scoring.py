"""Confidence scoring and article supersession"""

import logging
from datetime import datetime, timezone
from kb.db import get_connection
from kb.errors import PermanentError

logger = logging.getLogger(__name__)

# Age penalty starts after this many days
_AGE_THRESHOLD_DAYS = 180
# Maximum age penalty (full decay at 2x threshold = 360 days)
_AGE_MAX_PENALTY = 0.3
# Penalty for having contradicts links
_CONFLICT_PENALTY = 0.2


def calculate_confidence(article_id: str) -> float:
    """
    Compute confidence score (0.0-1.0) for an article.

    Factors:
    - source_count: base = 0.3 + (min(source_count, 5) * 0.1)
    - Age since last_confirmed_at: linear decay after 180 days
    - Contradicts links: -0.2 if any exist

    Args:
        article_id: Article ID to score

    Returns:
        Confidence score between 0.0 and 1.0
    """
    conn = get_connection()

    # Get article data
    cursor = conn.execute("""
        SELECT source_count, last_confirmed_at
        FROM articles WHERE id = ?
    """, (article_id,))
    row = cursor.fetchone()

    if not row:
        raise PermanentError(f"Article not found: {article_id}")

    source_count = row['source_count'] or 0
    last_confirmed_at = row['last_confirmed_at']

    # Base score from sources: 0.3 + up to 0.5 from sources
    base = 0.3 + (min(source_count, 5) * 0.1)

    # Age penalty: linear decay after threshold
    age_penalty = 0.0
    if last_confirmed_at:
        try:
            confirmed_dt = datetime.fromisoformat(last_confirmed_at)
            if confirmed_dt.tzinfo is None:
                confirmed_dt = confirmed_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_since = (now - confirmed_dt).days
        except (ValueError, TypeError):
            days_since = _AGE_THRESHOLD_DAYS + 1

        if days_since > _AGE_THRESHOLD_DAYS:
            excess_days = days_since - _AGE_THRESHOLD_DAYS
            age_penalty = min(_AGE_MAX_PENALTY, excess_days / _AGE_THRESHOLD_DAYS * _AGE_MAX_PENALTY)

    # Check for contradicts links
    cursor = conn.execute("""
        SELECT COUNT(*) as count FROM links
        WHERE (source_id = ? OR target_id = ?) AND link_type = 'contradicts'
    """, (article_id, article_id))
    contradicts_count = cursor.fetchone()['count']

    conflict_penalty = _CONFLICT_PENALTY if contradicts_count > 0 else 0.0

    score = max(0.0, min(1.0, base - age_penalty - conflict_penalty))
    return round(score, 4)


def update_confidence(article_id: str, score: float = None, auto: bool = True) -> float:
    """
    Update confidence score for an article.

    Args:
        article_id: Article ID to update
        score: Manual score to set (overrides auto)
        auto: If True and no score provided, calculate automatically

    Returns:
        Final confidence score
    """
    conn = get_connection()

    if score is not None:
        final_score = max(0.0, min(1.0, score))
    elif auto:
        final_score = calculate_confidence(article_id)
    else:
        raise PermanentError("Either score or auto=True must be provided")

    # Never auto-set to 1.0 (reserved for explicit verification)
    if score is None and final_score >= 1.0:
        final_score = 0.99

    conn.execute("""
        UPDATE articles SET confidence_score = ? WHERE id = ?
    """, (final_score, article_id))

    logger.info(f"Updated confidence for {article_id}: {final_score}")
    return final_score


def confirm_article(article_id: str, reviewer: str = "human") -> float:
    """
    Confirm an article as accurate, boosting its confidence.

    Sets last_confirmed_at to now and bumps confidence to at least 0.7.

    Args:
        article_id: Article ID to confirm
        reviewer: Who confirmed (default: "human")

    Returns:
        New confidence score
    """
    conn = get_connection()

    # Verify article exists and get current score
    cursor = conn.execute("""
        SELECT confidence_score FROM articles WHERE id = ?
    """, (article_id,))
    row = cursor.fetchone()

    if not row:
        raise PermanentError(f"Article not found: {article_id}")

    current_score = row['confidence_score'] or 0.5
    new_score = max(current_score, 0.7)

    now = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        UPDATE articles
        SET last_confirmed_at = ?, confidence_score = ?
        WHERE id = ?
    """, (now, new_score, article_id))

    logger.info(f"Article {article_id} confirmed by {reviewer}, confidence: {new_score}")
    return new_score


def supersede_article(old_id: str, new_id: str) -> None:
    """
    Mark an article as superseded by a newer article.

    Sets superseded_by on the old article and creates a 'supersedes' link
    from new to old.

    Args:
        old_id: Article ID being superseded
        new_id: Article ID that replaces it
    """
    conn = get_connection()

    # Verify both articles exist
    for aid, label in [(old_id, "Old"), (new_id, "New")]:
        cursor = conn.execute("SELECT id FROM articles WHERE id = ?", (aid,))
        if not cursor.fetchone():
            raise PermanentError(f"{label} article not found: {aid}")

    conn.execute("BEGIN")

    try:
        # Set superseded_by on old article
        conn.execute("""
            UPDATE articles SET superseded_by = ? WHERE id = ?
        """, (new_id, old_id))

        # Create supersedes link (new -> old)
        try:
            conn.execute("""
                INSERT INTO links (source_id, target_id, link_type)
                VALUES (?, ?, 'supersedes')
            """, (new_id, old_id))
        except Exception:
            # Duplicate link - ignore
            pass

        conn.execute("COMMIT")

        logger.info(f"Article {old_id} superseded by {new_id}")

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to supersede article {old_id}: {e}")
        raise
