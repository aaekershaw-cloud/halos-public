"""Embed commands - vector embedding management for articles"""

import json
import logging
import sys

logger = logging.getLogger(__name__)


def cmd_embed_article(article_id: str) -> None:
    """
    Compute and persist embedding for a single article.

    Args:
        article_id: UUID of the article to embed.
    """
    from kb.embeddings import embed_article, _EMBEDDINGS_AVAILABLE

    if not _EMBEDDINGS_AVAILABLE:
        print("✗ sentence-transformers is not installed.")
        print("  Install with: pip install sentence-transformers")
        sys.exit(1)

    print(f"Embedding article: {article_id}")

    try:
        embed_article(article_id)
        print(f"✓ Embedded article {article_id}")
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error embedding article: {e}")
        logger.error(f"embed_article failed for {article_id}: {e}", exc_info=True)
        sys.exit(1)


def cmd_embed_all(batch_size: int = 50) -> None:
    """
    Compute and persist embeddings for all articles.

    Args:
        batch_size: Number of articles per encode batch.
    """
    from kb.embeddings import embed_all_articles, _EMBEDDINGS_AVAILABLE

    if not _EMBEDDINGS_AVAILABLE:
        print("✗ sentence-transformers is not installed.")
        print("  Install with: pip install sentence-transformers")
        sys.exit(1)

    print(f"Embedding all articles (batch size: {batch_size})...")

    try:
        count = embed_all_articles(batch_size=batch_size)
        if count == 0:
            print("No articles found to embed.")
        else:
            print(f"✓ Embedded {count} article(s)")
    except Exception as e:
        print(f"✗ Error during batch embedding: {e}")
        logger.error(f"embed_all_articles failed: {e}", exc_info=True)
        sys.exit(1)


def cmd_embed_search(query: str, limit: int = 20, output_format: str = "text") -> None:
    """
    Perform vector (semantic) search against stored embeddings.

    Args:
        query: Natural-language search query.
        limit: Maximum number of results to return.
        output_format: 'text' or 'json'.
    """
    from kb.embeddings import vector_search, _EMBEDDINGS_AVAILABLE

    if not _EMBEDDINGS_AVAILABLE:
        print("✗ sentence-transformers is not installed.")
        print("  Install with: pip install sentence-transformers")
        sys.exit(1)

    print(f"Vector search: {query}")
    print()

    try:
        results = vector_search(query, limit=limit)
    except Exception as e:
        print(f"✗ Search error: {e}")
        logger.error(f"vector_search failed: {e}", exc_info=True)
        sys.exit(1)

    if not results:
        print("No results found.")
        return

    if output_format == "json":
        print(json.dumps(results, indent=2))
        return

    # Text format
    print(f"Found {len(results)} result(s):\n")

    for i, result in enumerate(results, 1):
        similarity_pct = f"{result['similarity'] * 100:.1f}%"
        print(f"{i}. {result['title']}  [{similarity_pct}]")
        print(f"   ID: {result['id']}")
        print(f"   Slug: {result['slug']}")
        print(f"   Classification: {result['classification']}")

        if result.get("tags"):
            print(f"   Tags: {', '.join(result['tags'])}")

        print(f"   Updated: {result['updated_at']}")
        print()
