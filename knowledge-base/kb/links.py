"""Link extraction and graph management"""

import re
import logging
from collections import deque
from typing import Dict, List, Set, Tuple, Optional
import frontmatter
from kb.db import get_connection
from kb.errors import PermanentError

logger = logging.getLogger(__name__)


# Supported link type vocabulary
LINK_TYPE_VOCABULARY = [
    "related",
    "wiki_link",
    "confirms",
    "contradicts",
    "supersedes",
    "refines",
    "depends_on",
    "uses",
    "supports",
    "mentions",
    "authored_by",
    "part_of"
]


def add_typed_link(source_id: str, target_id: str, link_type: str = "related") -> None:
    """
    Add a typed link between two articles.

    Args:
        source_id: Source article ID
        target_id: Target article ID
        link_type: Link type from LINK_TYPE_VOCABULARY

    Raises:
        ValueError: If link_type not in vocabulary
        PermanentError: If either article doesn't exist
    """
    if link_type not in LINK_TYPE_VOCABULARY:
        raise ValueError(
            f"Invalid link type '{link_type}'. Must be one of: {', '.join(LINK_TYPE_VOCABULARY)}"
        )

    conn = get_connection()

    # Verify both articles exist
    for article_id in [source_id, target_id]:
        cursor = conn.execute(
            "SELECT id FROM articles WHERE id = ? AND deleted_at IS NULL",
            (article_id,)
        )
        if not cursor.fetchone():
            raise PermanentError(f"Article {article_id} does not exist")

    # Insert link (UNIQUE constraint prevents duplicates)
    try:
        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO links (source_id, target_id, link_type)
            VALUES (?, ?, ?)
            ON CONFLICT (source_id, target_id, link_type) DO NOTHING
        """, (source_id, target_id, link_type))
        conn.execute("COMMIT")
        logger.info(f"Added {link_type} link: {source_id} -> {target_id}")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise PermanentError(f"Failed to add link: {e}")


def extract_wiki_links(content: str) -> List[str]:
    """
    Extract wiki-style links [[Article Name]] from markdown content.

    Args:
        content: Markdown content to parse

    Returns:
        List of article names/slugs referenced
    """
    # Match [[Article Name]] or [[article-slug]]
    pattern = r'\[\[([^\]]+)\]\]'
    matches = re.findall(pattern, content)

    # Clean up matches (strip whitespace, normalize)
    links = []
    for match in matches:
        # Handle [[Display Text|actual-slug]] format
        if '|' in match:
            parts = match.split('|')
            link = parts[-1].strip()  # Use the slug part
        else:
            link = match.strip()

        if link:
            links.append(link)

    return links


def extract_concept_references(content: str, tags: List[str]) -> List[str]:
    """
    Extract concept references from content and tags.

    Args:
        content: Markdown content
        tags: Article tags

    Returns:
        List of concept names
    """
    concepts = set()

    # Tags are concepts
    concepts.update(tags)

    # Look for "Related:" or "See also:" sections
    related_pattern = r'(?:Related|See also):\s*([^\n]+)'
    for match in re.finditer(related_pattern, content, re.IGNORECASE):
        # Split on commas or bullets
        items = re.split(r'[,\-•]', match.group(1))
        for item in items:
            item = item.strip().strip('[]')
            if item:
                concepts.add(item)

    return list(concepts)


def resolve_link_target(link_text: str) -> Optional[str]:
    """
    Resolve wiki link text to article ID.

    Tries to match by:
    1. Exact slug match
    2. Title match (case-insensitive)
    3. Fuzzy title match

    Args:
        link_text: Link text from [[...]]

    Returns:
        Article ID or None if not found
    """
    conn = get_connection()

    # Convert to slug format (lowercase, hyphenated)
    slug_candidate = link_text.lower().replace(' ', '-')

    # Try exact slug match
    cursor = conn.execute("""
        SELECT id FROM articles WHERE slug = ?
    """, (slug_candidate,))
    row = cursor.fetchone()
    if row:
        return row['id']

    # Try title match (case-insensitive)
    cursor = conn.execute("""
        SELECT id FROM articles WHERE LOWER(title) = ?
    """, (link_text.lower(),))
    row = cursor.fetchone()
    if row:
        return row['id']

    # Try LIKE match (fuzzy)
    cursor = conn.execute("""
        SELECT id FROM articles WHERE title LIKE ?
        LIMIT 1
    """, (f'%{link_text}%',))
    row = cursor.fetchone()
    if row:
        return row['id']

    return None


def update_article_links(article_id: str):
    """
    Extract and update links for an article.

    Parses article content for wiki links and updates links table.

    Args:
        article_id: Article ID to process
    """
    conn = get_connection()

    # Get article content
    cursor = conn.execute("""
        SELECT content_path, tags FROM articles WHERE id = ?
    """, (article_id,))
    row = cursor.fetchone()

    if not row:
        raise PermanentError(f"Article not found: {article_id}")

    content_path = row['content_path']

    # Parse tags
    import json
    tags = []
    if row['tags']:
        try:
            tags = json.loads(row['tags'])
        except:
            pass

    # Load content
    try:
        with open(content_path, 'r') as f:
            doc = frontmatter.load(f)
            content = doc.content
    except Exception as e:
        logger.error(f"Failed to read article content for {article_id}: {e}")
        return

    # Extract links
    wiki_links = extract_wiki_links(content)

    # Begin transaction
    conn.execute("BEGIN")

    try:
        # Remove existing links from this article
        conn.execute("""
            DELETE FROM links WHERE source_id = ?
        """, (article_id,))

        # Add new links
        links_added = 0

        for link_text in wiki_links:
            target_id = resolve_link_target(link_text)

            if target_id:
                # Add link (ignore duplicates)
                try:
                    conn.execute("""
                        INSERT INTO links (source_id, target_id, link_type)
                        VALUES (?, ?, 'wiki_link')
                    """, (article_id, target_id))
                    links_added += 1
                except:
                    # Duplicate or constraint violation
                    pass
            else:
                logger.warning(f"Could not resolve link: [[{link_text}]] in article {article_id}")

        conn.execute("COMMIT")

        logger.info(f"Updated links for article {article_id}: {links_added} links added")

    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Failed to update links for {article_id}: {e}")
        raise


def update_all_links() -> int:
    """
    Update links for all articles in database.

    Returns:
        Number of articles processed
    """
    conn = get_connection()

    cursor = conn.execute("SELECT id FROM articles")
    article_ids = [row['id'] for row in cursor]

    logger.info(f"Updating links for {len(article_ids)} articles...")

    processed = 0

    for article_id in article_ids:
        try:
            update_article_links(article_id)
            processed += 1
        except Exception as e:
            logger.error(f"Failed to update links for {article_id}: {e}")

    logger.info(f"✓ Updated links for {processed}/{len(article_ids)} articles")

    return processed


def get_article_links(article_id: str) -> Dict[str, List[Dict]]:
    """
    Get all links for an article (both outgoing and incoming).

    Args:
        article_id: Article ID

    Returns:
        {
            'outgoing': [{'id': ..., 'title': ..., 'slug': ...}],
            'incoming': [{'id': ..., 'title': ..., 'slug': ...}]
        }
    """
    conn = get_connection()

    # Outgoing links
    cursor = conn.execute("""
        SELECT a.id, a.title, a.slug
        FROM links l
        JOIN articles a ON a.id = l.target_id
        WHERE l.source_id = ?
    """, (article_id,))

    outgoing = [
        {'id': row['id'], 'title': row['title'], 'slug': row['slug']}
        for row in cursor
    ]

    # Incoming links
    cursor = conn.execute("""
        SELECT a.id, a.title, a.slug
        FROM links l
        JOIN articles a ON a.id = l.source_id
        WHERE l.target_id = ?
    """, (article_id,))

    incoming = [
        {'id': row['id'], 'title': row['title'], 'slug': row['slug']}
        for row in cursor
    ]

    return {
        'outgoing': outgoing,
        'incoming': incoming
    }


def find_related_articles(article_id: str, limit: int = 5) -> List[Dict]:
    """
    Find related articles based on link proximity and typed relationships.

    Uses:
    - Direct links (outgoing/incoming): 10 points each
    - Second-degree connections: 5 points
    - Shared tags: 2 points per shared tag
    - Typed relationships from article_relationships table (weighted by type)

    Args:
        article_id: Article ID
        limit: Max results to return

    Returns:
        List of related articles with scores
    """
    conn = get_connection()

    # Get article's tags
    cursor = conn.execute("""
        SELECT tags FROM articles WHERE id = ?
    """, (article_id,))
    row = cursor.fetchone()

    if not row:
        return []

    import json
    article_tags = set()
    if row['tags']:
        try:
            article_tags = set(json.loads(row['tags']))
        except:
            pass

    # Find related articles with scoring
    # - Direct links: 10 points
    # - Second-degree: 5 points
    # - Shared tag: 2 points each
    # - Typed relationships: weighted by relationship type

    related_scores = {}

    # Direct outgoing links (10 points)
    cursor = conn.execute("""
        SELECT target_id FROM links WHERE source_id = ?
    """, (article_id,))
    for row in cursor:
        target_id = row['target_id']
        related_scores[target_id] = related_scores.get(target_id, 0) + 10

    # Direct incoming links (10 points)
    cursor = conn.execute("""
        SELECT source_id FROM links WHERE target_id = ?
    """, (article_id,))
    for row in cursor:
        source_id = row['source_id']
        if source_id != article_id:
            related_scores[source_id] = related_scores.get(source_id, 0) + 10

    # Second-degree connections (5 points)
    cursor = conn.execute("""
        SELECT DISTINCT l2.target_id
        FROM links l1
        JOIN links l2 ON l1.target_id = l2.source_id
        WHERE l1.source_id = ? AND l2.target_id != ?
    """, (article_id, article_id))
    for row in cursor:
        target_id = row['target_id']
        if target_id != article_id:
            related_scores[target_id] = related_scores.get(target_id, 0) + 5

    # Shared tags (2 points per shared tag)
    if article_tags:
        cursor = conn.execute("""
            SELECT id, tags FROM articles WHERE id != ?
        """, (article_id,))

        for row in cursor:
            other_id = row['id']
            other_tags = set()

            if row['tags']:
                try:
                    other_tags = set(json.loads(row['tags']))
                except:
                    pass

            shared_tags = article_tags & other_tags
            if shared_tags:
                related_scores[other_id] = related_scores.get(other_id, 0) + (2 * len(shared_tags))

    # Typed relationships from article_relationships table
    # Check table exists before querying (migration may not be applied yet)
    table_check = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='article_relationships'
    """).fetchone()

    if table_check:
        from kb.relationships import RELATIONSHIP_WEIGHTS

        # Outgoing typed relationships
        cursor = conn.execute("""
            SELECT to_article_id, relationship_type
            FROM article_relationships
            WHERE from_article_id = ?
        """, (article_id,))
        for row in cursor:
            weight = RELATIONSHIP_WEIGHTS.get(row['relationship_type'], 5)
            target = row['to_article_id']
            if target != article_id:
                related_scores[target] = related_scores.get(target, 0) + weight

        # Incoming typed relationships
        cursor = conn.execute("""
            SELECT from_article_id, relationship_type
            FROM article_relationships
            WHERE to_article_id = ?
        """, (article_id,))
        for row in cursor:
            weight = RELATIONSHIP_WEIGHTS.get(row['relationship_type'], 5)
            source = row['from_article_id']
            if source != article_id:
                related_scores[source] = related_scores.get(source, 0) + weight

    # Sort by score and get top N
    sorted_related = sorted(
        related_scores.items(),
        key=lambda x: x[1],
        reverse=True
    )[:limit]

    # Get article details
    results = []
    for related_id, score in sorted_related:
        cursor = conn.execute("""
            SELECT id, title, slug, classification
            FROM articles
            WHERE id = ?
        """, (related_id,))

        row = cursor.fetchone()
        if row:
            results.append({
                'id': row['id'],
                'title': row['title'],
                'slug': row['slug'],
                'classification': row['classification'],
                'score': score
            })

    return results


# Weight multipliers for link types in graph traversal scoring
LINK_TYPE_WEIGHTS: Dict[str, float] = {
    "wiki_link": 1.0,
    "supersedes": 1.5,
    "refines": 1.3,
    "depends_on": 1.2,
    "uses": 1.1,
    "contradicts": 0.8,
    "supports": 1.2,
    "mentions": 0.7,
    "part_of": 1.3,
}


def graph_search(
    seed_article_ids: List[str],
    max_depth: int = 2,
    max_results: int = 20,
) -> List[Dict]:
    """
    BFS traversal from seed articles through the link graph.

    Traverses the links table (and article_relationships if it exists) up to
    max_depth hops from the seeds. Articles at shallower depth receive higher
    scores. Link type weights via LINK_TYPE_WEIGHTS are applied per edge.

    Args:
        seed_article_ids: Starting article IDs (excluded from results).
        max_depth: Maximum BFS depth (default 2).
        max_results: Maximum number of results to return (default 20).

    Returns:
        List of dicts with keys: article_id, title, graph_score.
        Sorted by graph_score descending.
    """
    if not seed_article_ids:
        return []

    conn = get_connection()

    # Check whether article_relationships table exists once
    rel_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='article_relationships'"
    ).fetchone() is not None

    seeds = set(seed_article_ids)
    # scores accumulate across multiple BFS paths to the same article
    scores: Dict[str, float] = {}
    # BFS queue: (article_id, depth)
    queue: deque = deque()
    visited: Set[str] = set(seeds)

    # Seed the queue with depth-1 neighbours of all seed articles
    for seed_id in seed_article_ids:
        queue.append((seed_id, 0))

    while queue:
        current_id, depth = queue.popleft()

        if depth >= max_depth:
            continue

        next_depth = depth + 1
        # Depth decay: depth-1 neighbours score 1.0, depth-2 score 0.5, etc.
        depth_weight = 1.0 / next_depth

        # Outgoing wiki links
        cursor = conn.execute(
            "SELECT target_id, link_type FROM links WHERE source_id = ?",
            (current_id,),
        )
        for row in cursor:
            neighbour = row["target_id"]
            if neighbour in seeds:
                continue
            type_weight = LINK_TYPE_WEIGHTS.get(row["link_type"] or "wiki_link", 1.0)
            scores[neighbour] = scores.get(neighbour, 0.0) + depth_weight * type_weight
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, next_depth))

        # Incoming wiki links (backlinks)
        cursor = conn.execute(
            "SELECT source_id, link_type FROM links WHERE target_id = ?",
            (current_id,),
        )
        for row in cursor:
            neighbour = row["source_id"]
            if neighbour in seeds:
                continue
            type_weight = LINK_TYPE_WEIGHTS.get(row["link_type"] or "wiki_link", 1.0)
            scores[neighbour] = scores.get(neighbour, 0.0) + depth_weight * type_weight
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, next_depth))

        # Typed relationships (both directions) if table exists
        if rel_table_exists:
            from kb.relationships import RELATIONSHIP_WEIGHTS

            cursor = conn.execute(
                """
                SELECT to_article_id AS neighbour_id, relationship_type
                FROM article_relationships WHERE from_article_id = ?
                UNION ALL
                SELECT from_article_id AS neighbour_id, relationship_type
                FROM article_relationships WHERE to_article_id = ?
                """,
                (current_id, current_id),
            )
            for row in cursor:
                neighbour = row["neighbour_id"]
                if neighbour in seeds:
                    continue
                # Normalise relationship weight to [0.5, 1.5] range for consistency
                rel_weight = RELATIONSHIP_WEIGHTS.get(row["relationship_type"], 5) / 10.0
                type_weight = LINK_TYPE_WEIGHTS.get(row["relationship_type"], rel_weight)
                scores[neighbour] = scores.get(neighbour, 0.0) + depth_weight * type_weight
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append((neighbour, next_depth))

    if not scores:
        return []

    # Sort by score descending and take top max_results
    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:max_results]

    # Fetch article metadata for the top results
    results: List[Dict] = []
    for article_id, graph_score in top:
        cursor = conn.execute(
            "SELECT id, title FROM articles WHERE id = ?",
            (article_id,),
        )
        row = cursor.fetchone()
        if row:
            results.append(
                {
                    "article_id": row["id"],
                    "title": row["title"],
                    "graph_score": graph_score,
                }
            )

    return results


def get_link_graph() -> Dict[str, List[str]]:
    """
    Get complete link graph as adjacency list.

    Returns:
        Dictionary mapping article ID to list of target IDs
    """
    conn = get_connection()

    cursor = conn.execute("""
        SELECT source_id, target_id FROM links
    """)

    graph = {}

    for row in cursor:
        source = row['source_id']
        target = row['target_id']

        if source not in graph:
            graph[source] = []

        graph[source].append(target)

    return graph


def get_link_stats() -> Dict[str, int]:
    """
    Get link statistics.

    Returns:
        {
            'total_links': int,
            'articles_with_links': int,
            'avg_outgoing_links': float,
            'max_outgoing_links': int
        }
    """
    conn = get_connection()

    # Total links
    cursor = conn.execute("SELECT COUNT(*) as count FROM links")
    total_links = cursor.fetchone()['count']

    # Articles with outgoing links
    cursor = conn.execute("""
        SELECT COUNT(DISTINCT source_id) as count FROM links
    """)
    articles_with_links = cursor.fetchone()['count']

    # Average outgoing links
    avg_outgoing = 0
    if articles_with_links > 0:
        avg_outgoing = total_links / articles_with_links

    # Max outgoing links
    cursor = conn.execute("""
        SELECT MAX(link_count) as max_count
        FROM (
            SELECT COUNT(*) as link_count
            FROM links
            GROUP BY source_id
        )
    """)
    row = cursor.fetchone()
    max_outgoing = row['max_count'] if row and row['max_count'] else 0

    return {
        'total_links': total_links,
        'articles_with_links': articles_with_links,
        'avg_outgoing_links': round(avg_outgoing, 2),
        'max_outgoing_links': max_outgoing
    }
