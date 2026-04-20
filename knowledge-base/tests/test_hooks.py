"""Tests for the KB event bus and hook implementations.

Covers:
  - Event bus dispatches to subscribers
  - Event bus catches handler exceptions
  - Ingest hook creates related links
  - Session hooks return expected data structures
  - Hook failures do not crash the emitter
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# ---------------------------------------------------------------------------
# Schema for isolated test databases
# ---------------------------------------------------------------------------

FULL_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  slug TEXT UNIQUE NOT NULL,
  content_path TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  classification TEXT DEFAULT 'internal',
  tags JSON,
  sources JSON,
  checksum TEXT NOT NULL,
  deleted_at TIMESTAMP,
  archived_at TIMESTAMP,
  grace_period_days INTEGER,
  source_file TEXT,
  agent_scope TEXT,
  confidence_score REAL DEFAULT 0.5,
  source_count INTEGER DEFAULT 0,
  last_confirmed_at TIMESTAMP,
  superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_slug ON articles(slug);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(title, content, tags);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  link_type TEXT DEFAULT 'related',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_id, target_id, link_type)
);

CREATE TABLE IF NOT EXISTS article_relationships (
  id TEXT PRIMARY KEY,
  from_article_id TEXT NOT NULL,
  to_article_id TEXT NOT NULL,
  relationship_type TEXT NOT NULL,
  confidence REAL DEFAULT 0.5,
  notes TEXT,
  metadata TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (from_article_id, to_article_id, relationship_type)
);

CREATE TABLE IF NOT EXISTS article_embeddings (
  article_id TEXT PRIMARY KEY,
  embedding BLOB,
  embedding_dim INTEGER,
  model_name TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL,
  section TEXT NOT NULL,
  content TEXT NOT NULL,
  tags TEXT DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hook_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  entity_id TEXT,
  entity_type TEXT,
  status TEXT,
  data TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  operation TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL
);
"""


class IsolatedDB:
    """Context manager: isolated SQLite + wiki dir, patches kb.db singleton."""

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp(prefix="kb_hooks_")
        self.wiki_dir = Path(self._tmpdir) / "wiki"
        self.wiki_dir.mkdir(parents=True)

        db_fd, db_path = tempfile.mkstemp(suffix=".db", dir=self._tmpdir)
        os.close(db_fd)

        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(FULL_SCHEMA)
        self.conn = conn

        import kb.db as db_mod
        self._db_module = db_mod
        self._orig_conn = db_mod._conn
        db_mod._conn = conn
        return self

    def __exit__(self, *_):
        import kb.db as db_mod
        db_mod._conn = self._orig_conn
        if self.conn:
            self.conn.close()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def insert_article(
        self,
        title: str,
        slug: str = None,
        content: str = "Test content.",
        tags: list = None,
        agent_scope: str = None,
        confidence_score: float = 0.5,
        superseded_by: str = None,
    ) -> str:
        article_id = str(uuid.uuid4())
        slug = slug or title.lower().replace(" ", "-")
        content_file = self.wiki_dir / f"{slug}.md"
        content_file.write_text(content)
        checksum = hashlib.sha256(content.encode()).hexdigest()
        tags_json = json.dumps(tags or [])
        now = datetime.utcnow().isoformat()

        self.conn.execute(
            """
            INSERT INTO articles
              (id, title, slug, content_path, classification, tags, checksum,
               agent_scope, confidence_score, superseded_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id, title, slug, str(content_file), "internal",
                tags_json, checksum, agent_scope, confidence_score,
                superseded_by, now, now,
            ),
        )
        rowid = self.conn.execute(
            "SELECT rowid FROM articles WHERE id = ?", (article_id,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO articles_fts(rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (rowid, title, content, " ".join(tags or [])),
        )
        return article_id


# ---------------------------------------------------------------------------
# Event bus unit tests
# ---------------------------------------------------------------------------

class TestEventBusDispatch:
    """KBEventBus dispatches events to all registered subscribers."""

    def _make_bus(self):
        """Return a fresh KBEventBus with no default handlers."""
        from kb.hooks.event_bus import KBEventBus
        return KBEventBus()

    def test_subscribe_and_emit_calls_handler(self):
        bus = self._make_bus()
        received = []

        def handler(**kwargs):
            received.append(kwargs)

        bus.subscribe("ingest", handler)
        bus.emit("ingest", article_id="abc", content="hello")

        assert len(received) == 1
        assert received[0]["article_id"] == "abc"
        assert received[0]["content"] == "hello"

    def test_multiple_handlers_all_called(self):
        bus = self._make_bus()
        call_log = []

        def handler_a(**kwargs):
            call_log.append("a")

        def handler_b(**kwargs):
            call_log.append("b")

        bus.subscribe("ingest", handler_a)
        bus.subscribe("ingest", handler_b)
        bus.emit("ingest", article_id="x")

        assert "a" in call_log
        assert "b" in call_log
        assert len(call_log) == 2

    def test_emit_returns_result_list(self):
        bus = self._make_bus()

        def handler(**kwargs):
            pass

        bus.subscribe("ingest", handler)
        results = bus.emit("ingest", article_id="x")

        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["handler"] == "handler"
        assert results[0]["status"] == "success"
        assert results[0]["error"] is None

    def test_emit_with_no_handlers_returns_empty_list(self):
        bus = self._make_bus()
        results = bus.emit("query", query="test")

        assert results == []

    def test_emit_different_event_does_not_trigger_other_handlers(self):
        bus = self._make_bus()
        called = []

        def ingest_handler(**kwargs):
            called.append("ingest")

        bus.subscribe("ingest", ingest_handler)
        bus.emit("session_start", session_id="s1")

        assert called == []

    def test_same_handler_subscribes_multiple_events(self):
        bus = self._make_bus()
        fired = []

        def universal(**kwargs):
            fired.append(kwargs.get("event"))

        bus.subscribe("ingest", lambda **kw: fired.append("ingest"))
        bus.subscribe("session_start", lambda **kw: fired.append("session_start"))

        bus.emit("ingest", event="ingest", article_id="a")
        bus.emit("session_start", event="session_start", session_id="s")

        assert "ingest" in fired
        assert "session_start" in fired


class TestEventBusExceptionHandling:
    """Handler exceptions must never crash the emitter."""

    def _make_bus(self):
        from kb.hooks.event_bus import KBEventBus
        return KBEventBus()

    def test_handler_exception_does_not_crash_emitter(self):
        bus = self._make_bus()

        def bad_handler(**kwargs):
            raise RuntimeError("handler exploded")

        bus.subscribe("ingest", bad_handler)
        # Should not raise
        results = bus.emit("ingest", article_id="x")

        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "exploded" in results[0]["error"]

    def test_failing_handler_does_not_prevent_subsequent_handlers(self):
        bus = self._make_bus()
        second_called = []

        def bad_handler(**kwargs):
            raise ValueError("first handler fails")

        def good_handler(**kwargs):
            second_called.append(True)

        bus.subscribe("ingest", bad_handler)
        bus.subscribe("ingest", good_handler)
        results = bus.emit("ingest", article_id="x")

        assert second_called == [True]
        assert len(results) == 2
        assert results[0]["status"] == "error"
        assert results[1]["status"] == "success"

    def test_multiple_failing_handlers_all_reported(self):
        bus = self._make_bus()

        def fail_a(**kwargs):
            raise TypeError("type error")

        def fail_b(**kwargs):
            raise KeyError("key error")

        bus.subscribe("write", fail_a)
        bus.subscribe("write", fail_b)
        results = bus.emit("write", article_id="y")

        assert len(results) == 2
        assert all(r["status"] == "error" for r in results)
        assert "type error" in results[0]["error"]
        assert "key error" in results[1]["error"]

    def test_error_result_contains_handler_name(self):
        bus = self._make_bus()

        def named_bad_handler(**kwargs):
            raise Exception("oops")

        bus.subscribe("query", named_bad_handler)
        results = bus.emit("query")

        assert results[0]["handler"] == "named_bad_handler"
        assert results[0]["status"] == "error"

    def test_emit_logs_to_hook_events_table(self):
        """emit() should log to hook_events table (best-effort)."""
        from kb.hooks.event_bus import KBEventBus

        with IsolatedDB() as db:
            bus = KBEventBus()

            def handler(**kwargs):
                pass

            bus.subscribe("ingest", handler)
            bus.emit("ingest", article_id="test-article-id")

            row = db.conn.execute(
                "SELECT * FROM hook_events WHERE event_type = 'ingest' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row is not None
        assert row["event_type"] == "ingest"

    def test_emit_no_handlers_still_logs_event(self):
        """emit() with no handlers should log a 'no_handlers' event."""
        from kb.hooks.event_bus import KBEventBus

        with IsolatedDB() as db:
            bus = KBEventBus()
            bus.emit("session_end", session_id="s123")

            row = db.conn.execute(
                "SELECT * FROM hook_events WHERE event_type = 'session_end' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row is not None
        assert row["status"] == "no_handlers"


# ---------------------------------------------------------------------------
# Ingest hook tests
# ---------------------------------------------------------------------------

class TestIngestHook:
    """on_article_ingested creates related links in article_relationships."""

    def test_ingest_hook_does_not_raise_on_missing_article(self):
        """Hook must be silent when article_id doesn't exist."""
        from kb.hooks.ingest_hook import on_article_ingested

        with IsolatedDB():
            # Should not raise even for a nonexistent article
            on_article_ingested(
                article_id="nonexistent-id",
                content="some content",
                classification="internal",
            )

    def test_ingest_hook_does_not_raise_when_no_candidates(self):
        """Hook exits cleanly when no similar articles are found."""
        from kb.hooks.ingest_hook import on_article_ingested

        with IsolatedDB() as db:
            article_id = db.insert_article(
                "Lone Fossa Article",
                slug="lone-fossa",
                content="unique token fossa madagascar cat",
            )
            on_article_ingested(
                article_id=article_id,
                content="unique token fossa madagascar cat",
                classification="internal",
            )
            # No crash is the pass condition

    def test_ingest_hook_creates_related_links_for_similar_articles(self):
        """When similar articles exist and pass the confidence threshold, links are created."""
        from kb.hooks.ingest_hook import on_article_ingested
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            # Insert a base article
            existing_id = db.insert_article(
                "Existing Aye-Aye Article",
                slug="existing-aye-aye",
                content="aye-aye lemur primate nocturnal madagascar",
                confidence_score=0.9,
            )
            # Insert FTS entry for existing article (already done by insert_article)

            # Insert the new article to be hooked
            new_id = db.insert_article(
                "New Aye-Aye Research",
                slug="new-aye-aye-research",
                content="aye-aye lemur primate nocturnal madagascar new findings",
                confidence_score=0.9,
            )

            clear_cache()

            # Patch hybrid_search to return the existing article with high confidence
            mock_candidate = {
                "id": existing_id,
                "title": "Existing Aye-Aye Article",
                "confidence": 0.85,
                "rrf_score": 0.015,
            }
            with patch("kb.hooks.ingest_hook._find_candidates", return_value=[mock_candidate]):
                on_article_ingested(
                    article_id=new_id,
                    content="aye-aye lemur primate",
                    classification="internal",
                )

            row = db.conn.execute(
                "SELECT * FROM article_relationships WHERE from_article_id = ? AND to_article_id = ?",
                (new_id, existing_id),
            ).fetchone()

        assert row is not None
        assert row["relationship_type"] == "related"

    def test_ingest_hook_skips_low_confidence_candidates(self):
        """Candidates below 0.7 confidence should not create links."""
        from kb.hooks.ingest_hook import on_article_ingested
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            existing_id = db.insert_article(
                "Low-Conf Tenrec Article",
                slug="low-conf-tenrec",
                content="tenrec hedgehog insectivore madagascar",
                confidence_score=0.5,
            )
            new_id = db.insert_article(
                "New Tenrec Article",
                slug="new-tenrec",
                content="tenrec hedgehog insectivore new study",
                confidence_score=0.8,
            )

            clear_cache()

            mock_candidate = {
                "id": existing_id,
                "title": "Low-Conf Tenrec Article",
                "confidence": 0.4,  # below 0.7 threshold
            }
            with patch("kb.hooks.ingest_hook._find_candidates", return_value=[mock_candidate]):
                on_article_ingested(
                    article_id=new_id,
                    content="tenrec hedgehog",
                    classification="internal",
                )

            row = db.conn.execute(
                "SELECT * FROM article_relationships WHERE from_article_id = ?",
                (new_id,),
            ).fetchone()

        assert row is None

    def test_ingest_hook_logs_hook_event(self):
        """on_article_ingested should log an ingest.related_links hook_event.

        The log is written only when candidates are found (even if none pass the
        0.7 confidence threshold). We supply a below-threshold candidate so the
        function reaches the logging block without creating any actual links.
        """
        from kb.hooks.ingest_hook import on_article_ingested
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            article_id = db.insert_article(
                "Fanaloka Article",
                slug="fanaloka-article",
                content="unique token fanaloka civet madagascar carnivore",
            )
            other_id = db.insert_article(
                "Other Fanaloka Article",
                slug="other-fanaloka-article",
                content="unique token fanaloka civet other article",
            )

            clear_cache()
            # Provide a candidate that exists but has confidence below 0.7 so no
            # link is created; the hook still reaches the hook_events log call.
            mock_candidate = {"id": other_id, "confidence": 0.5}
            with patch("kb.hooks.ingest_hook._find_candidates", return_value=[mock_candidate]):
                on_article_ingested(
                    article_id=article_id,
                    content="fanaloka civet",
                    classification="internal",
                )

            row = db.conn.execute(
                "SELECT * FROM hook_events WHERE event_type = 'ingest.related_links' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row is not None
        assert row["entity_id"] == article_id


# ---------------------------------------------------------------------------
# Session hook tests
# ---------------------------------------------------------------------------

class TestSessionHooks:
    """on_session_start and on_session_end return expected data structures."""

    def test_on_session_start_returns_dict(self):
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB():
            result = on_session_start(
                session_id="s1",
                agent_name="alpha",
                project_dir="/tmp",
            )

        assert isinstance(result, dict)
        assert "markdown" in result
        assert "article_count" in result

    def test_on_session_start_article_count_is_int(self):
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB():
            result = on_session_start(session_id="s2", agent_name="alpha")

        assert isinstance(result["article_count"], int)
        assert result["article_count"] >= 0

    def test_on_session_start_includes_agent_articles(self):
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Session Article",
                slug="alpha-session-article",
                content="alpha session specific article content",
                agent_scope="alpha",
            )

            result = on_session_start(session_id="s3", agent_name="alpha")

        assert result["article_count"] >= 1
        assert isinstance(result["markdown"], str)

    def test_on_session_start_includes_shared_articles(self):
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB() as db:
            db.insert_article(
                "Global Shared Article",
                slug="global-shared-article",
                content="shared global article for all agents",
                agent_scope=None,
            )

            result = on_session_start(session_id="s4", agent_name="beta")

        assert result["article_count"] >= 1

    def test_on_session_start_excludes_other_agent_articles(self):
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Only Article",
                slug="alpha-only-article",
                content="exclusive to alpha agent scope",
                agent_scope="alpha",
            )

            result = on_session_start(session_id="s5", agent_name="beta")

        # Beta should not see Alpha's scoped article
        assert "Alpha Only Article" not in result["markdown"]

    def test_on_session_start_includes_memory_when_available(self):
        from kb.hooks.session_hooks import on_session_start
        from kb.memory import MemoryStore

        with IsolatedDB() as db:
            store = MemoryStore(conn=db.conn)
            store.add_entry("alpha", "short_term", "test memory entry for session")

            result = on_session_start(session_id="s6", agent_name="alpha")

        assert isinstance(result["markdown"], str)

    def test_on_session_start_handles_missing_agent(self):
        """Empty agent_name should not crash."""
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB():
            result = on_session_start(session_id="s7", agent_name="")

        assert isinstance(result, dict)
        assert "markdown" in result
        assert "article_count" in result

    def test_on_session_end_returns_dict(self):
        from kb.hooks.session_hooks import on_session_end

        with IsolatedDB():
            # compress_session is imported lazily inside _compress_session, so
            # patch at the consolidation module level where it is defined.
            with patch("kb.consolidation.compress_session") as mock_compress:
                mock_compress.return_value = {
                    "entries_compressed": 0,
                    "summary_text": "",
                    "target_section": "episodic",
                }
                result = on_session_end(
                    session_id="se1",
                    agent_name="alpha",
                    summary="test summary",
                )

        assert isinstance(result, dict)
        assert "compressed" in result
        assert "summary" in result

    def test_on_session_end_compressed_is_bool(self):
        from kb.hooks.session_hooks import on_session_end

        with IsolatedDB():
            # Patch _compress_session directly since compress_session is a lazy
            # import inside that inner function.
            with patch("kb.hooks.session_hooks._compress_session") as mock_inner:
                mock_inner.return_value = {"compressed": True, "summary": "Session wrapped up."}
                result = on_session_end(
                    session_id="se2",
                    agent_name="alpha",
                )

        assert isinstance(result["compressed"], bool)

    def test_on_session_end_no_agent_returns_not_compressed(self):
        """No agent_name means nothing to compress."""
        from kb.hooks.session_hooks import on_session_end

        with IsolatedDB():
            result = on_session_end(session_id="se3", agent_name="")

        assert result["compressed"] is False

    def test_on_session_end_error_returns_safe_dict(self):
        """If compress_session raises, on_session_end returns compressed=False."""
        from kb.hooks.session_hooks import on_session_end

        with IsolatedDB():
            with patch(
                "kb.hooks.session_hooks._compress_session",
                side_effect=RuntimeError("compress failed"),
            ):
                result = on_session_end(session_id="se4", agent_name="beta")

        assert result == {"compressed": False, "summary": ""}

    def test_on_session_start_error_returns_safe_dict(self):
        """If _load_session_context raises, on_session_start returns safe fallback."""
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB():
            with patch(
                "kb.hooks.session_hooks._load_session_context",
                side_effect=RuntimeError("load failed"),
            ):
                result = on_session_start(session_id="s8", agent_name="alpha")

        assert result == {"markdown": "", "article_count": 0}


# ---------------------------------------------------------------------------
# Hook failure isolation (emitter must never crash)
# ---------------------------------------------------------------------------

class TestHookFailureIsolation:
    """Hook failures in any component must not crash the event emitter."""

    def test_bus_emits_despite_db_hook_failure(self):
        """Even if hook_events INSERT fails, emit() should not crash."""
        from kb.hooks.event_bus import KBEventBus

        bus = KBEventBus()
        called = []

        def good_handler(**kwargs):
            called.append("called")

        bus.subscribe("ingest", good_handler)

        # Patch the connection to simulate DB failure on INSERT
        with patch("kb.hooks.event_bus.get_connection") as mock_get_conn:
            mock_conn = MagicMock()
            mock_conn.execute.side_effect = [None, Exception("DB down")]
            mock_get_conn.return_value = mock_conn
            results = bus.emit("ingest", article_id="x")

        # good_handler still ran (before DB logging attempt)
        assert "called" in called

    def test_get_bus_singleton_returns_same_instance(self):
        """get_bus() always returns the same KBEventBus instance."""
        from kb.hooks.event_bus import get_bus, _bus
        import kb.hooks.event_bus as bus_mod

        # Reset singleton for test isolation
        orig_bus = bus_mod._bus
        bus_mod._bus = None

        try:
            bus_a = get_bus()
            bus_b = get_bus()
            assert bus_a is bus_b
        finally:
            bus_mod._bus = orig_bus

    def test_ingest_hook_exception_does_not_propagate(self):
        """on_article_ingested wraps all errors; should never raise to caller."""
        from kb.hooks.ingest_hook import on_article_ingested

        with IsolatedDB():
            with patch(
                "kb.hooks.ingest_hook._create_related_links",
                side_effect=RuntimeError("inner failure"),
            ):
                # Must not raise
                on_article_ingested(
                    article_id="some-id",
                    content="content",
                    classification="internal",
                )

    def test_session_start_hook_exception_does_not_propagate(self):
        """on_session_start must never raise; returns safe fallback on error."""
        from kb.hooks.session_hooks import on_session_start

        with IsolatedDB():
            with patch(
                "kb.hooks.session_hooks._load_session_context",
                side_effect=Exception("catastrophic"),
            ):
                result = on_session_start(session_id="s99", agent_name="alpha")

        assert isinstance(result, dict)
        assert result.get("article_count") == 0

    def test_session_end_hook_exception_does_not_propagate(self):
        """on_session_end must never raise; returns safe fallback on error."""
        from kb.hooks.session_hooks import on_session_end

        with IsolatedDB():
            with patch(
                "kb.hooks.session_hooks._compress_session",
                side_effect=Exception("compress catastrophe"),
            ):
                result = on_session_end(session_id="s98", agent_name="beta")

        assert isinstance(result, dict)
        assert result.get("compressed") is False
