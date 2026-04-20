"""Generate abbreviated KB index for agent context loading."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from kb.db import get_connection

logger = logging.getLogger(__name__)

# Map directory names from content_path to display categories
CATEGORY_MAP = {
    'concepts': 'Concepts',
    'projects': 'Projects',
    'sessions': 'Sessions',
    'agents': 'Agents',
    'public': 'Reference',
    'internal': 'Internal',
}

DEFAULT_CATEGORY = 'Uncategorized'


def _extract_category(content_path: str) -> str:
    """Derive category from the wiki subdirectory in content_path."""
    path = str(content_path)
    for dirname, label in CATEGORY_MAP.items():
        if f'/wiki/{dirname}/' in path or f'wiki/{dirname}/' in path:
            return label
    return DEFAULT_CATEGORY


def _parse_tags(tags_raw: Optional[str]) -> List[str]:
    """Parse JSON tags string into a list."""
    if not tags_raw:
        return []
    try:
        parsed = json.loads(tags_raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _first_sentence(content: str, max_len: int = 80) -> str:
    """Extract a one-line summary from article content."""
    # Strip frontmatter
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            content = parts[2]

    # Strip markdown headers and blank lines, get first meaningful line
    for line in content.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Strip markdown formatting
        line = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line)  # links
        line = re.sub(r'[*_`]+', '', line)  # bold/italic/code
        line = line.strip()
        if line:
            if len(line) > max_len:
                return line[:max_len - 3] + '...'
            return line
    return ''


def generate_index(
    agent: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> str:
    """
    Generate an abbreviated index of KB articles.

    Args:
        agent: Agent name (for future per-agent scoping). Currently generates
               a global index since articles don't have agent ownership yet.
        output_path: If provided, write index to this file path.

    Returns:
        The index as a markdown string.
    """
    conn = get_connection()

    if agent:
        cursor = conn.execute("""
            SELECT id, title, slug, classification, tags, content_path,
                   created_at, updated_at
            FROM articles
            WHERE deleted_at IS NULL
              AND (agent_scope IS NULL OR agent_scope = ?)
            ORDER BY updated_at DESC
        """, (agent,))
    else:
        cursor = conn.execute("""
            SELECT id, title, slug, classification, tags, content_path,
                   created_at, updated_at
            FROM articles
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC
        """)

    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        index = _format_empty_index(agent)
    else:
        # Group articles by category
        grouped: Dict[str, list] = {}
        for row in rows:
            category = _extract_category(row['content_path'])
            tags = _parse_tags(row['tags'])

            # Read first sentence for summary
            summary = _read_summary(row['content_path'])

            entry = {
                'slug': row['slug'],
                'title': row['title'],
                'tags': tags,
                'classification': row['classification'],
                'updated_at': row['updated_at'],
                'summary': summary,
            }

            grouped.setdefault(category, []).append(entry)

        index = _format_index(agent, total, grouped)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(index, encoding='utf-8')
        logger.info(f"Index written to {output_path}")

    return index


def _read_summary(content_path: str) -> str:
    """Read article file and extract first meaningful sentence."""
    from kb.config import get_project_root

    path = Path(content_path)
    if not path.is_absolute():
        path = get_project_root() / path

    try:
        content = path.read_text(encoding='utf-8')
        return _first_sentence(content)
    except (FileNotFoundError, PermissionError):
        return ''


def _format_empty_index(agent: Optional[str]) -> str:
    """Format index when KB has no articles."""
    now = datetime.now().strftime('%Y-%m-%d')
    header = f'# KB Index ({now})\n'
    if agent:
        header += f'Agent: {agent}\n'
    header += '\nNo articles in the knowledge base yet.\n'
    return header


def _format_index(
    agent: Optional[str],
    total: int,
    grouped: Dict[str, list],
) -> str:
    """Format the full index markdown."""
    now = datetime.now().strftime('%Y-%m-%d')
    lines = [f'# KB Index ({now})']
    if agent:
        lines.append(f'Agent: {agent}')
    lines.append(f'{total} articles')
    lines.append('')
    lines.append('Search for full article: `kb search "<slug or keywords>"`')
    lines.append('')

    # Sort categories: put Reference and Uncategorized last
    priority = ['Projects', 'Concepts', 'Agents', 'Sessions', 'Internal', 'Reference', 'Uncategorized']
    sorted_cats = sorted(
        grouped.keys(),
        key=lambda c: priority.index(c) if c in priority else len(priority)
    )

    for category in sorted_cats:
        entries = grouped[category]
        lines.append(f'## {category}')

        for entry in entries:
            tags_str = ''
            if entry['tags']:
                tags_str = f' [{", ".join(entry["tags"])}]'

            summary_str = ''
            if entry['summary']:
                summary_str = f' — {entry["summary"]}'

            lines.append(f'- **{entry["slug"]}**: {entry["title"]}{tags_str}{summary_str}')

        lines.append('')

    return '\n'.join(lines)
