"""Session lifecycle hooks - load context on start, compress memory on end."""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def on_session_start(session_id: str = "", agent_name: str = "", project_dir: str = "", **kwargs) -> dict:
    """Load agent-scoped articles and memory when a session begins.

    Args:
        session_id: Unique identifier for this session.
        agent_name: Name of the agent starting the session (e.g. "alpha").
        project_dir: Working directory / project context for the session.
        **kwargs: Ignored extra payload fields.

    Returns:
        {"markdown": "...", "article_count": N}
    """
    try:
        return _load_session_context(session_id, agent_name, project_dir)
    except Exception as exc:
        logger.warning(
            f"session_hooks.on_session_start failed for agent '{agent_name}' "
            f"session '{session_id}': {exc}",
            exc_info=True,
        )
        return {"markdown": "", "article_count": 0}


def _load_session_context(session_id: str, agent_name: str, project_dir: str) -> dict:
    from kb.db import get_connection
    from kb.memory import MemoryStore

    parts = []
    article_count = 0

    # --- Agent-scoped articles ---
    try:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT id, title, slug, classification
            FROM articles
            WHERE agent_scope = ? OR agent_scope IS NULL
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (agent_name,),
        ).fetchall()

        if rows:
            article_count = len(rows)
            parts.append(f"## Articles ({article_count})")
            for r in rows:
                scope = f" [{r['agent_scope']}]" if r.get("agent_scope") else ""
                parts.append(f"- {r['title']}{scope} (`{r['slug']}`)")
    except Exception as exc:
        logger.debug(f"session_hooks: could not load articles: {exc}")

    # --- Agent memory ---
    try:
        if agent_name:
            store = MemoryStore()
            memory_md = store.render_for_prompt(agent_name)
            if memory_md and "(no entries yet)" not in memory_md:
                parts.append(memory_md)
    except Exception as exc:
        logger.debug(f"session_hooks: could not load memory: {exc}")

    markdown = "\n\n".join(parts) if parts else ""
    return {"markdown": markdown, "article_count": article_count}


def on_session_end(session_id: str = "", agent_name: str = "", summary: str = "", **kwargs) -> dict:
    """Compress agent short-term memory when a session ends.

    Calls compress_session from kb.consolidation to summarise the session's
    short-term memory entries into the episodic section.

    Args:
        session_id: Unique identifier for the ending session.
        agent_name: Name of the agent ending the session.
        summary: Optional external summary to store alongside compression.
        **kwargs: Ignored extra payload fields.

    Returns:
        {"compressed": True, "summary": "..."}
    """
    try:
        return _compress_session(session_id, agent_name, summary)
    except Exception as exc:
        logger.warning(
            f"session_hooks.on_session_end failed for agent '{agent_name}' "
            f"session '{session_id}': {exc}",
            exc_info=True,
        )
        return {"compressed": False, "summary": ""}


def _compress_session(session_id: str, agent_name: str, summary: str) -> Dict:
    from kb.consolidation import compress_session

    if not agent_name:
        logger.debug("session_hooks.on_session_end: no agent_name, skipping compression")
        return {"compressed": False, "summary": summary}

    result = compress_session(agent_name)
    summary_text = result.get("summary_text") or summary or ""

    return {"compressed": True, "summary": summary_text}
