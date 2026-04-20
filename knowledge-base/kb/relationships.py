"""Typed relationships between articles"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from kb.db import get_connection
from kb.errors import PermanentError

logger = logging.getLogger(__name__)

# All supported relationship types
RELATIONSHIP_TYPES = [
    "supersedes",
    "refines",
    "depends_on",
    "uses",
    "contradicts",
    "supports",
    "mentions",
    "part_of",
]

# Scoring weights for find_related_articles integration
# Higher = more relevant signal
RELATIONSHIP_WEIGHTS = {
    "supersedes": 15,
    "refines": 12,
    "depends_on": 10,
    "uses": 8,
    "contradicts": 6,
    "supports": 10,
    "mentions": 4,
    "part_of": 12,
}


def add_relationship(
    from_id: str,
    to_id: str,
    rel_type: str,
    metadata: Optional[Dict] = None,
) -> str:
    """
    Add a typed relationship between two articles.

    Args:
        from_id: Source article ID
        to_id: Target article ID
        rel_type: Relationship type (must be in RELATIONSHIP_TYPES)
        metadata: Optional dict of extra attributes

    Returns:
        Relationship record ID

    Raises:
        PermanentError: If article not found or rel_type invalid
    """
    if rel_type not in RELATIONSHIP_TYPES:
        raise PermanentError(
            f"Invalid relationship type '{rel_type}'. "
            f"Valid types: {RELATIONSHIP_TYPES}"
        )

    conn = get_connection()

    # Verify both articles exist
    for aid, label in [(from_id, "from"), (to_id, "to")]:
        cursor = conn.execute("SELECT id FROM articles WHERE id = ?", (aid,))
        if not cursor.fetchone():
            raise PermanentError(f"Article not found ({label}): {aid}")

    rel_id = str(uuid.uuid4())
    metadata_json = json.dumps(metadata) if metadata else None

    try:
        conn.execute(
            """
            INSERT INTO article_relationships
                (id, from_article_id, to_article_id, relationship_type, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rel_id, from_id, to_id, rel_type, metadata_json),
        )
        logger.info(f"Added relationship {from_id} -[{rel_type}]-> {to_id}")
        return rel_id
    except Exception as e:
        # Unique constraint: (from_id, to_id, rel_type) already exists
        if "UNIQUE" in str(e).upper():
            cursor = conn.execute(
                """
                SELECT id FROM article_relationships
                WHERE from_article_id = ? AND to_article_id = ? AND relationship_type = ?
                """,
                (from_id, to_id, rel_type),
            )
            row = cursor.fetchone()
            return row["id"] if row else rel_id
        raise


def get_relationships(
    article_id: str,
    direction: str = "outgoing",
    rel_type: Optional[str] = None,
) -> List[Dict]:
    """
    Get relationships for an article.

    Args:
        article_id: Article ID to query
        direction: "outgoing" (from_id=article), "incoming" (to_id=article),
                   or "both"
        rel_type: Optional filter by relationship type

    Returns:
        List of relationship dicts with keys:
            id, from_article_id, to_article_id, relationship_type,
            metadata, created_at, related_article (title, slug)
    """
    conn = get_connection()

    results = []

    def _fetch(where_clause, join_alias, id_col):
        type_filter = "AND r.relationship_type = ?" if rel_type else ""
        params = [article_id]
        if rel_type:
            params.append(rel_type)

        cursor = conn.execute(
            f"""
            SELECT r.id, r.from_article_id, r.to_article_id,
                   r.relationship_type, r.metadata, r.created_at,
                   a.title AS related_title, a.slug AS related_slug
            FROM article_relationships r
            JOIN articles a ON a.id = r.{id_col}
            WHERE {where_clause} {type_filter}
            ORDER BY r.created_at DESC
            """,
            params,
        )

        rows = []
        for row in cursor:
            meta = None
            if row["metadata"]:
                try:
                    meta = json.loads(row["metadata"])
                except (ValueError, TypeError):
                    meta = None
            rows.append(
                {
                    "id": row["id"],
                    "from_article_id": row["from_article_id"],
                    "to_article_id": row["to_article_id"],
                    "relationship_type": row["relationship_type"],
                    "metadata": meta,
                    "created_at": row["created_at"],
                    "related_article": {
                        "title": row["related_title"],
                        "slug": row["related_slug"],
                    },
                }
            )
        return rows

    if direction in ("outgoing", "both"):
        results.extend(_fetch("r.from_article_id = ?", "to", "to_article_id"))

    if direction in ("incoming", "both"):
        results.extend(_fetch("r.to_article_id = ?", "from", "from_article_id"))

    return results


def remove_relationship(
    from_id: str,
    to_id: str,
    rel_type: Optional[str] = None,
) -> int:
    """
    Remove relationship(s) between two articles.

    Args:
        from_id: Source article ID
        to_id: Target article ID
        rel_type: If given, only remove this type; otherwise remove all

    Returns:
        Number of rows deleted
    """
    conn = get_connection()

    if rel_type:
        cursor = conn.execute(
            """
            DELETE FROM article_relationships
            WHERE from_article_id = ? AND to_article_id = ? AND relationship_type = ?
            """,
            (from_id, to_id, rel_type),
        )
    else:
        cursor = conn.execute(
            """
            DELETE FROM article_relationships
            WHERE from_article_id = ? AND to_article_id = ?
            """,
            (from_id, to_id),
        )

    count = cursor.rowcount
    logger.info(f"Removed {count} relationship(s) between {from_id} and {to_id}")
    return count


def get_relationship_scores(article_id: str) -> Dict[str, int]:
    """
    Return a {related_article_id: score} dict for use in find_related_articles.

    Scores are based on RELATIONSHIP_WEIGHTS for each typed relationship.
    Both outgoing and incoming links contribute.

    Args:
        article_id: Article to score from

    Returns:
        Dict mapping related article IDs to their cumulative weighted score
    """
    conn = get_connection()

    scores: Dict[str, int] = {}

    # Outgoing: article_id -> other
    cursor = conn.execute(
        """
        SELECT to_article_id, relationship_type
        FROM article_relationships
        WHERE from_article_id = ?
        """,
        (article_id,),
    )
    for row in cursor:
        weight = RELATIONSHIP_WEIGHTS.get(row["relationship_type"], 5)
        target = row["to_article_id"]
        scores[target] = scores.get(target, 0) + weight

    # Incoming: other -> article_id
    cursor = conn.execute(
        """
        SELECT from_article_id, relationship_type
        FROM article_relationships
        WHERE to_article_id = ?
        """,
        (article_id,),
    )
    for row in cursor:
        weight = RELATIONSHIP_WEIGHTS.get(row["relationship_type"], 5)
        source = row["from_article_id"]
        scores[source] = scores.get(source, 0) + weight

    return scores
