"""Tests for kb.consolidation: consolidate_short_term and compress_session."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from kb.memory import MemoryStore, KNOWN_SECTIONS
from kb.consolidation import consolidate_short_term, compress_session


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent_section
    ON agent_memory(agent, section);
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent_created
    ON agent_memory(agent, created_at);
"""

# Fake LLM result returned by mocked call_llm
_FAKE_CONSOLIDATE_RESULT = {
    "content": (
        '{"lessons": ["Always test edge cases"],'
        ' "facts": ["Python 3 is required"],'
        ' "decisions": ["Use SQLite for storage"]}'
    ),
    "model": "claude-haiku-4-5-20251001",
    "input_tokens": 100,
    "output_tokens": 50,
    "cost_usd": 0.0002,
}

_FAKE_COMPRESS_RESULT = {
    "content": "The agent processed tweets and learned about social media timing.",
    "model": "claude-haiku-4-5-20251001",
    "input_tokens": 80,
    "output_tokens": 20,
    "cost_usd": 0.0001,
}


def _old_ts():
    """Return an ISO timestamp 72 hours ago (older than the 48h cutoff)."""
    return (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()


def _new_ts():
    """Return an ISO timestamp 1 hour ago (newer than the 48h cutoff)."""
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


class ConsolidationTestBase(unittest.TestCase):
    """Shared setUp/tearDown with an isolated SQLite store."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(
            self.tmp.name, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.store = MemoryStore(conn=self.conn)

        # Patch MemoryStore inside consolidation so it uses the test store.
        self._store_patcher = patch(
            "kb.consolidation.MemoryStore", return_value=self.store
        )
        self._store_patcher.start()

    def tearDown(self):
        self._store_patcher.stop()
        self.conn.close()
        os.unlink(self.tmp.name)

    def _add_old(self, agent, content):
        """Add a short_term entry with a timestamp older than 48 hours."""
        eid = self.store.add_entry(agent, "short_term", content)
        self.conn.execute(
            "UPDATE agent_memory SET created_at=?, updated_at=? WHERE id=?",
            (_old_ts(), _old_ts(), eid),
        )
        return eid

    def _add_new(self, agent, content):
        """Add a short_term entry with a timestamp within 48 hours."""
        eid = self.store.add_entry(agent, "short_term", content)
        self.conn.execute(
            "UPDATE agent_memory SET created_at=?, updated_at=? WHERE id=?",
            (_new_ts(), _new_ts(), eid),
        )
        return eid


class TestConsolidateShortTermDryRun(ConsolidationTestBase):
    """consolidate_short_term with dry_run=True must not mutate the DB."""

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_dry_run_does_not_write_entries(self, mock_llm, mock_cost):
        self._add_old("alpha", "old session note 1")
        self._add_old("alpha", "old session note 2")

        stats = consolidate_short_term("alpha", dry_run=True, model="haiku")

        # LLM was called
        mock_llm.assert_called_once()

        # No new entries were written; originals still present
        self.assertEqual(self.store.count("alpha"), 2)
        self.assertEqual(stats["entries_deleted"], 0)
        self.assertEqual(stats["learned_added"], 0)
        self.assertEqual(stats["long_term_added"], 0)
        self.assertEqual(stats["decisions_added"], 0)

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_dry_run_still_returns_summaries(self, mock_llm, mock_cost):
        self._add_old("alpha", "some lesson")

        stats = consolidate_short_term("alpha", dry_run=True, model="haiku")

        self.assertIn("lessons", stats["summaries"])
        self.assertIn("facts", stats["summaries"])
        self.assertIn("decisions", stats["summaries"])


class TestConsolidateAgeThreshold(ConsolidationTestBase):
    """Only entries older than 48 hours should be eligible."""

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_new_entries_are_skipped(self, mock_llm, mock_cost):
        self._add_new("alpha", "brand new note")

        stats = consolidate_short_term("alpha", dry_run=True, model="haiku")

        # No eligible entries — LLM must not be called
        mock_llm.assert_not_called()
        self.assertEqual(stats["entries_reviewed"], 0)

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_mixed_ages_only_processes_old(self, mock_llm, mock_cost):
        self._add_old("alpha", "old note eligible")
        self._add_new("alpha", "fresh note skip me")

        stats = consolidate_short_term("alpha", dry_run=True, model="haiku")

        mock_llm.assert_called_once()
        self.assertEqual(stats["entries_reviewed"], 1)

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_limit_caps_eligible_entries(self, mock_llm, mock_cost):
        for i in range(10):
            self._add_old("alpha", f"old note {i}")

        stats = consolidate_short_term("alpha", limit=3, dry_run=True, model="haiku")

        self.assertEqual(stats["entries_reviewed"], 3)


class TestConsolidateEmptyShortTerm(ConsolidationTestBase):
    """consolidate_short_term with no eligible entries must return early."""

    def test_empty_short_term_returns_zero_stats(self):
        stats = consolidate_short_term("alpha", dry_run=True, model="haiku")

        self.assertEqual(stats["entries_reviewed"], 0)
        self.assertEqual(stats["long_term_added"], 0)
        self.assertEqual(stats["learned_added"], 0)
        self.assertEqual(stats["decisions_added"], 0)
        self.assertEqual(stats["entries_deleted"], 0)

    def test_empty_short_term_does_not_call_llm(self):
        with patch("kb.consolidation.call_llm") as mock_llm:
            consolidate_short_term("alpha", dry_run=True, model="haiku")
            mock_llm.assert_not_called()


class TestConsolidateLiveRun(ConsolidationTestBase):
    """consolidate_short_term with dry_run=False should mutate the DB."""

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_CONSOLIDATE_RESULT)
    def test_live_run_writes_entries_and_deletes_originals(self, mock_llm, mock_cost):
        self._add_old("alpha", "old session fact")

        stats = consolidate_short_term("alpha", dry_run=False, model="haiku")

        # Original entry should be deleted
        self.assertEqual(stats["entries_deleted"], 1)
        self.assertEqual(
            len(self.store.get_entries("alpha", section="short_term")), 0
        )

        # At least one parsed entry should be written
        total_added = (
            stats["learned_added"]
            + stats["long_term_added"]
            + stats["decisions_added"]
        )
        self.assertGreater(total_added, 0)


class TestCompressSessionMockedLLM(ConsolidationTestBase):
    """compress_session with a mocked LLM."""

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_COMPRESS_RESULT)
    def test_compress_returns_stats(self, mock_llm, mock_cost):
        self.store.add_entry("alpha", "short_term", "tweet drafted")
        self.store.add_entry("alpha", "short_term", "scheduled post")

        stats = compress_session("alpha", dry_run=True, model="haiku")

        self.assertEqual(stats["entries_compressed"], 2)
        self.assertIn("tweet", stats["summary_text"])
        mock_llm.assert_called_once()

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_COMPRESS_RESULT)
    def test_compress_dry_run_preserves_short_term(self, mock_llm, mock_cost):
        self.store.add_entry("alpha", "short_term", "tweet drafted")

        compress_session("alpha", dry_run=True, model="haiku")

        # short_term must be untouched in dry_run
        self.assertEqual(
            len(self.store.get_entries("alpha", section="short_term")), 1
        )

    @patch("kb.consolidation.record_cost")
    @patch("kb.consolidation.call_llm", return_value=_FAKE_COMPRESS_RESULT)
    def test_compress_live_clears_short_term(self, mock_llm, mock_cost):
        self.store.add_entry("alpha", "short_term", "note one")
        self.store.add_entry("alpha", "short_term", "note two")

        stats = compress_session("alpha", dry_run=False, model="haiku")

        self.assertEqual(stats["entries_compressed"], 2)
        # short_term must be cleared
        self.assertEqual(
            len(self.store.get_entries("alpha", section="short_term")), 0
        )
        # Summary must be written to episodic
        episodic = self.store.get_entries("alpha", section="episodic")
        self.assertEqual(len(episodic), 1)
        self.assertIn("tweet", episodic[0]["content"])

    def test_compress_empty_short_term_returns_zero(self):
        stats = compress_session("alpha", dry_run=True, model="haiku")

        self.assertEqual(stats["entries_compressed"], 0)
        self.assertEqual(stats["summary_text"], "")

    def test_compress_empty_does_not_call_llm(self):
        with patch("kb.consolidation.call_llm") as mock_llm:
            compress_session("alpha", dry_run=True, model="haiku")
            mock_llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
