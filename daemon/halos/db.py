"""Database setup and helpers for HalOS."""

import json
import logging
import os
import sys
from typing import Optional
import aiosqlite
from pathlib import Path

logger = logging.getLogger(__name__)

# Add KB to Python path for hook imports
_KB_DIR = os.environ.get(
    "HALOS_KB_DIR",
    str(Path.home() / "Projects" / "knowledge-base"),
)
if _KB_DIR not in sys.path:
    sys.path.insert(0, _KB_DIR)

try:
    from kb.hooks.session_hooks import on_session_end
    KB_HOOKS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"KB session hooks unavailable in db.py: {e}")
    KB_HOOKS_AVAILABLE = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, key)
);

CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY,
    task_type TEXT NOT NULL,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    scheduled_for TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    result TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY,
    task_type TEXT NOT NULL,
    duration_ms INTEGER,
    tokens_used INTEGER,
    cost_cents REAL,
    success BOOLEAN,
    summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    task_type TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    payload TEXT,
    enabled BOOLEAN DEFAULT 1,
    last_run TIMESTAMP,
    next_run TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    is_default BOOLEAN DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    last_message_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    content TEXT NOT NULL,
    delivered BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    started_ts TIMESTAMP NOT NULL,
    finished_ts TIMESTAMP,
    status TEXT NOT NULL,
    error_class TEXT,
    error_msg TEXT,
    is_retry BOOLEAN DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_stask_runs_name_ts
    ON scheduled_task_runs(task_name, started_ts DESC);

CREATE TABLE IF NOT EXISTS scheduler_reload (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    requested_at TIMESTAMP,
    processed_at TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db = None  # aiosqlite.Connection

    async def connect(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        # WAL mode: multiple readers + one writer can coexist without locking.
        # busy_timeout: SQLite retries automatically for up to 5s instead of
        # raising "database is locked" immediately.
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        await self._run_migrations()

    async def _run_migrations(self):
        """Add new columns if they don't exist yet."""
        for sql in [
            "ALTER TABLE sessions ADD COLUMN model TEXT DEFAULT 'sonnet'",
            "ALTER TABLE sessions ADD COLUMN total_cost_usd REAL DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN total_turns INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN engine TEXT DEFAULT 'claude'",
            # RALPLAN-DR v2.1 ADR-003: per-task outcome tracking
            "ALTER TABLE scheduled_tasks ADD COLUMN last_run_status TEXT",
            "ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT",
            "ALTER TABLE scheduled_tasks ADD COLUMN last_success_ts TIMESTAMP",
            # Auto-retry: flag rows that were fired by _poll_missed_runs so the
            # poll doesn't loop retries and we can tell organic vs retry runs.
            "ALTER TABLE scheduled_task_runs ADD COLUMN is_retry BOOLEAN DEFAULT 0",
        ]:
            try:
                await self.db.execute(sql)
                await self.db.commit()
            except Exception:
                pass  # Column already exists

        # Normalize session names to lowercase to prevent case-mismatch bugs.
        # Config uses mixed case (e.g. "Alpha") but code paths use .lower().
        # Without this, stale mixed-case rows become zombies that block lookups.
        try:
            # Delete duplicate mixed-case rows (keep the one with highest message_count)
            await self.db.execute("""
                DELETE FROM sessions WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY LOWER(name)
                            ORDER BY message_count DESC, id DESC
                        ) as rn FROM sessions
                    ) WHERE rn = 1
                )
            """)
            # Normalize remaining rows to lowercase
            await self.db.execute("UPDATE sessions SET name = LOWER(name) WHERE name <> LOWER(name)")
            await self.db.commit()
        except Exception:
            pass  # Window functions require SQLite 3.25+; harmless if it fails

    async def close(self):
        if self.db:
            await self.db.close()

    # --- Conversations ---

    async def add_message(self, source: str, role: str, content: str):
        await self.db.execute(
            "INSERT INTO conversations (source, role, content) VALUES (?, ?, ?)",
            (source, role, content),
        )
        await self.db.commit()

    async def get_messages_since(self, source: str, last_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT id, role, content FROM conversations WHERE source = ? AND id > ? ORDER BY id ASC",
            (source, last_id),
        )
        rows = await cursor.fetchall()
        return [{"id": row["id"], "role": row["role"], "content": row["content"]} for row in rows]

    async def get_recent_messages(self, source: str = None, limit: int = 50) -> list[dict]:
        if source:
            cursor = await self.db.execute(
                "SELECT role, content FROM conversations WHERE source = ? ORDER BY id DESC LIMIT ?",
                (source, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def get_message_count(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) FROM conversations")
        row = await cursor.fetchone()
        return row[0]

    # --- Memory ---

    async def add_memory(self, category: str, key: str, value: str, source: str = None):
        await self.db.execute(
            """INSERT INTO memory (category, key, value, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(category, key) DO UPDATE SET
                 value = excluded.value,
                 updated_at = CURRENT_TIMESTAMP""",
            (category, key, value, source),
        )
        await self.db.commit()

    async def get_memory(self, category: str = None) -> list[dict]:
        if category:
            cursor = await self.db.execute(
                "SELECT category, key, value FROM memory WHERE category = ? ORDER BY key",
                (category,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT category, key, value FROM memory ORDER BY category, key"
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def search_memory(self, query: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT category, key, value FROM memory WHERE key LIKE ? OR value LIKE ?",
            (f"%{query}%", f"%{query}%"),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_memory(self, category: str, key: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM memory WHERE category = ? AND key = ?",
            (category, key),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    # --- Task Queue ---

    async def enqueue_task(self, task_type: str, payload: dict = None, priority: int = 5) -> int:
        cursor = await self.db.execute(
            "INSERT INTO task_queue (task_type, payload, priority) VALUES (?, ?, ?)",
            (task_type, json.dumps(payload) if payload else None, priority),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_pending_tasks(self, limit: int = 10) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT id, task_type, payload, priority, created_at
               FROM task_queue WHERE status = 'pending'
               ORDER BY priority ASC, created_at ASC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d["payload"]:
                d["payload"] = json.loads(d["payload"])
            result.append(d)
        return result

    async def update_task_status(self, task_id: int, status: str, result: str = None, error: str = None):
        if status == "running":
            await self.db.execute(
                "UPDATE task_queue SET status = ?, started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, task_id),
            )
        elif status in ("completed", "failed"):
            await self.db.execute(
                """UPDATE task_queue SET status = ?, completed_at = CURRENT_TIMESTAMP,
                   result = ?, error = ? WHERE id = ?""",
                (status, result, error, task_id),
            )
        else:
            await self.db.execute(
                "UPDATE task_queue SET status = ? WHERE id = ?",
                (status, task_id),
            )
        await self.db.commit()

    # --- Task Log ---

    async def log_task(self, task_type: str, duration_ms: int, tokens_used: int = 0,
                       cost_cents: float = 0, success: bool = True, summary: str = None):
        await self.db.execute(
            """INSERT INTO task_log (task_type, duration_ms, tokens_used, cost_cents, success, summary)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_type, duration_ms, tokens_used, cost_cents, success, summary),
        )
        await self.db.commit()

    async def get_cost_summary(self, period: str = "today") -> dict:
        if period == "today":
            where = "DATE(created_at) = DATE('now', 'localtime')"
        elif period == "week":
            where = "created_at >= DATE('now', '-7 days')"
        elif period == "month":
            where = "created_at >= DATE('now', '-30 days')"
        else:
            where = "1=1"

        cursor = await self.db.execute(
            f"""SELECT COUNT(*) as calls, COALESCE(SUM(tokens_used), 0) as tokens,
                COALESCE(SUM(cost_cents), 0) as cost_cents
                FROM task_log WHERE {where}"""
        )
        row = await cursor.fetchone()
        return {"calls": row[0], "tokens": row[1], "cost_cents": row[2]}

    # --- Scheduled Tasks ---

    async def upsert_scheduled_task(self, name: str, task_type: str, cron_expression: str,
                                     payload: dict = None, enabled: bool = True):
        await self.db.execute(
            """INSERT INTO scheduled_tasks (name, task_type, cron_expression, payload, enabled)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 task_type = excluded.task_type,
                 cron_expression = excluded.cron_expression,
                 payload = excluded.payload,
                 enabled = excluded.enabled""",
            (name, task_type, cron_expression, json.dumps(payload) if payload else None, enabled),
        )
        await self.db.commit()

    async def get_scheduled_tasks(self, enabled_only: bool = False) -> list[dict]:
        query = "SELECT * FROM scheduled_tasks"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name"
        cursor = await self.db.execute(query)
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    d["payload"] = {"raw": d["payload"]}
            result.append(d)
        return result

    async def set_scheduled_task_enabled(self, name: str, enabled: bool) -> bool:
        cursor = await self.db.execute(
            "UPDATE scheduled_tasks SET enabled = ? WHERE name = ?",
            (enabled, name),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def update_scheduled_task_run(self, name: str):
        await self.db.execute(
            "UPDATE scheduled_tasks SET last_run = CURRENT_TIMESTAMP WHERE name = ?",
            (name,),
        )
        await self.db.commit()

    async def insert_scheduled_task_run(
        self,
        task_name: str,
        started_ts: str,
        finished_ts: Optional[str],
        status: str,
        error_class: Optional[str] = None,
        error_msg: Optional[str] = None,
        is_retry: bool = False,
    ) -> None:
        """Append an append-only row to scheduled_task_runs (RALPLAN-DR v2.1 ADR-003).

        Row is ALWAYS written regardless of notification dedup — observability
        must not be gated on notification policy.
        """
        await self.db.execute(
            """INSERT INTO scheduled_task_runs
               (task_name, started_ts, finished_ts, status, error_class, error_msg, is_retry)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_name, started_ts, finished_ts, status, error_class, error_msg, 1 if is_retry else 0),
        )
        await self.db.commit()

    async def get_latest_scheduled_task_run(self, task_name: str) -> Optional[dict]:
        """Return the most recent scheduled_task_runs row for a task, or None."""
        cursor = await self.db.execute(
            """SELECT id, task_name, started_ts, finished_ts, status,
                      error_class, error_msg, is_retry
               FROM scheduled_task_runs
               WHERE task_name = ?
               ORDER BY started_ts DESC
               LIMIT 1""",
            (task_name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_scheduled_task_status(
        self,
        task_name: str,
        status: str,
        error: Optional[str],
        success_ts: Optional[str],
    ) -> None:
        """Update denormalized latest-outcome columns on scheduled_tasks."""
        if success_ts:
            await self.db.execute(
                """UPDATE scheduled_tasks
                   SET last_run_status = ?, last_error = ?, last_success_ts = ?
                   WHERE name = ?""",
                (status, error, success_ts, task_name),
            )
        else:
            await self.db.execute(
                """UPDATE scheduled_tasks
                   SET last_run_status = ?, last_error = ?
                   WHERE name = ?""",
                (status, error, task_name),
            )
        await self.db.commit()

    async def delete_scheduled_task(self, name: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM scheduled_tasks WHERE name = ?",
            (name,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    # --- Sessions ---

    async def get_active_sessions(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM sessions WHERE status = 'active' ORDER BY last_message_at DESC"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_default_session(self):
        cursor = await self.db.execute(
            "SELECT * FROM sessions WHERE is_default = 1 AND status = 'active'"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_default_session(self, name: str):
        await self.db.execute("UPDATE sessions SET is_default = 0")
        await self.db.execute(
            "UPDATE sessions SET is_default = 1 WHERE name = ?", (name,)
        )
        await self.db.commit()

    async def get_session(self, name: str) -> Optional[dict]:
        # Normalize to lowercase to prevent case-mismatch bugs between
        # TUI (writes lowercase) and AgentBot (config may use mixed case).
        name = name.lower()
        cursor = await self.db.execute(
            "SELECT * FROM sessions WHERE LOWER(name) = ? AND status = 'active'", (name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def upsert_session(self, name: str, session_id: str, project_dir: str, engine: str = None):
        name = name.lower()
        if engine:
            await self.db.execute(
                """INSERT INTO sessions (name, session_id, project_dir, status, last_message_at, engine)
                   VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     session_id = excluded.session_id,
                     status = 'active',
                     last_message_at = CURRENT_TIMESTAMP,
                     engine = excluded.engine""",
                (name, session_id, project_dir, engine),
            )
        else:
            await self.db.execute(
                """INSERT INTO sessions (name, session_id, project_dir, status, last_message_at)
                   VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP)
                   ON CONFLICT(name) DO UPDATE SET
                     session_id = excluded.session_id,
                     status = 'active',
                     last_message_at = CURRENT_TIMESTAMP""",
                (name, session_id, project_dir),
            )
        await self.db.commit()

    async def update_session_activity(self, name: str):
        name = name.lower()
        await self.db.execute(
            "UPDATE sessions SET message_count = message_count + 1, last_message_at = CURRENT_TIMESTAMP WHERE LOWER(name) = ?",
            (name,),
        )
        await self.db.commit()

    async def terminate_session(self, name: str):
        name = name.lower()
        # Get session data before terminating for KB hooks
        session = await self.get_session(name)

        # Update session status
        await self.db.execute(
            "UPDATE sessions SET status = 'terminated', is_default = 0 WHERE LOWER(name) = ?",
            (name,),
        )
        await self.db.commit()

        # Call KB session end hook to compress memory
        if KB_HOOKS_AVAILABLE and session:
            try:
                # Extract agent name from project_dir (same logic as ClaudeCodeEngine._agent_key_for)
                project_dir = session.get("project_dir", "")
                if project_dir:
                    agent_name = Path(project_dir).name.strip().lower()
                else:
                    agent_name = name.strip().lower()

                session_id = session.get("session_id", "")
                result = on_session_end(
                    session_id=session_id,
                    agent_name=agent_name,
                    summary="",
                )
                if result.get("compressed"):
                    logger.info(f"KB session compression completed for {agent_name}")
            except Exception as e:
                logger.warning(f"KB session end hook failed for {name}: {e}")

    # --- Agent Messages ---

    async def enqueue_agent_message(self, sender: str, recipient: str, content: str) -> int:
        cursor = await self.db.execute(
            "INSERT INTO agent_messages (sender, recipient, content) VALUES (?, ?, ?)",
            (sender, recipient, content),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_pending_agent_messages(self, recipient: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT id, sender, content FROM agent_messages WHERE recipient = ? AND delivered = 0 ORDER BY id ASC",
            (recipient,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_agent_messages_delivered(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self.db.execute(
            f"UPDATE agent_messages SET delivered = 1 WHERE id IN ({placeholders})",
            ids,
        )
        await self.db.commit()

    # --- Scheduler Reload ---

    async def request_scheduler_reload(self) -> None:
        """Request scheduler reload from TUI (written by TUI, read by daemon)."""
        await self.db.execute(
            """INSERT INTO scheduler_reload (id, requested_at, processed_at)
               VALUES (1, CURRENT_TIMESTAMP, NULL)
               ON CONFLICT(id) DO UPDATE SET
                 requested_at = CURRENT_TIMESTAMP,
                 processed_at = NULL""",
        )
        await self.db.commit()

    async def check_scheduler_reload_request(self) -> bool:
        """Check if scheduler reload was requested (daemon polls this)."""
        cursor = await self.db.execute(
            "SELECT requested_at, processed_at FROM scheduler_reload WHERE id = 1"
        )
        row = await cursor.fetchone()
        if not row:
            return False
        # Reload requested if requested_at is set and processed_at is NULL
        return row["requested_at"] is not None and row["processed_at"] is None

    async def mark_scheduler_reload_processed(self) -> None:
        """Mark reload as processed (daemon calls this after reloading)."""
        await self.db.execute(
            "UPDATE scheduler_reload SET processed_at = CURRENT_TIMESTAMP WHERE id = 1"
        )
        await self.db.commit()
