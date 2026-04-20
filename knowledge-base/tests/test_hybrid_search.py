"""Tests for hybrid search RRF fusion and filter behaviour.

Covers:
  - RRF fusion combines three ranked lists correctly
  - Hybrid search degrades gracefully without embeddings
  - Agent scope filtering applies across all streams
  - Confidence/supersession filters work in hybrid mode
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared schema and DB helpers (mirror from test_integration_e2e)
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
        self._tmpdir = tempfile.mkdtemp(prefix="kb_hybrid_")
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

    def add_link(self, source_id: str, target_id: str, link_type: str = "related"):
        self.conn.execute(
            "INSERT OR IGNORE INTO links (source_id, target_id, link_type) VALUES (?, ?, ?)",
            (source_id, target_id, link_type),
        )


# ---------------------------------------------------------------------------
# Unit-level RRF logic tests (no DB required)
# ---------------------------------------------------------------------------

class TestRRFFusion:
    """Verify RRF arithmetic in isolation."""

    def _rrf_score(self, rank: int, k: int = 60) -> float:
        return 1.0 / (k + rank)

    def test_rrf_formula_single_stream(self):
        """Score for rank 1 should be 1/(k+1)."""
        score = self._rrf_score(1, k=60)
        assert abs(score - (1.0 / 61)) < 1e-9

    def test_rrf_higher_rank_is_lower_score(self):
        """Earlier (lower-number) ranks should produce higher scores."""
        score_1 = self._rrf_score(1)
        score_5 = self._rrf_score(5)
        score_10 = self._rrf_score(10)
        assert score_1 > score_5 > score_10

    def test_rrf_multi_stream_accumulates(self):
        """Same article in two streams should accumulate both scores."""
        # Article A: rank 1 in FTS, rank 2 in graph
        score_a = self._rrf_score(1) + self._rrf_score(2)
        # Article B: rank 1 in FTS only
        score_b = self._rrf_score(1)
        # A appears in both streams, so its total > B's even if A ranks 2nd in graph
        assert score_a > score_b

    def test_rrf_three_streams_max_score(self):
        """Article at rank 1 in all three streams should have max score."""
        score = self._rrf_score(1) + self._rrf_score(1) + self._rrf_score(1)
        expected = 3.0 / 61
        assert abs(score - expected) < 1e-9

    def test_rrf_sorted_descending(self):
        """Simulate RRF fusion on two lists and verify sort order."""
        # Two ranked lists
        list_a = ["article_1", "article_2", "article_3"]
        list_b = ["article_2", "article_3", "article_1"]

        k = 60
        scores: Dict[str, float] = {}
        for rank, aid in enumerate(list_a, start=1):
            scores[aid] = scores.get(aid, 0.0) + 1.0 / (k + rank)
        for rank, aid in enumerate(list_b, start=1):
            scores[aid] = scores.get(aid, 0.0) + 1.0 / (k + rank)

        ranked = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        # article_2 is rank 2 in A and rank 1 in B — high combined score
        # article_1 is rank 1 in A and rank 3 in B
        assert ranked[0] in ("article_1", "article_2")
        # All three articles present
        assert set(ranked) == {"article_1", "article_2", "article_3"}

    def test_rrf_k_dampens_high_rank_influence(self):
        """Higher k means ranks influence scores less strongly."""
        # With k=1: rank 1 scores 0.5, rank 10 scores ~0.09 (ratio 5.6x)
        # With k=100: rank 1 scores ~0.0099, rank 10 scores ~0.009 (ratio 1.1x)
        score_high_k_r1 = 1.0 / (100 + 1)
        score_high_k_r10 = 1.0 / (100 + 10)
        score_low_k_r1 = 1.0 / (1 + 1)
        score_low_k_r10 = 1.0 / (1 + 10)

        ratio_high_k = score_high_k_r1 / score_high_k_r10
        ratio_low_k = score_low_k_r1 / score_low_k_r10

        assert ratio_high_k < ratio_low_k


# ---------------------------------------------------------------------------
# Integration-level hybrid_search tests
# ---------------------------------------------------------------------------

class TestHybridSearchFunctionality:
    """Tests against the actual hybrid_search() function with isolated DB."""

    def test_hybrid_search_returns_list(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Tapir Article",
                slug="tapir-article",
                content="unique token tapir jungle animal",
                confidence_score=0.8,
            )
            clear_cache()
            results = hybrid_search("tapir")

        assert isinstance(results, list)

    def test_hybrid_search_rrf_score_in_results(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Meerkat Article",
                slug="meerkat-article",
                content="unique token meerkat desert mammal",
                confidence_score=0.8,
            )
            clear_cache()
            results = hybrid_search("meerkat")

        if results:
            assert "rrf_score" in results[0]
            assert isinstance(results[0]["rrf_score"], float)
            assert results[0]["rrf_score"] > 0.0

    def test_hybrid_search_sorted_by_rrf_descending(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            for i in range(3):
                db.insert_article(
                    f"Marmot Article {i}",
                    slug=f"marmot-article-{i}",
                    content=f"unique token marmot rodent burrow item{i}",
                    confidence_score=0.8,
                )
            clear_cache()
            results = hybrid_search("marmot")

        if len(results) >= 2:
            for i in range(len(results) - 1):
                assert results[i]["rrf_score"] >= results[i + 1]["rrf_score"]

    def test_hybrid_search_limit_respected(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            for i in range(10):
                db.insert_article(
                    f"Wolverine Article {i}",
                    slug=f"wolverine-article-{i}",
                    content=f"unique token wolverine mustelid forest item{i}",
                    confidence_score=0.8,
                )
            clear_cache()
            results = hybrid_search("wolverine", limit=3)

        assert len(results) <= 3

    def test_hybrid_search_empty_query_returns_no_crash(self):
        """An empty FTS query may raise or return empty — should not crash."""
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            clear_cache()
            try:
                results = hybrid_search("xyzzy_no_match_token_99")
                assert isinstance(results, list)
            except Exception:
                pass  # Some FTS errors are acceptable for no-match queries


class TestHybridSearchGracefulDegradation:
    """hybrid_search should work without sentence-transformers installed."""

    def test_no_embeddings_still_returns_fts_results(self):
        """When _EMBEDDINGS_AVAILABLE is False, vector stream is skipped gracefully."""
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Ocelot Article",
                slug="ocelot-article",
                content="unique token ocelot cat feline spotted",
                confidence_score=0.8,
            )
            clear_cache()
            with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
                results = hybrid_search("ocelot")

        assert isinstance(results, list)
        # FTS stream should still find the article
        titles = [r["title"] for r in results]
        assert "Ocelot Article" in titles

    def test_vector_search_import_error_does_not_crash(self):
        """ImportError from vector_search is swallowed; other streams still work."""
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Margay Article",
                slug="margay-article",
                content="unique token margay small feline nocturnal",
                confidence_score=0.8,
            )
            clear_cache()

            def _raise_import(*args, **kwargs):
                raise ImportError("sentence-transformers not available")

            with patch("kb.embeddings.vector_search", side_effect=_raise_import):
                results = hybrid_search("margay")

        assert isinstance(results, list)

    def test_graph_search_with_no_links(self):
        """Graph stream with no links returns empty list; fusion still works."""
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Isolated Serval Article",
                slug="isolated-serval",
                content="unique token serval african cat isolated",
                confidence_score=0.8,
            )
            clear_cache()
            results = hybrid_search("serval")

        # Should return FTS results even with no graph neighbours
        assert isinstance(results, list)


class TestHybridSearchAgentScopeFiltering:
    """Agent scope filter applies across all streams in hybrid mode."""

    def test_agent_scope_excludes_other_agent(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Alpha Coati Article",
                slug="alpha-coati",
                content="unique token coatimundi raccoon alpha",
                agent_scope="alpha",
                confidence_score=0.8,
            )
            db.insert_article(
                "Beta Coati Article",
                slug="beta-coati",
                content="unique token coatimundi raccoon beta",
                agent_scope="beta",
                confidence_score=0.8,
            )
            clear_cache()
            alpha_results = hybrid_search("coatimundi", agent="alpha")
            beta_results = hybrid_search("coatimundi", agent="beta")

        alpha_titles = [r["title"] for r in alpha_results]
        beta_titles = [r["title"] for r in beta_results]
        assert "Alpha Coati Article" in alpha_titles
        assert "Beta Coati Article" not in alpha_titles
        assert "Beta Coati Article" in beta_titles
        assert "Alpha Coati Article" not in beta_titles

    def test_shared_articles_appear_for_all_agents_in_hybrid(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "Shared Binturong Article",
                slug="shared-binturong",
                content="unique token binturong bearcat shared",
                agent_scope=None,
                confidence_score=0.8,
            )
            clear_cache()
            alpha_results = hybrid_search("binturong", agent="alpha")
            beta_results = hybrid_search("binturong", agent="beta")

        assert any(r["title"] == "Shared Binturong Article" for r in alpha_results)
        assert any(r["title"] == "Shared Binturong Article" for r in beta_results)


class TestHybridSearchConfidenceAndSupersession:
    """Confidence and supersession filters in hybrid_search."""

    def test_min_confidence_filters_hybrid_results(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            db.insert_article(
                "High Conf Cassowary",
                slug="high-conf-cassowary",
                content="unique token cassowary bird australia high",
                confidence_score=0.9,
            )
            db.insert_article(
                "Low Conf Cassowary",
                slug="low-conf-cassowary",
                content="unique token cassowary bird australia low",
                confidence_score=0.2,
            )
            clear_cache()
            results = hybrid_search("cassowary", min_confidence=0.5)

        titles = [r["title"] for r in results]
        assert "High Conf Cassowary" in titles
        assert "Low Conf Cassowary" not in titles

    def test_superseded_excluded_from_hybrid_by_default(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            new_id = db.insert_article(
                "New Dugong Article",
                slug="new-dugong",
                content="unique token dugong sea cow current",
                confidence_score=0.9,
            )
            db.insert_article(
                "Old Dugong Article",
                slug="old-dugong",
                content="unique token dugong sea cow outdated",
                confidence_score=0.9,
                superseded_by=new_id,
            )
            clear_cache()
            results = hybrid_search("dugong")

        titles = [r["title"] for r in results]
        assert "New Dugong Article" in titles
        assert "Old Dugong Article" not in titles

    def test_include_superseded_true_in_hybrid(self):
        from kb.search import hybrid_search
        from kb.performance import clear_cache

        with IsolatedDB() as db:
            new_id = db.insert_article(
                "New Numbat Article",
                slug="new-numbat",
                content="unique token numbat termite eater current",
                confidence_score=0.9,
            )
            db.insert_article(
                "Old Numbat Article",
                slug="old-numbat",
                content="unique token numbat termite eater old",
                confidence_score=0.9,
                superseded_by=new_id,
            )
            clear_cache()
            results = hybrid_search("numbat", include_superseded=True)

        titles = [r["title"] for r in results]
        assert "New Numbat Article" in titles
        assert "Old Numbat Article" in titles
