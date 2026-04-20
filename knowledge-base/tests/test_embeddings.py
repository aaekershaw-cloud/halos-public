"""Tests for kb/embeddings.py - semantic embedding support"""

import os
import re
import struct
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# In-memory DB factory
# ---------------------------------------------------------------------------

_ARTICLES_DDL = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    content_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    classification TEXT DEFAULT 'internal',
    tags JSON DEFAULT '[]',
    checksum TEXT NOT NULL,
    agent_scope TEXT
)
"""

_EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS article_embeddings (
    article_id    TEXT PRIMARY KEY,
    embedding     BLOB,
    embedding_dim INTEGER,
    model_name    TEXT,
    created_at    TEXT
)
"""


def _make_conn() -> sqlite3.Connection:
    """Create a fresh in-memory SQLite connection with required tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(_ARTICLES_DDL)
    conn.execute(_EMBEDDINGS_DDL)
    # Minimal FTS stub so update_article_fts doesn't fail
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts
        USING fts5(title, content, content='articles', content_rowid='rowid')
    """)
    conn.commit()
    return conn


def _make_float32_blob(dim: int = 384, value: float = 0.5) -> bytes:
    return struct.pack(f"{dim}f", *([value] * dim))


def _insert_article(conn: sqlite3.Connection, article_id: str, title: str,
                    content_path: str = "wiki/internal/dummy.md") -> None:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") + "-" + article_id[:6]
    conn.execute(
        "INSERT OR IGNORE INTO articles (id, title, slug, content_path, classification, tags, checksum) "
        "VALUES (?, ?, ?, ?, 'internal', '[]', 'abc')",
        (article_id, title, slug, content_path),
    )
    conn.commit()


def _reset_model():
    """Clear the cached model singleton between tests."""
    import kb.embeddings as emb
    emb._model = None


# ---------------------------------------------------------------------------
# embed_text
# ---------------------------------------------------------------------------

class TestEmbedText(unittest.TestCase):

    def setUp(self):
        _reset_model()

    def test_returns_correct_byte_length(self):
        """384 float32s == 1536 bytes."""
        import numpy as np
        mock_model = MagicMock()
        mock_model.encode.return_value = np.ones((1, 384), dtype=np.float32)

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings.np", np):
            from kb.embeddings import embed_text
            blob = embed_text("hello world")

        assert isinstance(blob, bytes)
        assert len(blob) == 384 * 4

    def test_returns_float32_values(self):
        """Unpacked BLOB must yield 384 float32 values."""
        import numpy as np
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.25] * 384], dtype=np.float32)

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings.np", np):
            from kb.embeddings import embed_text
            blob = embed_text("test")

        values = struct.unpack("384f", blob)
        assert len(values) == 384
        assert abs(values[0] - 0.25) < 1e-5

    def test_raises_import_error_when_unavailable(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import embed_text
            with self.assertRaises(ImportError):
                embed_text("fail")


# ---------------------------------------------------------------------------
# vector_search
# ---------------------------------------------------------------------------

class TestVectorSearch(unittest.TestCase):

    def setUp(self):
        self.conn = _make_conn()
        _reset_model()

    def test_returns_empty_when_no_embeddings(self):
        """Empty article_embeddings table -> []."""
        import numpy as np
        mock_model = MagicMock()
        mock_model.encode.return_value = np.ones((1, 384), dtype=np.float32)

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings.np", np), \
             patch("kb.db.get_connection", return_value=self.conn):
            from kb.embeddings import vector_search
            results = vector_search("anything")

        assert results == []

    def test_returns_ranked_results(self):
        """Results sorted by descending cosine similarity."""
        import numpy as np

        aid_a = "aaa001"
        aid_b = "bbb002"
        _insert_article(self.conn, aid_a, "Article A", "wiki/internal/a.md")
        _insert_article(self.conn, aid_b, "Article B", "wiki/internal/b.md")

        # A: all +1s (very similar to all-1s query), B: all -1s (dissimilar)
        self.conn.execute(
            "INSERT INTO article_embeddings (article_id, embedding, embedding_dim, model_name, created_at) "
            "VALUES (?, ?, 384, 'test', '2026-01-01')",
            (aid_a, _make_float32_blob(384, 1.0)),
        )
        self.conn.execute(
            "INSERT INTO article_embeddings (article_id, embedding, embedding_dim, model_name, created_at) "
            "VALUES (?, ?, 384, 'test', '2026-01-01')",
            (aid_b, _make_float32_blob(384, -1.0)),
        )
        self.conn.commit()

        mock_model = MagicMock()
        mock_model.encode.return_value = np.ones((1, 384), dtype=np.float32)

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings.np", np), \
             patch("kb.db.get_connection", return_value=self.conn):
            from kb.embeddings import vector_search
            results = vector_search("query", limit=10)

        assert len(results) == 2
        assert results[0]["id"] == aid_a
        assert results[0]["similarity"] > results[1]["similarity"]

    def test_limit_respected(self):
        import numpy as np
        for i in range(5):
            aid = f"lim{i:03d}"
            _insert_article(self.conn, aid, f"Limit Art {i}", f"wiki/internal/l{i}.md")
            self.conn.execute(
                "INSERT INTO article_embeddings (article_id, embedding, embedding_dim, model_name, created_at) "
                "VALUES (?, ?, 384, 'test', '2026-01-01')",
                (aid, _make_float32_blob(384, float(i + 1))),
            )
        self.conn.commit()

        mock_model = MagicMock()
        mock_model.encode.return_value = np.ones((1, 384), dtype=np.float32)

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings.np", np), \
             patch("kb.db.get_connection", return_value=self.conn):
            from kb.embeddings import vector_search
            results = vector_search("query", limit=2)

        assert len(results) == 2

    def test_raises_import_error_when_unavailable(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import vector_search
            with self.assertRaises(ImportError):
                vector_search("test")


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation(unittest.TestCase):

    def test_embed_text_degrades(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import embed_text
            with self.assertRaises(ImportError):
                embed_text("x")

    def test_embed_article_degrades(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import embed_article
            with self.assertRaises(ImportError):
                embed_article("id")

    def test_embed_all_articles_degrades(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import embed_all_articles
            with self.assertRaises(ImportError):
                embed_all_articles()

    def test_vector_search_degrades(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import vector_search
            with self.assertRaises(ImportError):
                vector_search("x")

    def test_get_article_embedding_degrades(self):
        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.embeddings import get_article_embedding
            with self.assertRaises(ImportError):
                get_article_embedding("id")


# ---------------------------------------------------------------------------
# Batch embedding
# ---------------------------------------------------------------------------

class TestBatchEmbedding(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = _make_conn()
        _reset_model()

    def _add_articles(self, count: int):
        """Write markdown files and insert article rows into the test conn."""
        wiki_dir = Path(self.tmp) / "wiki" / "internal"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            aid = f"b{i:04d}"
            slug = f"batch-art-{i}"
            md = wiki_dir / f"{slug}.md"
            md.write_text(f"# Batch {i}\n\nBody {i}.", encoding="utf-8")
            rel = str(md.relative_to(self.tmp))
            _insert_article(self.conn, aid, f"Batch Art {i}", rel)

    def test_embeds_all_articles(self):
        """Returns count equal to number of articles."""
        import numpy as np
        self._add_articles(5)

        mock_model = MagicMock()
        mock_model.encode.side_effect = lambda texts, **kw: np.ones(
            (len(texts), 384), dtype=np.float32
        )

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings._model", mock_model), \
             patch("kb.embeddings.np", np), \
             patch("kb.db.get_connection", return_value=self.conn), \
             patch("kb.config.get_project_root", return_value=Path(self.tmp)):
            from kb.embeddings import embed_all_articles
            count = embed_all_articles(batch_size=3)

        assert count == 5

    def test_batch_size_controls_encode_calls(self):
        """6 articles with batch_size=2 -> exactly 3 encode() calls."""
        import numpy as np
        self._add_articles(6)

        mock_model = MagicMock()
        mock_model.encode.side_effect = lambda texts, **kw: np.ones(
            (len(texts), 384), dtype=np.float32
        )

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings._model", mock_model), \
             patch("kb.embeddings.np", np), \
             patch("kb.db.get_connection", return_value=self.conn), \
             patch("kb.config.get_project_root", return_value=Path(self.tmp)):
            from kb.embeddings import embed_all_articles
            embed_all_articles(batch_size=2)

        assert mock_model.encode.call_count == 3

    def test_returns_zero_for_empty_db(self):
        """Returns 0 and never calls encode() when no articles exist."""
        import numpy as np
        mock_model = MagicMock()

        with patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.get_embedding_model", return_value=mock_model), \
             patch("kb.embeddings._model", mock_model), \
             patch("kb.embeddings.np", np), \
             patch("kb.db.get_connection", return_value=self.conn), \
             patch("kb.config.get_project_root", return_value=Path(self.tmp)):
            from kb.embeddings import embed_all_articles
            count = embed_all_articles()

        assert count == 0
        mock_model.encode.assert_not_called()


# ---------------------------------------------------------------------------
# Auto-embed on ingest
# ---------------------------------------------------------------------------

class TestAutoEmbedOnIngest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = _make_conn()
        _reset_model()

    def _content(self, title: str) -> str:
        import uuid
        uid = uuid.uuid4().hex[:6]
        return f"---\ntitle: {title} {uid}\n---\n\nBody text."

    def test_auto_embed_called_on_direct_ingest(self):
        """_ingest_direct calls embed_article once after successful write."""
        content = self._content("Embed Hook Test")
        embedded = []

        def fake_embed(aid):
            embedded.append(aid)

        with patch("kb.config.get_project_root", return_value=Path(self.tmp)), \
             patch("kb.db.get_connection", return_value=self.conn), \
             patch("kb.search.update_article_fts"), \
             patch("kb.embeddings._EMBEDDINGS_AVAILABLE", True), \
             patch("kb.embeddings.embed_article", side_effect=fake_embed):
            from kb.commands.ingest import _ingest_direct
            _ingest_direct(Path(self.tmp) / "src.md", content, "internal")

        assert len(embedded) == 1

    def test_auto_embed_skipped_gracefully_when_unavailable(self):
        """_ingest_direct completes without error when embeddings unavailable."""
        content = self._content("No Embed Test")

        with patch("kb.config.get_project_root", return_value=Path(self.tmp)), \
             patch("kb.db.get_connection", return_value=self.conn), \
             patch("kb.search.update_article_fts"), \
             patch("kb.embeddings._EMBEDDINGS_AVAILABLE", False):
            from kb.commands.ingest import _ingest_direct
            # Must not raise
            _ingest_direct(Path(self.tmp) / "src2.md", content, "internal")


if __name__ == "__main__":
    unittest.main()
