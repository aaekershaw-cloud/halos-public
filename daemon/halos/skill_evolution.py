"""Self-evolving skill system for HalOS.

Inspired by GenericAgent's skill crystallization, this module gives HalOS agents
an L1 Insight Index (skill routing) and L3 Skills layer (reusable SOPs) that
grow automatically as agents solve tasks.

Skills are stored locally in HalOS SQLite for fast lookup. A background
crystallization pass distills successful multi-tool turns into reusable
workflows.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Heuristic thresholds for auto-crystallization
_MIN_TOOLS_FOR_CRYSTALLIZE = 2
_MIN_RESULT_CHARS = 200
_MAX_RESULT_CHARS_FOR_CRYSTALLIZE = 8000  # avoid huge paste dumps

_SKILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    name TEXT NOT NULL,
    trigger_patterns TEXT NOT NULL,  -- JSON array of trigger phrases
    content TEXT NOT NULL,            -- SOP / instructions
    usage_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    last_used TIMESTAMP,
    embedding BLOB,                   -- float32 vector bytes
    embedding_dim INTEGER,            -- dimensionality (e.g. 384)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent, name)
);

CREATE INDEX IF NOT EXISTS idx_skills_agent ON skills(agent);

CREATE TABLE IF NOT EXISTS session_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_facts TEXT,  -- JSON array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archives_agent ON session_archives(agent);
"""


@dataclass
class CrystallizeResult:
    worthy: bool
    skill_id: Optional[int] = None
    archive_id: Optional[int] = None
    name: str = ""
    reason: str = ""


class SkillEvolver:
    """Manages skill discovery, injection, and crystallization for an agent."""

    def __init__(self, db, claude_api=None, kimi_api=None):
        """
        Args:
            db: HalOS Database instance (aiosqlite.Connection via Database wrapper).
            claude_api: Optional ClaudeAPI for lightweight crystallization calls.
            kimi_api: Optional KimiAPI fallback for crystallization.
        """
        self.db = db
        self.claude_api = claude_api
        self.kimi_api = kimi_api

    async def ensure_schema(self):
        """Create skills + archives tables if missing; migrate embedding columns."""
        if not (self.db and self.db.db):
            return
        await self.db.db.executescript(_SKILLS_SCHEMA)
        # Graceful migration: add embedding columns if table existed before v2
        for col_sql in [
            "ALTER TABLE skills ADD COLUMN embedding BLOB",
            "ALTER TABLE skills ADD COLUMN embedding_dim INTEGER",
        ]:
            try:
                await self.db.db.execute(col_sql)
            except Exception:
                pass  # Column already exists
        await self.db.db.commit()

    # ------------------------------------------------------------------
    # L1 Insight Index — skill retrieval
    # ------------------------------------------------------------------

    async def find_skills(
        self,
        agent: str,
        message: str,
        limit: int = 3,
        min_score: float = 0.3,
    ) -> list[dict]:
        """Return relevant skills for an agent ranked by semantic similarity.

        Tries embedding-based cosine similarity first (if sentence-transformers
        is available and skills have embeddings). Falls back to keyword overlap
        against trigger_patterns otherwise.
        """
        agent = (agent or "").strip().lower()
        if not agent or not message:
            return []

        rows = await self._get_skills_for_agent(agent)
        if not rows:
            return []

        # --- Attempt 1: embedding cosine similarity ---
        msg_emb = _embed_text(message)
        if msg_emb is not None:
            scored = []
            msg_vec = _bytes_to_vector(msg_emb)
            for row in rows:
                emb_blob = row.get("embedding")
                emb_dim = row.get("embedding_dim")
                if emb_blob and emb_dim:
                    skill_vec = _bytes_to_vector(emb_blob, emb_dim)
                    if skill_vec is not None:
                        sim = _cosine_similarity(msg_vec, skill_vec)
                        if sim >= min_score:
                            scored.append((sim, row))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                return [dict(r) for _, r in scored[:limit]]

        # --- Attempt 2: keyword overlap fallback ---
        msg_tokens = set(_tokenize(message))
        scored = []
        for row in rows:
            try:
                triggers = json.loads(row["trigger_patterns"] or "[]")
            except json.JSONDecodeError:
                triggers = []
            if not triggers:
                continue

            trigger_tokens = set()
            for t in triggers:
                trigger_tokens.update(_tokenize(t))

            if not trigger_tokens:
                continue

            overlap = len(msg_tokens & trigger_tokens)
            score = overlap / max(len(trigger_tokens), 1)
            if score >= min_score:
                scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [dict(r) for _, r in scored[:limit]]

    async def render_skills_for_prompt(self, skills: list[dict]) -> str:
        """Format matched skills as Markdown for system-prompt injection."""
        if not skills:
            return ""
        lines = ["## Relevant Skills"]
        for s in skills:
            name = s.get("name", "Unnamed")
            content = (s.get("content") or "").strip()
            uses = s.get("usage_count", 0)
            uses_note = f" (used {uses}x)" if uses else ""
            lines.append(f"\n### {name}{uses_note}")
            for paragraph in content.splitlines():
                lines.append(f"{paragraph}")
        return "\n".join(lines)

    async def record_skill_use(self, skill_id: int, success: bool = True):
        """Bump usage counters."""
        if not self.db or not self.db.db:
            return
        await self.db.db.execute(
            """UPDATE skills
               SET usage_count = usage_count + 1,
                   success_count = success_count + ?,
                   last_used = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (1 if success else 0, skill_id),
        )
        await self.db.db.commit()

    # ------------------------------------------------------------------
    # L3 Skill crystallization
    # ------------------------------------------------------------------

    async def crystallize_turn(
        self,
        agent: str,
        source: str,
        instruction: str,
        result_text: str,
        tool_calls: list[str],
        model: str = "haiku",
    ) -> CrystallizeResult:
        """Analyze a completed turn and optionally crystallize it into a skill.

        This is designed to be fire-and-forget: callers should wrap it in
        asyncio.create_task() so it never blocks the reply path.
        """
        agent = (agent or "").strip().lower()
        if not agent:
            return CrystallizeResult(worthy=False, reason="no agent")

        # --- Heuristic gate: skip obviously unsuitable turns ---
        if not tool_calls or len(tool_calls) < _MIN_TOOLS_FOR_CRYSTALLIZE:
            return CrystallizeResult(worthy=False, reason="too few tools")

        result_len = len(result_text or "")
        if result_len < _MIN_RESULT_CHARS:
            return CrystallizeResult(worthy=False, reason="result too short")
        if result_len > _MAX_RESULT_CHARS_FOR_CRYSTALLIZE:
            return CrystallizeResult(worthy=False, reason="result too long")

        if result_text and any(p in result_text.lower() for p in (
            "error:", "exception:", "sorry, something went wrong",
            "failed to", "could not", "unable to",
        )):
            return CrystallizeResult(worthy=False, reason="likely error result")

        # --- Ask a cheap LLM whether this turn is novel ---
        analysis = await self._analyze_turn(
            agent=agent,
            instruction=instruction,
            result_text=result_text,
            tool_calls=tool_calls,
            model=model,
        )
        if not analysis.get("worthy"):
            return CrystallizeResult(
                worthy=False,
                reason=analysis.get("reason", "LLM deemed unworthy"),
            )

        # --- Persist skill ---
        skill_id = await self._upsert_skill(
            agent=agent,
            name=analysis["name"],
            trigger_patterns=analysis.get("triggers", []),
            content=analysis.get("sop", ""),
        )

        # --- Persist L4 archive ---
        archive_id = await self._add_archive(
            agent=agent,
            source=source,
            summary=analysis.get("summary", ""),
            key_facts=analysis.get("key_facts", []),
        )

        logger.info(
            f"SkillEvolver: crystallized turn for {agent} -> "
            f"skill='{analysis['name']}' id={skill_id} archive={archive_id}"
        )
        return CrystallizeResult(
            worthy=True,
            skill_id=skill_id,
            archive_id=archive_id,
            name=analysis["name"],
        )

    async def _analyze_turn(
        self,
        agent: str,
        instruction: str,
        result_text: str,
        tool_calls: list[str],
        model: str = "haiku",
    ) -> dict:
        """Lightweight LLM call to decide if a turn is worth crystallizing."""
        prompt = _CRYSTALLIZE_PROMPT.format(
            agent=agent,
            instruction=instruction[:2000],
            tools=", ".join(tool_calls),
            result=result_text[:3000],
        )

        system = (
            "You are a skill-crystallization engine. "
            "You decide whether an agent turn represents a reusable workflow. "
            "Respond ONLY with valid JSON. No markdown, no commentary."
        )

        raw = ""
        try:
            if self.claude_api:
                raw = await self.claude_api.complete(prompt=prompt, system=system, model=model)
            elif self.kimi_api:
                raw = await self.kimi_api.complete(prompt=prompt, system=system)
            else:
                return {"worthy": False, "reason": "no LLM backend available"}
        except Exception as e:
            logger.warning(f"SkillEvolver: LLM analysis failed: {e}")
            return {"worthy": False, "reason": f"LLM error: {e}"}

        return _extract_json(raw)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _get_skills_for_agent(self, agent: str) -> list:
        if not self.db or not self.db.db:
            return []
        cursor = await self.db.db.execute(
            "SELECT * FROM skills WHERE agent = ? ORDER BY usage_count DESC, created_at DESC",
            (agent,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def _upsert_skill(
        self,
        agent: str,
        name: str,
        trigger_patterns: list[str],
        content: str,
    ) -> int:
        """Insert or update a skill. Returns skill id."""
        if not self.db or not self.db.db:
            return -1
        name = name.strip()
        triggers_json = json.dumps(trigger_patterns, ensure_ascii=False)
        # Generate embedding for semantic search
        emb_blob = _embed_text(content)
        emb_dim = _EMBEDDING_DIM if emb_blob else None
        cursor = await self.db.db.execute(
            """INSERT INTO skills (agent, name, trigger_patterns, content, embedding, embedding_dim)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent, name) DO UPDATE SET
                 trigger_patterns = excluded.trigger_patterns,
                 content = excluded.content,
                 embedding = excluded.embedding,
                 embedding_dim = excluded.embedding_dim,
                 created_at = CURRENT_TIMESTAMP
               RETURNING id""",
            (agent, name, triggers_json, content.strip(), emb_blob, emb_dim),
        )
        row = await cursor.fetchone()
        await self.db.db.commit()
        return row[0] if row else -1

    async def _add_archive(
        self,
        agent: str,
        source: str,
        summary: str,
        key_facts: list[str],
    ) -> int:
        if not self.db or not self.db.db:
            return -1
        cursor = await self.db.db.execute(
            """INSERT INTO session_archives (agent, source, summary, key_facts)
               VALUES (?, ?, ?, ?)
               RETURNING id""",
            (agent, source, summary.strip(), json.dumps(key_facts, ensure_ascii=False)),
        )
        row = await cursor.fetchone()
        await self.db.db.commit()
        return row[0] if row else -1

    async def list_skills(self, agent: str) -> list[dict]:
        """Human-facing list of skills for an agent."""
        return await self._get_skills_for_agent(agent)

    async def delete_skill(self, skill_id: int) -> bool:
        if not self.db or not self.db.db:
            return False
        cursor = await self.db.db.execute(
            "DELETE FROM skills WHERE id = ?", (skill_id,)
        )
        await self.db.db.commit()
        return cursor.rowcount > 0

    async def delete_skill_by_name(self, agent: str, name: str) -> bool:
        """Delete a skill by agent + name (case-insensitive)."""
        if not self.db or not self.db.db:
            return False
        agent = (agent or "").strip().lower()
        name = (name or "").strip()
        cursor = await self.db.db.execute(
            "DELETE FROM skills WHERE agent = ? AND name = ?",
            (agent, name),
        )
        await self.db.db.commit()
        return cursor.rowcount > 0


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_CRYSTALLIZE_PROMPT = """Analyze the following agent turn and decide whether it represents a novel, reusable workflow worth saving as a skill.

AGENT: {agent}
USER REQUEST: {instruction}
TOOLS USED: {tools}
RESULT: {result}

Rules:
1. Only crystallize if the turn involved MULTIPLE tools or a non-obvious sequence.
2. Do NOT crystallize simple Q&A, greetings, chitchat, or single-tool lookups.
3. Do NOT crystallize if the result contains errors, apologies, or failures.
4. Prefer tasks involving: file operations, API calls, debugging, configuration, multi-step reasoning, data processing.
5. If a very similar skill likely already exists, do NOT crystallize again.

Output valid JSON only. No markdown fences, no commentary.

{{
  "worthy": true,
  "name": "Short skill name (max 5 words)",
  "triggers": ["phrase 1", "phrase 2", "phrase 3"],
  "sop": "Concise step-by-step instructions for repeating this task. Include exact commands or code snippets if they were key to success.",
  "summary": "One-line description of what was accomplished.",
  "key_facts": ["key fact 1", "key fact 2"]
}}

If not worthy:
{{
  "worthy": false,
  "reason": "brief reason"
}}
"""


def _tokenize(text: str) -> set[str]:
    """Simple normalization for overlap scoring."""
    text = text.lower()
    # Keep alphanumeric + spaces, drop punctuation
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = {t for t in text.split() if len(t) > 2}
    return tokens


def _extract_json(text: str) -> dict:
    """Robust JSON extraction from LLM output that might include markdown or fluff."""
    text = text or ""
    # Try stripping markdown fences
    if "```json" in text:
        text = text.split("```json")[-1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[-1].split("```")[0]

    text = text.strip()
    # Find outermost braces
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {"worthy": False, "reason": "no JSON object found"}

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        logger.debug(f"SkillEvolver: JSON parse failed: {e}")
        return {"worthy": False, "reason": f"JSON parse error: {e}"}


# ------------------------------------------------------------------
# Embedding helpers (optional — falls back to keyword if unavailable)
# ------------------------------------------------------------------

_EMBEDDING_DIM: int | None = None


def _embed_text(text: str) -> bytes | None:
    """Encode text into a float32 embedding BLOB using KB sentence-transformers.

    Returns None if sentence-transformers is not installed or text is empty.
    """
    global _EMBEDDING_DIM
    if not text or not text.strip():
        return None
    try:
        # KB is on sys.path via claude_code.py / db.py
        from kb.embeddings import embed_text as _kb_embed_text, _EMBEDDING_DIM as _dim

        blob = _kb_embed_text(text.strip())
        _EMBEDDING_DIM = _dim
        return blob
    except Exception as e:
        logger.debug(f"SkillEvolver: embedding unavailable ({e}), will fallback to keyword")
        return None


def _bytes_to_vector(blob: bytes, dim: int | None = None) -> "np.ndarray" | None:
    """Convert a float32 BLOB back to a numpy vector."""
    try:
        import numpy as np

        target_dim = dim or _EMBEDDING_DIM or 384
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.shape[0] != target_dim:
            # Defensive: if blob size doesn't match, try reshaping or ignore
            return None
        return vec
    except Exception:
        return None


def _cosine_similarity(a: "np.ndarray", b: "np.ndarray") -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 on zero vectors."""
    try:
        import numpy as np

        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))
    except Exception:
        return 0.0
