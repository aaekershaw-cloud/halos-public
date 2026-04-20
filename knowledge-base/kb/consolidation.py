"""Memory consolidation: promote short-term entries and compress sessions."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from kb.costs import record_cost
from kb.llm import call_llm, parse_llm_output
from kb.memory import KNOWN_SECTIONS, MemoryStore

logger = logging.getLogger(__name__)

# ── Prompts ──────────────────────────────────────────────────────────────

_CONSOLIDATE_SYSTEM = (
    "You are a memory-consolidation assistant. "
    "Extract durable knowledge from short-term session notes."
)

_CONSOLIDATE_PROMPT = """\
Below are short-term memory entries from an AI agent's recent sessions.
Extract lessons, facts, and decisions that are worth keeping long-term.

Return a JSON object with these arrays (each item is a plain string):
{
  "lessons": ["..."],   // Reusable insights or patterns learned
  "facts": ["..."],     // Factual information worth remembering
  "decisions": ["..."]  // Choices or commitments made
}

Only include entries that have lasting value. Omit trivial or transient notes.
If a category has nothing worth keeping, use an empty array.

--- BEGIN ENTRIES ---
%s
--- END ENTRIES ---
"""

_COMPRESS_SYSTEM = (
    "You are a memory-compression assistant. "
    "Summarise session observations into a brief note."
)

_COMPRESS_PROMPT = """\
Summarise the following short-term memory entries into 1-2 concise sentences
that capture the most important context from this session.
Return ONLY the summary text, no JSON or markdown formatting.

--- BEGIN ENTRIES ---
%s
--- END ENTRIES ---
"""

# Section mapping for consolidated items
_CONSOLIDATION_TARGETS = {
    "lessons": "learned",
    "facts": "long_term",
    "decisions": "decisions",
}


def consolidate_short_term(
    agent: str,
    llm_client: Any = None,
    limit: int = 50,
    dry_run: bool = False,
    model: str = "haiku",
) -> dict:
    """Promote old short-term entries into durable memory sections.

    Fetches the oldest *limit* short_term entries (skipping anything younger
    than 48 hours), asks an LLM to extract lessons/facts/decisions, writes
    them to the appropriate sections, and hard-deletes the originals.

    Args:
        agent: Agent name (e.g. "alpha").
        llm_client: Unused, reserved for future injection.
        limit: Max entries to review per run.
        dry_run: If True, run the LLM but skip all DB mutations.
        model: LLM model shortname (haiku, sonnet, opus).

    Returns:
        Stats dict with counts of work performed.
    """
    store = MemoryStore()
    entries = store.get_entries(agent, section="short_term")

    # Filter to entries older than 48 hours.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    eligible = []
    for e in entries:
        ts = e.get("created_at") or e.get("updated_at")
        if not ts:
            eligible.append(e)
            continue
        try:
            entry_dt = datetime.fromisoformat(str(ts))
            # Treat naive timestamps as UTC.
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            if entry_dt <= cutoff:
                eligible.append(e)
        except ValueError:
            eligible.append(e)

    # Sort oldest first, then apply limit.
    eligible.sort(key=lambda e: e.get("created_at") or e.get("updated_at") or "")
    eligible = eligible[:limit]

    stats: Dict[str, Any] = {
        "entries_reviewed": len(eligible),
        "long_term_added": 0,
        "learned_added": 0,
        "decisions_added": 0,
        "entries_deleted": 0,
        "summaries": {},
    }

    if not eligible:
        return stats

    # Build text block for LLM.
    text_block = "\n".join(
        f"[{e.get('created_at', '?')}] {e['content']}" for e in eligible
    )

    result = call_llm(
        prompt=_CONSOLIDATE_PROMPT % text_block,
        model=model,
        system=_CONSOLIDATE_SYSTEM,
    )

    record_cost(
        operation="consolidate_short_term",
        model=result["model"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
        cost_usd=result["cost_usd"],
    )

    parsed = parse_llm_output(result["content"], expected_format="json")

    # Normalise to expected structure.
    lessons = parsed.get("lessons") or []
    facts = parsed.get("facts") or []
    decisions = parsed.get("decisions") or []

    stats["summaries"] = {
        "lessons": lessons,
        "facts": facts,
        "decisions": decisions,
    }

    if not dry_run:
        for item in lessons:
            store.add_entry(agent, _CONSOLIDATION_TARGETS["lessons"], item)
            stats["learned_added"] += 1

        for item in facts:
            store.add_entry(agent, _CONSOLIDATION_TARGETS["facts"], item)
            stats["long_term_added"] += 1

        for item in decisions:
            store.add_entry(agent, _CONSOLIDATION_TARGETS["decisions"], item)
            stats["decisions_added"] += 1

        for e in eligible:
            store.delete_entry(e["id"])
            stats["entries_deleted"] += 1

    return stats


def compress_session(
    agent: str,
    llm_client: Any = None,
    model: str = "haiku",
    target_section: str = "episodic",
    dry_run: bool = False,
) -> dict:
    """Compress all short-term entries into a single session summary.

    Asks an LLM for a 1-2 sentence summary, writes it to the target section
    (default ``episodic``), and clears short_term.

    Args:
        agent: Agent name.
        llm_client: Unused, reserved for future injection.
        model: LLM model shortname.
        target_section: Where to store the summary (episodic or long_term).
        dry_run: If True, run the LLM but skip all DB mutations.

    Returns:
        Stats dict describing the compression result.
    """
    store = MemoryStore()
    entries = store.get_entries(agent, section="short_term")

    if not entries:
        return {
            "entries_compressed": 0,
            "summary_text": "",
            "target_section": target_section,
        }

    # Fall back to long_term if the requested section is not recognised.
    if target_section not in KNOWN_SECTIONS:
        logger.warning(
            "Target section %r not in KNOWN_SECTIONS, falling back to long_term",
            target_section,
        )
        target_section = "long_term"

    text_block = "\n".join(
        f"[{e.get('created_at', '?')}] {e['content']}" for e in entries
    )

    result = call_llm(
        prompt=_COMPRESS_PROMPT % text_block,
        model=model,
        system=_COMPRESS_SYSTEM,
    )

    record_cost(
        operation="compress_session",
        model=result["model"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
        cost_usd=result["cost_usd"],
    )

    summary_text = result["content"].strip()

    if not dry_run:
        store.add_entry(agent, target_section, summary_text)
        store.clear_section(agent, "short_term")

    return {
        "entries_compressed": len(entries),
        "summary_text": summary_text,
        "target_section": target_section,
    }
