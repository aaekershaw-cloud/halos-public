"""Agent memory store.

Replaces per-agent `memory.md` flat files with a SQLite-backed store.
Each entry belongs to an agent + section (e.g. beta/short_term).

The `render_for_prompt()` output is what HalOS injects into Claude's
system prompt on every turn.
"""

import logging
from datetime import datetime
from typing import List, Dict, Optional

from kb.db import get_connection

logger = logging.getLogger(__name__)


# Sections roughly map to the old memory.md H2/H3 headings.
KNOWN_SECTIONS = (
    "short_term",
    "episodic",
    "long_term",
    "learned",
    "decisions",
    "open_questions",
    "identity",
    "family",
    "upcoming",
    "preferences",
    "fitness_log",
)

SECTION_ORDER = {name: i for i, name in enumerate(KNOWN_SECTIONS)}

# Human-readable headings used by render_for_prompt().
SECTION_HEADINGS = {
    "short_term": "Short-Term Memory",
    "episodic": "Session Summaries",
    "long_term": "Long-Term Memory",
    "learned": "Learned",
    "decisions": "Decisions",
    "open_questions": "Open Questions",
    "identity": "Identity Context",
    "family": "Family Reference",
    "upcoming": "Upcoming",
    "preferences": "Preferences",
    "fitness_log": "Fitness Log",
}


def _normalize_agent(agent: str) -> str:
    """Normalize an agent name so 'Beta' and 'beta' collide."""
    return (agent or "").strip().lower()


class MemoryStore:
    """CRUD + rendering for per-agent memory entries."""

    def __init__(self, conn=None):
        self._conn = conn or get_connection()

    # ------------------------------------------------------------------ writes

    def add_entry(self, agent: str, section: str, content: str, tags: str = "") -> int:
        """Insert a new entry. Returns the new row id."""
        agent = _normalize_agent(agent)
        section = (section or "").strip().lower()
        if not agent:
            raise ValueError("agent is required")
        if not section:
            raise ValueError("section is required")
        if not content or not content.strip():
            raise ValueError("content is required")

        now = datetime.utcnow().isoformat(timespec="seconds")
        cursor = self._conn.execute(
            """
            INSERT INTO agent_memory (agent, section, content, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent, section, content.strip(), tags or "", now, now),
        )
        return cursor.lastrowid

    def update_entry(self, entry_id: int, content: Optional[str] = None,
                     tags: Optional[str] = None) -> bool:
        """Update content/tags for an existing entry. Returns True if updated."""
        fields = []
        params: list = []
        if content is not None:
            fields.append("content = ?")
            params.append(content.strip())
        if tags is not None:
            fields.append("tags = ?")
            params.append(tags)
        if not fields:
            return False
        fields.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat(timespec="seconds"))
        params.append(entry_id)
        cursor = self._conn.execute(
            f"UPDATE agent_memory SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        return cursor.rowcount > 0

    def delete_entry(self, entry_id: int) -> bool:
        """Delete an entry by id. Returns True if a row was removed."""
        cursor = self._conn.execute(
            "DELETE FROM agent_memory WHERE id = ?", (entry_id,)
        )
        return cursor.rowcount > 0

    def promote_to_long_term(self, entry_id: int, target_section: str = "long_term") -> bool:
        """Move a short_term entry into a long-term section."""
        target_section = (target_section or "long_term").strip().lower()
        now = datetime.utcnow().isoformat(timespec="seconds")
        cursor = self._conn.execute(
            "UPDATE agent_memory SET section = ?, updated_at = ? WHERE id = ?",
            (target_section, now, entry_id),
        )
        return cursor.rowcount > 0

    def clear_section(self, agent: str, section: str) -> int:
        """Delete all entries for an agent in a given section. Returns count."""
        agent = _normalize_agent(agent)
        section = (section or "").strip().lower()
        cursor = self._conn.execute(
            "DELETE FROM agent_memory WHERE agent = ? AND section = ?",
            (agent, section),
        )
        return cursor.rowcount

    def clear_agent(self, agent: str) -> int:
        """Delete every entry for an agent. Used by the migration script."""
        agent = _normalize_agent(agent)
        cursor = self._conn.execute(
            "DELETE FROM agent_memory WHERE agent = ?", (agent,)
        )
        return cursor.rowcount

    # ------------------------------------------------------------------- reads

    def get_entry(self, entry_id: int) -> Optional[Dict]:
        row = self._conn.execute(
            "SELECT * FROM agent_memory WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_entries(self, agent: str, section: Optional[str] = None,
                    limit: Optional[int] = None) -> List[Dict]:
        """Return entries for agent, optionally filtered by section.

        Sorted by section (known-order first, then alpha) then updated_at DESC.
        """
        agent = _normalize_agent(agent)
        params: list = [agent]
        sql = "SELECT * FROM agent_memory WHERE agent = ?"
        if section:
            sql += " AND section = ?"
            params.append(section.strip().lower())
        sql += " ORDER BY section ASC, updated_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = self._conn.execute(sql, params).fetchall()
        entries = [dict(r) for r in rows]

        # Stable custom sort: known sections first in defined order, then alpha.
        entries.sort(key=lambda e: (
            SECTION_ORDER.get(e["section"], 999),
            e["section"],
            -_sort_key_ts(e.get("updated_at")),
            -int(e["id"]),
        ))
        return entries

    def search(self, agent: str, query: str, limit: Optional[int] = None) -> List[Dict]:
        """Case-insensitive substring match on content."""
        agent = _normalize_agent(agent)
        q = (query or "").strip()
        if not q:
            return []
        params: list = [agent, f"%{q.lower()}%"]
        sql = (
            "SELECT * FROM agent_memory WHERE agent = ? "
            "AND LOWER(content) LIKE ? "
            "ORDER BY updated_at DESC, id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_agents(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT agent FROM agent_memory ORDER BY agent"
        ).fetchall()
        return [r["agent"] for r in rows]

    def count(self, agent: Optional[str] = None) -> int:
        if agent:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agent_memory WHERE agent = ?",
                (_normalize_agent(agent),),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM agent_memory"
            ).fetchone()
        return int(row["n"]) if row else 0

    # --------------------------------------------------------------- rendering

    def render_for_prompt(self, agent: str, max_chars: int = 4000) -> str:
        """Render entries as Markdown for system-prompt injection."""
        entries = self.get_entries(agent)
        if not entries:
            return "## Memory\n(no entries yet)"

        # Group entries by section preserving SECTION_ORDER.
        grouped: Dict[str, List[Dict]] = {}
        for e in entries:
            grouped.setdefault(e["section"], []).append(e)

        def _sect_sort_key(s: str):
            return (SECTION_ORDER.get(s, 999), s)

        lines: List[str] = ["## Memory"]
        for section in sorted(grouped.keys(), key=_sect_sort_key):
            heading = SECTION_HEADINGS.get(
                section, section.replace("_", " ").title()
            )
            lines.append("")
            lines.append(f"### {heading}")
            for e in grouped[section]:
                content = (e.get("content") or "").strip()
                if not content:
                    continue
                # Multi-line entries get bullet + blockquote-free wrap.
                if "\n" in content:
                    first, *rest = content.splitlines()
                    lines.append(f"- {first}")
                    for extra in rest:
                        lines.append(f"  {extra}")
                else:
                    lines.append(f"- {content}")

        rendered = "\n".join(lines).rstrip() + "\n"
        if max_chars and len(rendered) > max_chars:
            rendered = rendered[:max_chars].rstrip() + "\n...[truncated]\n"
        return rendered


def _sort_key_ts(ts) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return 0.0
