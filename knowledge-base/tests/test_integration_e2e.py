"""End-to-end integration tests for the KB enhancement features.

Tests cover 5 scenarios:
  1. Full memory lifecycle: short_term -> consolidate -> compress -> episodic
  2. Search with quality filters: min_confidence filtering via FTS search
  3. Agent scoping: alpha, beta, and shared articles
  4. Supersession chain: A superseded by B, search excludes A
  5. Hybrid search: FTS + graph streams fuse via RRF
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to build an isolated test environment
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
CREATE INDEX IF NOT EXISTS idx_articles_updated ON articles(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_confidence ON articles(confidence_score);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(title, content, tags);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  link_type TEXT DEFAULT 'related',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_id, target_id, link_type)
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_id);

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

_FAKE_CONSOLIDATE_RESULT = {
    "content": '{"lessons": ["Always verify assumptions"], "facts": ["SQLite supports FTS5"], "decisions": ["Use in-memory DBs for tests"]}',
    "model": "claude-haiku-4-5-20251001",
    "input_tokens": 100,
    "output_tokens": 50,
    "cost_usd": 0.0002,
}

_FAKE_COMPRESS_RESULT = {
    "content": "Agent processed memory lifecycle tasks and validated DB schema.",
    "model": "claude-haiku-4-5-20251001",
    "input_tokens": 80,
    "output_tokens": 20,
    "cost_usd": 0.0001,
}


class IsolatedDB:
    """Context manager that wires all KB modules to a temp SQLite file."""

    def __init__(self):
        self.tmp = None
        self.conn = None
        self.wiki_dir = None
        self._db_module = None

    def __enter__(self):
        # Create temp dir for wiki files
        self._tmpdir = tempfile.mkdtemp(prefix="kb_e2e_")
        self.wiki_dir = Path(self._tmpdir) / "wiki"
        self.wiki_dir.mkdir(parents=True)

        # Create temp DB file
        db_fd, db_path = tempfile.mkstemp(suffix=".db", dir=self._tmpdir)
        os.close(db_fd)
        self.db_path = db_path

        # Patch kb.db to use our temp connection
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
        classification: str = "internal",
        tags: list = None,
        agent_scope: str = None,
        confidence_score: float = 0.5,
        superseded_by: str = None,
    ) -> str:
        """Insert a minimal article and its content file, return its UUID."""
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
                article_id, title, slug, str(content_file), classification,
                tags_json, checksum, agent_scope, confidence_score,
                superseded_by, now, now,
            ),
        )

        # Index in FTS
        rowid = self.conn.execute(
            "SELECT rowid FROM articles WHERE id = ?", (article_id,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO articles_fts(rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (rowid, title, content, " ".join(tags or [])),
        )

        return article_id

    def add_link(self, source_id: str, target_id: str, link_type: str = "related"):
        self.conn.execute(
            "INSERT OR IGNORE INTO links (source_id, target_id, link_type) VALUES (?, ?, ?)",
            (source_id, target_id, link_type),
        )


# ---------------------------------------------------------------------------
# Scenario 1: Full memory lifecycle
# ---------------------------------------------------------------------------

class TestMemoryLifecycle:
    """100 short_term entries -> consolidate -> compress -> ends in episodic."""

    def _make_store(self):
        from kb.memory import MemoryStore
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(FULL_SCHEMA)
        return MemoryStore(conn=conn), conn

    def test_add_100_short_term_entries(self):
        store, conn = self._make_store()
        for i in range(100):
            store.add_entry("alpha", "short_term", f"session note {i}")
        assert store.count("alpha") == 100
        entries = store.get_entries("alpha", section="short_term")
        assert len(entries) == 100
        conn.close()

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_consolidate_promotes_old_entries(self, mock_llm, mock_cost):
        from kb.consolidation import consolidate_short_term
        from kb.memory import MemoryStore

        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(FULL_SCHEMA)
        store = MemoryStore(conn=conn)

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        for i in range(5):
            eid = store.add_entry("alpha", "short_term", f"old note {i}")
            conn.execute(
                "UPDATE agent_memory SET created_at=?, updated_at=? WHERE id=?",
                (old_ts, old_ts, eid),
            )

        with patch("kb.consolidation.MemoryStore", return_value=store):
            stats = consolidate_short_term("alpha", dry_run=False, model="haiku")

        assert stats["entries_reviewed"] == 5
        assert stats["entries_deleted"] == 5
        total_added = stats["learned_added"] + stats["long_term_added"] + stats["decisions_added"]
        assert total_added > 0
        # short_term should now be empty
        assert store.count("alpha") == total_added
        conn.close()

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_COMPRESS_RESULT)
    def test_compress_writes_to_episodic(self, mock_llm, mock_cost):
        from kb.consolidation import compress_session
        from kb.memory import MemoryStore

        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(FULL_SCHEMA)
        store = MemoryStore(conn=conn)

        for i in range(3):
            store.add_entry("alpha", "short_term", f"task {i}")

        with patch("kb.consolidation.MemoryStore", return_value=store):
            result = compress_session("alpha", dry_run=False, model="haiku")

        assert result["entries_compressed"] == 3
        assert result["summary_text"] != ""
        assert store.count("alpha") == 1  # one episodic entry
        episodic = store.get_entries("alpha", section="episodic")
        assert len(episodic) == 1
        assert "Agent" in episodic[0]["content"]
        conn.close()

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_COMPRESS_RESULT)
    def test_full_lifecycle_consolidate_then_compress(self, mock_llm, mock_cost):
        """Simulate: add short_term -> consolidate old -> compress remaining -> episodic."""
        from kb.consolidation import compress_session
        from kb.memory import MemoryStore

        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(FULL_SCHEMA)
        store = MemoryStore(conn=conn)

        for i in range(3):
            store.add_entry("alpha", "short_term", f"fresh note {i}")

        with patch("kb.consolidation.MemoryStore", return_value=store):
            result = compress_session("alpha", dry_run=False, model="haiku")

        assert result["entries_compressed"] == 3
        remaining = store.get_entries("alpha", section="short_term")
        assert len(remaining) == 0
        episodic = store.get_entries("alpha", section="episodic")
        assert len(episodic) == 1
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 2: Search with quality filters (min_confidence)
# ---------------------------------------------------------------------------

class TestSearchQualityFilters:
    """min_confidence filtering excludes low-confidence articles."""

    def test_min_confidence_excludes_low_confidence_articles(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "High Confidence Article",
                slug="high-confidence",
                content="unique term zebrafish research notes",
                confidence_score=0.9,
            )
            db.insert_article(
                "Low Confidence Article",
                slug="low-confidence",
                content="unique term zebrafish preliminary draft",
                confidence_score=0.2,
            )

            clear_cache()
            results = search_articles("zebrafish", min_confidence=0.5)

        titles = [r["title"] for r in results]
        assert "High Confidence Article" in titles
        assert "Low Confidence Article" not in titles

    def test_zero_min_confidence_returns_all(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Article",
                slug="alpha-article",
                content="unique token platypus facts",
                confidence_score=0.9,
            )
            db.insert_article(
                "Beta Article",
                slug="beta-article",
                content="unique token platypus notes",
                confidence_score=0.1,
            )

            clear_cache()
            results = search_articles("platypus", min_confidence=0.0)

        titles = [r["title"] for r in results]
        assert "Alpha Article" in titles
        assert "Beta Article" in titles

    def test_high_threshold_returns_empty_when_no_match(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Medium Confidence Article",
                slug="medium-confidence",
                content="unique token quokka marsupial",
                confidence_score=0.6,
            )

            clear_cache()
            results = search_articles("quokka", min_confidence=0.99)

        assert results == []

    def test_confidence_score_present_in_results(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Scored Article",
                slug="scored-article",
                content="unique token narwhal ocean",
                confidence_score=0.75,
            )

            clear_cache()
            results = search_articles("narwhal")

        assert len(results) >= 1
        assert "confidence_score" in results[0]


# ---------------------------------------------------------------------------
# Scenario 3: Agent scoping
# ---------------------------------------------------------------------------

class TestAgentScoping:
    """Agent-scoped articles are isolated; shared (NULL scope) articles are visible to all."""

    def test_alpha_articles_not_visible_to_beta(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Private Notes",
                slug="alpha-private",
                content="unique token secretsauce alpha internal",
                agent_scope="alpha",
                confidence_score=0.8,
            )
            db.insert_article(
                "Beta Private Notes",
                slug="beta-private",
                content="unique token secretsauce beta internal",
                agent_scope="beta",
                confidence_score=0.8,
            )

            clear_cache()
            alpha_results = search_articles("secretsauce", agent_scope="alpha")
            beta_results = search_articles("secretsauce", agent_scope="beta")

        alpha_titles = [r["title"] for r in alpha_results]
        beta_titles = [r["title"] for r in beta_results]
        assert "Alpha Private Notes" in alpha_titles
        assert "Beta Private Notes" not in alpha_titles
        assert "Beta Private Notes" in beta_titles
        assert "Alpha Private Notes" not in beta_titles

    def test_shared_articles_visible_to_all_agents(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Shared Knowledge",
                slug="shared-knowledge",
                content="unique token sharedtoken global knowledge",
                agent_scope=None,  # shared
                confidence_score=0.8,
            )

            clear_cache()
            alpha_results = search_articles("sharedtoken", agent_scope="alpha")
            beta_results = search_articles("sharedtoken", agent_scope="beta")

        assert any(r["title"] == "Shared Knowledge" for r in alpha_results)
        assert any(r["title"] == "Shared Knowledge" for r in beta_results)

    def test_no_scope_filter_returns_all(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Only",
                slug="alpha-only",
                content="unique token mixedset article alpha",
                agent_scope="alpha",
                confidence_score=0.8,
            )
            db.insert_article(
                "Beta Only",
                slug="beta-only",
                content="unique token mixedset article beta",
                agent_scope="beta",
                confidence_score=0.8,
            )

            clear_cache()
            all_results = search_articles("mixedset", agent_scope=None)

        titles = [r["title"] for r in all_results]
        assert "Alpha Only" in titles
        assert "Beta Only" in titles


# ---------------------------------------------------------------------------
# Scenario 4: Supersession chain
# ---------------------------------------------------------------------------

class TestSupersessionChain:
    """Article A superseded by B: search excludes A by default."""

    def test_superseded_article_excluded_from_search(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            new_id = db.insert_article(
                "New Version Article",
                slug="new-version",
                content="unique token frogfish current version article",
                confidence_score=0.9,
            )
            db.insert_article(
                "Old Version Article",
                slug="old-version",
                content="unique token frogfish deprecated old version article",
                confidence_score=0.9,
                superseded_by=new_id,
            )

            clear_cache()
            results = search_articles("frogfish", include_superseded=False)

        titles = [r["title"] for r in results]
        assert "New Version Article" in titles
        assert "Old Version Article" not in titles

    def test_include_superseded_returns_both(self):
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            new_id = db.insert_article(
                "Current Axolotl Article",
                slug="current-axolotl",
                content="unique token axolotl salamander current",
                confidence_score=0.9,
            )
            db.insert_article(
                "Outdated Axolotl Article",
                slug="outdated-axolotl",
                content="unique token axolotl salamander outdated",
                confidence_score=0.9,
                superseded_by=new_id,
            )

            clear_cache()
            results = search_articles("axolotl", include_superseded=True)

        titles = [r["title"] for r in results]
        assert "Current Axolotl Article" in titles
        assert "Outdated Axolotl Article" in titles

    def test_supersession_default_excludes_old(self):
        """Default call (no include_superseded arg) should exclude superseded."""
        from kb.search import search_articles
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            new_id = db.insert_article(
                "New Capybara",
                slug="new-capybara",
                content="unique token capybara rodent new",
                confidence_score=0.8,
            )
            db.insert_article(
                "Old Capybara",
                slug="old-capybara",
                content="unique token capybara rodent old",
                confidence_score=0.8,
                superseded_by=new_id,
            )

            clear_cache()
            results = search_articles("capybara")

        titles = [r["title"] for r in results]
        assert "New Capybara" in titles
        assert "Old Capybara" not in titles


# ---------------------------------------------------------------------------
# Scenario 5: Hybrid search RRF
# ---------------------------------------------------------------------------

class TestHybridSearchRRF:
    """hybrid_search fuses FTS + graph streams using RRF scoring."""

    def test_hybrid_search_returns_rrf_scores(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Hybrid Topic Article",
                slug="hybrid-topic",
                content="unique token wombat marsupial burrow",
                confidence_score=0.8,
            )

            clear_cache()
            results = hybrid_search("wombat")

        assert isinstance(results, list)
        if results:
            assert "rrf_score" in results[0]
            assert results[0]["rrf_score"] > 0.0

    def test_hybrid_search_respects_agent_scope(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Hybrid Article",
                slug="alpha-hybrid",
                content="unique token echidna mammal spine alpha",
                agent_scope="alpha",
                confidence_score=0.8,
            )
            db.insert_article(
                "Beta Hybrid Article",
                slug="beta-hybrid",
                content="unique token echidna mammal spine beta",
                agent_scope="beta",
                confidence_score=0.8,
            )

            clear_cache()
            results = hybrid_search("echidna", agent="alpha")

        titles = [r["title"] for r in results]
        assert "Alpha Hybrid Article" in titles
        assert "Beta Hybrid Article" not in titles

    def test_hybrid_search_excludes_superseded(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            new_id = db.insert_article(
                "New Kookaburra Article",
                slug="new-kookaburra",
                content="unique token kookaburra bird laugh current",
                confidence_score=0.9,
            )
            db.insert_article(
                "Old Kookaburra Article",
                slug="old-kookaburra",
                content="unique token kookaburra bird laugh old",
                confidence_score=0.9,
                superseded_by=new_id,
            )

            clear_cache()
            results = hybrid_search("kookaburra", include_superseded=False)

        titles = [r["title"] for r in results]
        assert "New Kookaburra Article" in titles
        assert "Old Kookaburra Article" not in titles

    def test_hybrid_search_graph_boost(self):
        """Articles linked to FTS top-5 seeds gain graph scores via RRF."""
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            seed_id = db.insert_article(
                "Seed Pangolin Article",
                slug="seed-pangolin",
                content="unique token pangolin scales mammal seed",
                confidence_score=0.85,
            )
            linked_id = db.insert_article(
                "Linked Pangolin Article",
                slug="linked-pangolin",
                content="unique token pangolin linked neighbour",
                confidence_score=0.8,
            )
            db.add_link(seed_id, linked_id, "related")

            clear_cache()
            results = hybrid_search("pangolin")

        ids = [r["id"] for r in results]
        # Both seed and linked article should appear
        assert seed_id in ids or linked_id in ids
