"""Tests for kb.memory.MemoryStore."""

import os
import sqlite3
import tempfile
import unittest

from kb.memory import MemoryStore, KNOWN_SECTIONS


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


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(
            self.tmp.name, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.store = MemoryStore(conn=self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    # ----- add / get --------------------------------------------------

    def test_add_entry_returns_id_and_persists(self):
        eid = self.store.add_entry(
            agent="beta",
            section="short_term",
            content="Drafted 8 tweets today",
            tags="session,tweets",
        )
        self.assertIsInstance(eid, int)
        self.assertGreater(eid, 0)

        entry = self.store.get_entry(eid)
        self.assertEqual(entry["agent"], "beta")
        self.assertEqual(entry["section"], "short_term")
        self.assertEqual(entry["content"], "Drafted 8 tweets today")
        self.assertEqual(entry["tags"], "session,tweets")

    def test_agent_is_normalized_lowercase(self):
        eid = self.store.add_entry("Beta", "short_term", "hi")
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["agent"], "beta")

    def test_add_entry_without_tags(self):
        eid = self.store.add_entry("beta", "short_term", "foo")
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["tags"], "")

    def test_add_entry_validates_inputs(self):
        with self.assertRaises(ValueError):
            self.store.add_entry("", "short_term", "content")
        with self.assertRaises(ValueError):
            self.store.add_entry("beta", "", "content")
        with self.assertRaises(ValueError):
            self.store.add_entry("beta", "short_term", "   ")

    def test_get_entries_grouped_and_sorted(self):
        self.store.add_entry("beta", "short_term", "a")
        self.store.add_entry("beta", "long_term", "b")
        self.store.add_entry("beta", "short_term", "c")
        self.store.add_entry("alpha", "short_term", "other agent")

        entries = self.store.get_entries(agent="beta")
        self.assertEqual(len(entries), 3)
        # short_term must come before long_term per KNOWN_SECTIONS order
        sections = [e["section"] for e in entries]
        self.assertEqual(sections[0], "short_term")
        self.assertEqual(sections[-1], "long_term")

    def test_get_entries_filters_by_section(self):
        self.store.add_entry("beta", "short_term", "a")
        self.store.add_entry("beta", "long_term", "b")
        entries = self.store.get_entries("beta", section="short_term")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["content"], "a")

    # ----- update / delete --------------------------------------------

    def test_update_entry(self):
        eid = self.store.add_entry("beta", "short_term", "old")
        ok = self.store.update_entry(eid, content="new", tags="fresh")
        self.assertTrue(ok)
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["content"], "new")
        self.assertEqual(entry["tags"], "fresh")

    def test_update_entry_missing_returns_false(self):
        self.assertFalse(self.store.update_entry(9999, content="x"))

    def test_delete_entry(self):
        eid = self.store.add_entry("beta", "short_term", "bye")
        self.assertTrue(self.store.delete_entry(eid))
        self.assertIsNone(self.store.get_entry(eid))
        self.assertFalse(self.store.delete_entry(eid))

    # ----- search ------------------------------------------------------

    def test_search_case_insensitive_substring(self):
        self.store.add_entry("beta", "short_term", "Drafted 8 tweets today")
        self.store.add_entry("beta", "short_term", "Scheduled Reddit scouting run")
        self.store.add_entry("beta", "long_term", "Values clarity over clever")

        hits = self.store.search("beta", "tweet")
        self.assertEqual(len(hits), 1)
        self.assertIn("tweets", hits[0]["content"])

        hits = self.store.search("beta", "TWEET")
        self.assertEqual(len(hits), 1)

        hits = self.store.search("beta", "")
        self.assertEqual(hits, [])

    def test_search_scoped_per_agent(self):
        self.store.add_entry("beta", "short_term", "tweet draft")
        self.store.add_entry("alpha", "short_term", "tweet from alpha")
        self.assertEqual(len(self.store.search("beta", "tweet")), 1)
        self.assertEqual(len(self.store.search("alpha", "tweet")), 1)

    # ----- promotion / clearing ---------------------------------------

    def test_promote_to_long_term(self):
        eid = self.store.add_entry("beta", "short_term", "learned something")
        self.assertTrue(self.store.promote_to_long_term(eid))
        entry = self.store.get_entry(eid)
        self.assertEqual(entry["section"], "long_term")
        self.assertEqual(entry["content"], "learned something")

    def test_promote_custom_target_section(self):
        eid = self.store.add_entry("beta", "short_term", "lesson")
        self.assertTrue(self.store.promote_to_long_term(eid, target_section="learned"))
        self.assertEqual(self.store.get_entry(eid)["section"], "learned")

    def test_clear_section(self):
        self.store.add_entry("beta", "short_term", "a")
        self.store.add_entry("beta", "short_term", "b")
        self.store.add_entry("beta", "long_term", "keep")
        removed = self.store.clear_section("beta", "short_term")
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.store.get_entries("beta", "short_term")), 0)
        self.assertEqual(len(self.store.get_entries("beta", "long_term")), 1)

    def test_clear_agent(self):
        self.store.add_entry("beta", "short_term", "x")
        self.store.add_entry("beta", "long_term", "y")
        self.store.add_entry("alpha", "short_term", "keep")
        self.assertEqual(self.store.clear_agent("beta"), 2)
        self.assertEqual(self.store.count("beta"), 0)
        self.assertEqual(self.store.count("alpha"), 1)

    def test_list_agents_and_count(self):
        self.store.add_entry("beta", "short_term", "a")
        self.store.add_entry("alpha", "long_term", "b")
        self.assertEqual(self.store.list_agents(), ["beta", "alpha"])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.count("beta"), 1)

    # ----- rendering ---------------------------------------------------

    def test_render_empty(self):
        out = self.store.render_for_prompt("beta")
        self.assertIn("## Memory", out)
        self.assertIn("(no entries yet)", out)

    def test_render_groups_and_headings(self):
        self.store.add_entry("beta", "short_term", "last session: 8 tweets")
        self.store.add_entry("beta", "long_term", "voice: witty, direct")
        self.store.add_entry("beta", "learned", "hashtags hurt reach")
        out = self.store.render_for_prompt("beta")
        self.assertIn("## Memory", out)
        self.assertIn("### Short-Term Memory", out)
        self.assertIn("### Long-Term Memory", out)
        self.assertIn("### Learned", out)
        # short_term heading should appear before long_term heading
        self.assertLess(
            out.index("### Short-Term Memory"),
            out.index("### Long-Term Memory"),
        )
        self.assertIn("- last session: 8 tweets", out)

    def test_render_respects_max_chars(self):
        long = "x" * 2000
        self.store.add_entry("beta", "short_term", long)
        out = self.store.render_for_prompt("beta", max_chars=200)
        self.assertLessEqual(len(out), 220)  # truncation suffix adds a bit
        self.assertIn("[truncated]", out)

    def test_render_unknown_section_falls_back_to_title(self):
        self.store.add_entry("beta", "custom_section", "hi")
        out = self.store.render_for_prompt("beta")
        self.assertIn("### Custom Section", out)

    def test_known_sections_are_all_present(self):
        # Defensive: migrating script depends on these section names existing.
        for name in KNOWN_SECTIONS:
            self.assertIsInstance(name, str)
            self.assertTrue(name)


if __name__ == "__main__":
    unittest.main()
