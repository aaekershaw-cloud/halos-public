"""Ingest lifecycle hook - auto-relate new articles to existing ones."""

import json
import logging
from datetime import datetime

from kb.db import get_connection

logger = logging.getLogger(__name__)


def on_article_ingested(article_id: str, content: str = "", classification: str = "", **kwargs) -> None:
    """Auto-create 'related' links for a freshly ingested article.

    Searches for semantically similar articles using hybrid_search (if available)
    or FTS search_articles as fallback.  Creates 'related' links in the
    article_relationships table for the top 3 matches with confidence > 0.7.

    Errors are logged but never propagated - this must not fail ingest.

    Args:
        article_id: UUID of the newly ingested article.
        content: Raw content of the article (used as search query seed).
        classification: Classification level of the article.
        **kwargs: Ignored extra payload fields.
    """
    try:
        _create_related_links(article_id, content, classification)
    except Exception as exc:
        logger.warning(f"ingest_hook: failed for article {article_id}: {exc}", exc_info=True)


def _create_related_links(article_id: str, content: str, classification: str) -> None:
    conn = get_connection()

    # Verify the article exists
    row = conn.execute(
        "SELECT id, title FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    if not row:
        logger.debug(f"ingest_hook: article {article_id} not found, skipping")
        return

    article_title = row["title"]

    # Build a short search query from title (more reliable than raw content)
    search_query = article_title[:200] if article_title else (content[:200] if content else "")
    if not search_query.strip():
        logger.debug(f"ingest_hook: no search query for article {article_id}, skipping")
        return

    # Try hybrid_search first, fall back to FTS search_articles
    candidates = _find_candidates(search_query, article_id, classification)

    if not candidates:
        logger.debug(f"ingest_hook: no candidates found for article {article_id}")
        return

    # Check that article_relationships table exists
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='article_relationships'"
    ).fetchone()
    if not table_exists:
        logger.debug("ingest_hook: article_relationships table not found, skipping related links")
        return

    links_created = 0
    for candidate in candidates[:3]:
        candidate_id = candidate.get("id")
        confidence = candidate.get("confidence", 0.0)

        if not candidate_id or candidate_id == article_id:
            continue
        # Note: FTS-based confidence is typically low (0.04-0.2)
        # Embeddings-based would be higher (0.7-1.0)
        # Use lower threshold for FTS fallback
        if confidence < 0.03:
            continue

        try:
            import uuid
            metadata = json.dumps({
                "confidence": confidence,
                "notes": "auto-linked by ingest hook",
                "candidate_title": candidate.get("title", "")
            })
            conn.execute(
                """
                INSERT OR IGNORE INTO article_relationships
                    (id, from_article_id, to_article_id, relationship_type, metadata, created_at)
                VALUES (?, ?, ?, 'related', ?, ?)
                """,
                (str(uuid.uuid4()), article_id, candidate_id, metadata, datetime.utcnow().isoformat()),
            )
            links_created += 1
        except Exception as link_exc:
            logger.debug(f"ingest_hook: could not insert link to {candidate_id}: {link_exc}")

    if links_created:
        logger.info(f"ingest_hook: created {links_created} related link(s) for article {article_id}")

    # Log to hook_events
    try:
        conn.execute(
            """
            INSERT INTO hook_events (event_type, entity_id, entity_type, status, data, created_at)
            VALUES ('ingest.related_links', ?, 'article', 'success', ?, ?)
            """,
            (
                article_id,
                json.dumps({"links_created": links_created, "candidates_found": len(candidates)}),
                datetime.utcnow().isoformat(),
            ),
        )
    except Exception as log_exc:
        logger.debug(f"ingest_hook: could not log hook_event: {log_exc}")


def _find_candidates(query: str, exclude_id: str, classification: str) -> list:
    """Return candidate articles with a 'confidence' field.

    Tries hybrid_search first; falls back to FTS search_articles.
    Returns at most 5 candidates excluding the source article.
    """
    # Attempt hybrid search (may not exist yet)
    try:
        from kb.search import hybrid_search
        results = hybrid_search(query, limit=5)
        filtered = [r for r in results if r.get("id") != exclude_id]
        # Results are already ordered by rrf_score desc. Raw RRF scores are
        # tiny (~0.016 per stream at rank 1) and aren't 0-1 normalised, so
        # derive confidence from position in the ranked list instead:
        # position 1 → 1.0, position 2 → 0.5, position 3 → 0.33, …
        for position, r in enumerate(filtered, start=1):
            if "confidence" not in r:
                r["confidence"] = 1.0 / position
        return filtered
    except (ImportError, AttributeError):
        pass

    # Fallback: FTS search_articles - assign confidence from fts rank
    try:
        from kb.search import search_articles
        results = search_articles(query, limit=5)
        filtered = [r for r in results if r.get("id") != exclude_id]
        # FTS rank is negative (lower = better). Convert to 0-1 confidence.
        for r in filtered:
            rank = r.get("rank", -1.0) or -1.0
            # Map rank to confidence: rank of 0 -> 1.0, rank of -10 -> 0.5, etc.
            r["confidence"] = max(0.0, min(1.0, 1.0 / (1.0 + abs(float(rank)))))
        return filtered
    except Exception as exc:
        logger.debug(f"ingest_hook: search_articles fallback failed: {exc}")
        return []
