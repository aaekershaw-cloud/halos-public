"""Semantic embedding support using sentence-transformers.

Provides vector embeddings for articles stored in the article_embeddings table
(migration 008). Falls back gracefully if sentence-transformers is not installed.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Graceful degradation if sentence-transformers is not installed
try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
    _EMBEDDINGS_AVAILABLE = True
except ImportError:
    _EMBEDDINGS_AVAILABLE = False
    np = None  # type: ignore
    SentenceTransformer = None  # type: ignore
    logger.warning(
        "sentence-transformers not installed. Embedding functions will raise ImportError. "
        "Install with: pip install sentence-transformers"
    )

_MODEL_NAME = "all-MiniLM-L6-v2"
_EMBEDDING_DIM = 384
_model: Optional[object] = None  # SentenceTransformer instance cached here


def _require_embeddings() -> None:
    """Raise ImportError if sentence-transformers is not available."""
    if not _EMBEDDINGS_AVAILABLE:
        raise ImportError(
            "sentence-transformers is required for embedding functions. "
            "Install with: pip install sentence-transformers"
        )


def get_embedding_model() -> "SentenceTransformer":
    """
    Return the sentence-transformers model (lazy singleton).

    Loads 'all-MiniLM-L6-v2' on first call and caches it for subsequent calls.
    The model produces 384-dimensional float32 embeddings and is CPU-friendly
    at ~22 MB.

    Raises:
        ImportError: If sentence-transformers is not installed.
    """
    _require_embeddings()

    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {_MODEL_NAME}")
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info(f"Embedding model loaded ({_EMBEDDING_DIM}-dim)")

    return _model


def embed_text(text: str) -> bytes:
    """
    Encode a single string into a 384-dim float32 embedding BLOB.

    Args:
        text: Input text to embed.

    Returns:
        Raw bytes (float32 BLOB) suitable for storing in SQLite.

    Raises:
        ImportError: If sentence-transformers is not installed.
    """
    _require_embeddings()

    model = get_embedding_model()
    vector = model.encode([text], convert_to_numpy=True)  # shape (1, 384)
    return vector[0].astype(np.float32).tobytes()


def embed_article(article_id: str) -> None:
    """
    Compute and persist the embedding for a single article.

    Fetches the article's title + content from the database, embeds the
    combined text, then INSERT OR REPLACE into article_embeddings.

    Args:
        article_id: UUID of the article to embed.

    Raises:
        ImportError: If sentence-transformers is not installed.
        ValueError: If the article is not found.
    """
    _require_embeddings()

    from kb.db import get_connection
    from kb.config import get_project_root

    conn = get_connection()

    # Fetch article metadata
    cursor = conn.execute(
        "SELECT id, title, content_path FROM articles WHERE id = ?",
        (article_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"Article not found: {article_id}")

    title = row["title"]
    content_path = row["content_path"]

    # Read markdown content
    root = get_project_root()
    full_path = root / content_path

    content = ""
    if full_path.exists():
        with open(full_path, "r", encoding="utf-8") as f:
            raw = f.read()
        # Strip YAML frontmatter
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                content = parts[2]
            else:
                content = raw
        else:
            content = raw
    else:
        logger.warning(f"Content file not found for article {article_id}: {content_path}")

    # Combine title + content for richer embedding
    text = f"{title}\n\n{content}".strip()
    embedding_blob = embed_text(text)

    created_at = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT OR REPLACE INTO article_embeddings
            (article_id, embedding, embedding_dim, model_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (article_id, embedding_blob, _EMBEDDING_DIM, _MODEL_NAME, created_at),
    )
    conn.commit()

    logger.debug(f"Embedded article {article_id}")


def embed_all_articles(batch_size: int = 50) -> int:
    """
    Compute and persist embeddings for every article in the database.

    Uses model.encode() with batch processing for efficiency.

    Args:
        batch_size: Number of articles to encode in each batch.

    Returns:
        Number of articles embedded.

    Raises:
        ImportError: If sentence-transformers is not installed.
    """
    _require_embeddings()

    from kb.db import get_connection
    from kb.config import get_project_root

    conn = get_connection()
    root = get_project_root()
    model = get_embedding_model()

    # Fetch all articles
    cursor = conn.execute("SELECT id, title, content_path FROM articles")
    articles = [dict(row) for row in cursor]

    if not articles:
        logger.info("No articles found to embed")
        return 0

    logger.info(f"Embedding {len(articles)} articles in batches of {batch_size}")

    total_embedded = 0

    for batch_start in range(0, len(articles), batch_size):
        batch = articles[batch_start : batch_start + batch_size]

        texts: List[str] = []
        for article in batch:
            content_path = article["content_path"]
            full_path = root / content_path
            content = ""
            if full_path.exists():
                with open(full_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        content = parts[2]
                    else:
                        content = raw
                else:
                    content = raw
            texts.append(f"{article['title']}\n\n{content}".strip())

        # Batch encode
        vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)

        created_at = datetime.now(timezone.utc).isoformat()

        for article, vector in zip(batch, vectors):
            embedding_blob = vector.astype(np.float32).tobytes()
            conn.execute(
                """
                INSERT OR REPLACE INTO article_embeddings
                    (article_id, embedding, embedding_dim, model_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (article["id"], embedding_blob, _EMBEDDING_DIM, _MODEL_NAME, created_at),
            )

        conn.commit()
        total_embedded += len(batch)
        logger.info(f"Embedded {total_embedded}/{len(articles)} articles")

    return total_embedded


def get_article_embedding(article_id: str) -> Optional["np.ndarray"]:
    """
    Retrieve the stored embedding for an article as a numpy array.

    Args:
        article_id: UUID of the article.

    Returns:
        384-dim float32 numpy array, or None if no embedding is stored.

    Raises:
        ImportError: If sentence-transformers is not installed.
    """
    _require_embeddings()

    from kb.db import get_connection

    conn = get_connection()
    cursor = conn.execute(
        "SELECT embedding FROM article_embeddings WHERE article_id = ?",
        (article_id,),
    )
    row = cursor.fetchone()
    if not row or row["embedding"] is None:
        return None

    return np.frombuffer(row["embedding"], dtype=np.float32).copy()


def vector_search(query: str, limit: int = 20) -> List[Dict]:
    """
    Find articles most semantically similar to the query string.

    Embeds the query, fetches all stored article embeddings, computes cosine
    similarity, and returns the top results sorted by descending similarity.

    Args:
        query: Natural-language search query.
        limit: Maximum number of results to return (default 20).

    Returns:
        List of dicts with keys: id, title, slug, classification, tags,
        created_at, updated_at, similarity (float in [0, 1]).

    Raises:
        ImportError: If sentence-transformers is not installed.
    """
    _require_embeddings()

    from kb.db import get_connection

    conn = get_connection()

    # Embed the query
    query_blob = embed_text(query)
    query_vec = np.frombuffer(query_blob, dtype=np.float32).copy()

    # Fetch all stored embeddings joined with article metadata
    cursor = conn.execute(
        """
        SELECT
            a.id,
            a.title,
            a.slug,
            a.classification,
            a.tags,
            a.created_at,
            a.updated_at,
            ae.embedding
        FROM article_embeddings ae
        JOIN articles a ON a.id = ae.article_id
        WHERE ae.embedding IS NOT NULL
        """
    )
    rows = cursor.fetchall()

    if not rows:
        return []

    # Build matrix of article embeddings for vectorised cosine similarity
    article_ids: List[str] = []
    matrix_rows: List["np.ndarray"] = []

    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32).copy()
        article_ids.append(row["id"])
        matrix_rows.append(vec)

    matrix = np.stack(matrix_rows, axis=0)  # (N, 384)

    # Cosine similarity: dot(query, article) / (|query| * |article|)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    article_norms = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)
    similarities = article_norms @ query_norm  # (N,)

    # Sort descending and take top N
    top_indices = np.argsort(similarities)[::-1][:limit]

    # Build result list
    row_by_id: Dict[str, object] = {row["id"]: row for row in rows}
    results: List[Dict] = []

    import json

    for idx in top_indices:
        article_id = article_ids[idx]
        row = row_by_id[article_id]
        score = float(similarities[idx])

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
                "similarity": score,
            }
        )

    return results
