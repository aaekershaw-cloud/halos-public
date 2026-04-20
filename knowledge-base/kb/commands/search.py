"""Search command implementation"""

import json
import logging
from pathlib import Path
from kb.search import search_articles, get_article_snippet
from kb.config import get_project_root
from kb.db import get_connection

logger = logging.getLogger(__name__)


def _read_full_article(article_id: str) -> str:
    """Read full article content from disk."""
    conn = get_connection()
    cursor = conn.execute("SELECT content_path FROM articles WHERE id = ?", (article_id,))
    row = cursor.fetchone()
    if not row:
        return ''

    content_path = row['content_path']
    path = Path(content_path)
    if not path.is_absolute():
        path = get_project_root() / path

    try:
        content = path.read_text(encoding='utf-8')
        # Strip frontmatter
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                content = parts[2].strip()
        return content
    except (FileNotFoundError, PermissionError):
        return ''


def cmd_search(query: str, tag: str = None, classification: str = None,
               since: str = None, format: str = 'text', agent: str = None):
    """
    Search wiki articles.

    Args:
        query: Search query
        tag: Filter by tag
        classification: Filter by classification
        since: Filter by date (YYYY-MM-DD)
        format: Output format (text or json)
        agent: Filter by agent scope
    """
    print(f"Searching for: {query}")

    if tag:
        print(f"  Tag filter: {tag}")
    if classification:
        print(f"  Classification filter: {classification}")
    if since:
        print(f"  Since: {since}")
    if agent:
        print(f"  Agent scope: {agent}")

    print()

    try:
        results = search_articles(
            query=query,
            tag=tag,
            classification=classification,
            since=since,
            limit=20,
            agent_scope=agent
        )

        if not results:
            print("No results found.")
            return

        if format == 'json':
            print(json.dumps(results, indent=2))
            return

        # Text format
        print(f"Found {len(results)} result(s):\n")

        for i, result in enumerate(results, 1):
            print(f"{i}. {result['title']}")
            print(f"   ID: {result['id']}")
            print(f"   Slug: {result['slug']}")
            print(f"   Classification: {result['classification']}")

            if result['tags']:
                print(f"   Tags: {', '.join(result['tags'])}")

            print(f"   Updated: {result['updated_at']}")

            # Show full content or snippet based on format flag
            if format == 'text':
                full = _read_full_article(result['id'])
                if full:
                    print(f"   ---")
                    for line in full.splitlines():
                        print(f"   {line}")
                    print(f"   ---")
            else:
                try:
                    snippet = get_article_snippet(result['id'], query)
                    if snippet:
                        print(f"   Snippet: {snippet[:150]}...")
                except:
                    pass

            print()

    except Exception as e:
        print(f"✗ Search error: {e}")
        logger.error(f"Search error: {e}", exc_info=True)
