"""Search functionality with FTS5"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Optional
from kb.config import get_project_root
from kb.db import get_connection
from kb.performance import cached, optimize_fts_query

logger = logging.getLogger(__name__)


def update_article_fts(conn, article_id: str):
    """
    Update FTS index after article change.

    IMPORTANT: Does NOT commit - caller must manage transaction.
    Call this BEFORE committing your article changes.

    Args:
        conn: SQLite connection
        article_id: Article UUID to index
    """
    cursor = conn.execute("""
        SELECT id, title, content_path, tags
        FROM articles
        WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        logger.warning(f"Article not found for FTS update: {article_id}")
        return

    article_id = row['id']
    title = row['title']
    content_path = row['content_path']
    tags = row['tags']

    # Read markdown file
    root = get_project_root()
    full_path = root / content_path

    if not full_path.exists():
        logger.warning(f"Content file not found: {content_path}")
        return

    with open(full_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Strip frontmatter
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            content = parts[2]

    # Parse tags if JSON
    tags_str = tags
    if tags and isinstance(tags, str):
        try:
            tags_list = json.loads(tags)
            tags_str = ' '.join(tags_list)
        except:
            pass

    # Get rowid for this article
    rowid_cursor = conn.execute("""
        SELECT rowid FROM articles WHERE id = ?
    """, (article_id,))
    rowid_row = rowid_cursor.fetchone()

    if not rowid_row:
        logger.warning(f"No rowid found for article: {article_id}")
        return

    rowid = rowid_row[0]

    # Delete existing FTS entry
    conn.execute("""
        DELETE FROM articles_fts
        WHERE rowid = ?
    """, (rowid,))

    # Insert new FTS entry
    conn.execute("""
        INSERT INTO articles_fts(rowid, title, content, tags)
        VALUES (?, ?, ?, ?)
    """, (rowid, title, content, tags_str))

    # NO COMMIT - caller handles it


def delete_article_fts(conn, article_id: str):
    """
    Remove article from FTS index.

    Does NOT commit - caller manages transaction.

    Args:
        conn: SQLite connection
        article_id: Article UUID to remove
    """
    # Get rowid
    cursor = conn.execute("""
        SELECT rowid FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        return

    rowid = row[0]

    conn.execute("""
        DELETE FROM articles_fts
        WHERE rowid = ?
    """, (rowid,))

    # NO COMMIT


@cached(ttl_seconds=300)
def search_articles(
    query: str,
    tag: Optional[str] = None,
    classification: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 20,
    agent_scope: Optional[str] = None,
    min_confidence: float = 0.0,
    include_superseded: bool = False
) -> List[Dict]:
    """
    Search articles using FTS5.

    Args:
        query: Search query (FTS5 syntax)
        tag: Filter by tag
        classification: Filter by classification level
        since: Filter by date (YYYY-MM-DD)
        limit: Max results (default 20)
        agent_scope: Filter by agent (NULL/shared articles always included)
        min_confidence: Minimum confidence_score threshold (default 0.0)
        include_superseded: Include articles that have been superseded (default False)

    Returns:
        List of article dictionaries with ranking
    """
    conn = get_connection()

    # Optimize FTS query
    optimized_query = optimize_fts_query(query)

    # Build query
    sql_parts = ["""
        SELECT
            a.id,
            a.title,
            a.slug,
            a.classification,
            a.tags,
            a.created_at,
            a.updated_at,
            a.confidence_score,
            articles_fts.rank as fts_rank
        FROM articles a
        JOIN articles_fts ON articles_fts.rowid = a.rowid
        WHERE articles_fts MATCH ?
    """]

    params = [optimized_query]

    # Add filters
    if tag:
        sql_parts.append("AND json_extract(a.tags, '$') LIKE ?")
        params.append(f'%"{tag}"%')

    if classification:
        sql_parts.append("AND a.classification = ?")
        params.append(classification)

    if since:
        sql_parts.append("AND a.updated_at >= ?")
        params.append(since)

    if agent_scope:
        sql_parts.append("AND (a.agent_scope IS NULL OR a.agent_scope = ?)")
        params.append(agent_scope)

    if min_confidence > 0.0:
        sql_parts.append("AND a.confidence_score >= ?")
        params.append(min_confidence)

    if not include_superseded:
        sql_parts.append("AND a.superseded_by IS NULL")

    # Order by FTS rank (lower is better in FTS5)
    sql_parts.append("ORDER BY fts_rank")
    sql_parts.append(f"LIMIT {limit}")

    sql = " ".join(sql_parts)

    try:
        cursor = conn.execute(sql, params)
        results = []

        for row in cursor:
            # Parse tags
            tags = []
            if row['tags']:
                try:
                    tags = json.loads(row['tags'])
                except:
                    pass

            results.append({
                'id': row['id'],
                'title': row['title'],
                'slug': row['slug'],
                'classification': row['classification'],
                'tags': tags,
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
                'confidence_score': row['confidence_score'],
                'rank': row['fts_rank']
            })

        return results

    except Exception as e:
        logger.error(f"Search error: {e}")
        raise


def hybrid_search(
    query: str,
    agent: Optional[str] = None,
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    limit: int = 20,
    k: int = 60,
) -> List[Dict]:
    """
    Hybrid search combining FTS5, vector, and graph streams with Reciprocal Rank Fusion.

    Stream 1: FTS5 full-text search via search_articles().
    Stream 2: Vector semantic search via vector_search() (skipped gracefully if
              sentence-transformers is not installed or no embeddings exist).
    Stream 3: Graph BFS from the top-5 FTS hits via graph_search().

    RRF score for each article = sum(1 / (k + rank_i)) across all streams it
    appears in. rank is 1-indexed within each stream. k=60 is the standard
    constant that dampens the influence of very high ranks.

    Filters (agent_scope, min_confidence, include_superseded) are applied to
    the unified result set after fusion.

    Args:
        query: Search query string.
        agent: Filter results to this agent scope (NULL/shared always included).
        min_confidence: Minimum confidence_score threshold (default 0.0).
        include_superseded: Include superseded articles (default False).
        limit: Maximum number of results to return (default 20).
        k: RRF constant (default 60).

    Returns:
        List of article dicts with an additional 'rrf_score' field,
        sorted by rrf_score descending.
    """
    from kb.links import graph_search

    # --- Stream 1: FTS5 ---
    # Fetch generously so graph seeds are meaningful; filters applied after fusion
    fts_results = search_articles(
        query,
        agent_scope=agent,
        min_confidence=min_confidence,
        include_superseded=include_superseded,
        limit=50,
    )

    # --- Stream 2: Vector ---
    vector_results: List[Dict] = []
    try:
        from kb.embeddings import vector_search, _EMBEDDINGS_AVAILABLE
        if _EMBEDDINGS_AVAILABLE:
            vector_results = vector_search(query, limit=50)
    except (ImportError, Exception) as exc:
        logger.debug(f"Vector search unavailable, skipping: {exc}")

    # --- Stream 3: Graph ---
    seed_ids = [r["id"] for r in fts_results[:5]]
    graph_results = graph_search(seed_ids, max_depth=2, max_results=50) if seed_ids else []

    # --- Build per-article ranked positions ---
    # rrf_scores[article_id] accumulates 1/(k+rank) from every stream
    rrf_scores: Dict[str, float] = {}

    for rank, article in enumerate(fts_results, start=1):
        aid = article["id"]
        rrf_scores[aid] = rrf_scores.get(aid, 0.0) + 1.0 / (k + rank)

    for rank, article in enumerate(vector_results, start=1):
        aid = article["id"]
        rrf_scores[aid] = rrf_scores.get(aid, 0.0) + 1.0 / (k + rank)

    for rank, article in enumerate(graph_results, start=1):
        aid = article["article_id"]
        rrf_scores[aid] = rrf_scores.get(aid, 0.0) + 1.0 / (k + rank)

    if not rrf_scores:
        return []

    # Sort by RRF score descending
    ranked_ids = sorted(rrf_scores.keys(), key=lambda aid: rrf_scores[aid], reverse=True)

    # Build a lookup from all streams so we can enrich results without extra DB hits
    conn = get_connection()

    # Fetch full article metadata for all candidates
    placeholder = ",".join("?" * len(ranked_ids))
    cursor = conn.execute(
        f"""
        SELECT id, title, slug, classification, tags,
               created_at, updated_at, confidence_score,
               agent_scope, superseded_by
        FROM articles
        WHERE id IN ({placeholder})
        """,
        ranked_ids,
    )

    article_rows: Dict[str, object] = {row["id"]: row for row in cursor}

    results: List[Dict] = []
    for aid in ranked_ids:
        row = article_rows.get(aid)
        if row is None:
            # Graph hit for an article not in DB (shouldn't happen, skip)
            continue

        # Apply agent scope filter
        if agent and row["agent_scope"] is not None and row["agent_scope"] != agent:
            continue

        # Apply confidence filter
        conf = row["confidence_score"] or 0.0
        if conf < min_confidence:
            continue

        # Apply superseded filter
        if not include_superseded and row["superseded_by"] is not None:
            continue

        tags = []
        if row["tags"]:
            try:
                tags = json.loads(row["tags"])
            except Exception:
                pass

        results.append(
            {
                "id": row["id"],
                "title": row["title"],
                "slug": row["slug"],
                "classification": row["classification"],
                "tags": tags,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "confidence_score": conf,
                "rrf_score": rrf_scores[aid],
            }
        )

        if len(results) >= limit:
            break

    return results


def get_article_snippet(article_id: str, query: str, max_length: int = 200) -> str:
    """
    Get snippet from article matching search query.

    Args:
        article_id: Article UUID
        query: Search terms
        max_length: Max snippet length

    Returns:
        Snippet text with search terms highlighted
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT content_path FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        return ""

    content_path = row['content_path']
    root = get_project_root()
    full_path = root / content_path

    if not full_path.exists():
        return ""

    with open(full_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Strip frontmatter
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            content = parts[2]

    # Simple snippet extraction (find first occurrence of query term)
    query_lower = query.lower()
    content_lower = content.lower()

    # Try to find query in content
    pos = content_lower.find(query_lower)

    if pos == -1:
        # Query not found, return beginning
        snippet = content[:max_length]
    else:
        # Extract context around query
        start = max(0, pos - max_length // 2)
        end = min(len(content), pos + max_length // 2)
        snippet = content[start:end]

        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."

    return snippet.strip()


def rebuild_fts_index() -> int:
    """
    Rebuild FTS index from scratch.

    Clears existing FTS index and rebuilds from articles table.
    Uses BEGIN IMMEDIATE to prevent concurrent writes during rebuild.

    Returns:
        Number of articles indexed
    """
    conn = get_connection()

    logger.info("Rebuilding FTS index...")

    # IMMEDIATE lock prevents other writers during the full rebuild
    conn.execute("BEGIN IMMEDIATE")

    try:
        # Get all article IDs first (within the lock)
        cursor = conn.execute("""
            SELECT id FROM articles
        """)
        article_ids = [row['id'] for row in cursor]

        # Clear existing FTS index
        conn.execute("DELETE FROM articles_fts")

        # Rebuild index for each article
        for article_id in article_ids:
            update_article_fts(conn, article_id)

        conn.execute("COMMIT")

        logger.info(f"✓ Rebuilt FTS index for {len(article_ids)} articles")
        return len(article_ids)

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to rebuild FTS index: {e}")
        raise
