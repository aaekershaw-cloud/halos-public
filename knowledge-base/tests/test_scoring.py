"""Tests for confidence scoring module"""

import os
import sys
import tempfile
import shutil
import pytest
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.db import init_database, get_connection
from kb.scoring import (
    calculate_confidence,
    update_confidence,
    confirm_article,
    supersede_article
)


class TestConfidenceScoring:
    """Test confidence scoring functionality"""

    def setup_method(self):
        """Set up test database"""
        self.test_dir = tempfile.mkdtemp(prefix='kb_test_scoring_')
        os.environ['KB_DIR'] = os.path.join(self.test_dir, '.kb')
        init_database()

        # Apply migrations to get latest schema
        from kb.db import apply_migrations
        apply_migrations()

        # Clear any existing articles first
        conn = get_connection()
        conn.execute("DELETE FROM articles")

        # Create test articles
        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO articles (id, title, slug, content_path, classification, checksum, confidence_score, source_count)
            VALUES
                ('art1', 'Article 1', 'article-1', '/tmp/art1.md', 'internal', 'abc123', 0.5, 0),
                ('art2', 'Article 2', 'article-2', '/tmp/art2.md', 'internal', 'def456', 0.5, 3),
                ('art3', 'Article 3', 'article-3', '/tmp/art3.md', 'internal', 'ghi789', 0.5, 0)
        """)
        conn.execute("COMMIT")

    def teardown_method(self):
        """Clean up test database"""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        if 'KB_DIR' in os.environ:
            del os.environ['KB_DIR']

    def test_calculate_confidence_base(self):
        """Test confidence calculation with base score"""
        score = calculate_confidence('art1')
        assert 0.0 <= score <= 1.0
        # With source_count=0, base should be 0.3
        assert 0.25 <= score <= 0.35

    def test_calculate_confidence_with_sources(self):
        """Test confidence calculation with multiple sources"""
        score = calculate_confidence('art2')
        # With source_count=3, base should be 0.3 + 0.3 = 0.6
        assert 0.55 <= score <= 0.65

    def test_calculate_confidence_age_penalty(self):
        """Test age penalty for old unconfirmed articles"""
        conn = get_connection()
        # Set last_confirmed_at to 200 days ago
        old_date = (datetime.now() - timedelta(days=200)).isoformat()
        conn.execute("BEGIN")
        conn.execute("""
            UPDATE articles
            SET last_confirmed_at = ?
            WHERE id = 'art1'
        """, (old_date,))
        conn.execute("COMMIT")

        score = calculate_confidence('art1')
        # Should have age penalty applied
        assert score < 0.3

    def test_calculate_confidence_never_exceeds_one(self):
        """Test that calculated confidence never exceeds 1.0"""
        conn = get_connection()
        conn.execute("BEGIN")
        conn.execute("""
            UPDATE articles
            SET source_count = 10
            WHERE id = 'art1'
        """, )
        conn.execute("COMMIT")

        score = calculate_confidence('art1')
        assert score <= 1.0

    def test_update_confidence_manual(self):
        """Test manual confidence score update"""
        score = update_confidence('art1', score=0.95, auto=False)
        assert score == 0.95

        # Verify in database
        conn = get_connection()
        cursor = conn.execute("""
            SELECT confidence_score FROM articles WHERE id = 'art1'
        """)
        row = cursor.fetchone()
        assert row['confidence_score'] == 0.95

    def test_update_confidence_auto_caps_at_99(self):
        """Test that auto-calculated scores are capped at 0.99"""
        conn = get_connection()
        conn.execute("BEGIN")
        conn.execute("""
            UPDATE articles
            SET source_count = 10, last_confirmed_at = ?
            WHERE id = 'art1'
        """, (datetime.now().isoformat(),))
        conn.execute("COMMIT")

        score = update_confidence('art1', auto=True)
        assert score < 1.0
        assert score <= 0.99

    def test_confirm_article(self):
        """Test confirming an article"""
        score = confirm_article('art1', reviewer='human')

        # Should bump to at least 0.7
        assert score >= 0.7

        # Verify last_confirmed_at is set
        conn = get_connection()
        cursor = conn.execute("""
            SELECT last_confirmed_at, confidence_score
            FROM articles WHERE id = 'art1'
        """)
        row = cursor.fetchone()
        assert row['last_confirmed_at'] is not None
        assert row['confidence_score'] >= 0.7

    def test_confirm_article_preserves_high_score(self):
        """Test that confirmation doesn't lower existing high scores"""
        # Set initial score to 0.9
        update_confidence('art1', score=0.9, auto=False)

        # Confirm it
        score = confirm_article('art1')

        # Should preserve the 0.9 score (not lower to 0.7)
        assert score == 0.9

    def test_supersede_article(self):
        """Test superseding an article"""
        supersede_article('art1', 'art2')

        # Verify superseded_by is set
        conn = get_connection()
        cursor = conn.execute("""
            SELECT superseded_by FROM articles WHERE id = 'art1'
        """)
        row = cursor.fetchone()
        assert row['superseded_by'] == 'art2'

        # Verify supersedes link was created
        cursor = conn.execute("""
            SELECT COUNT(*) as count FROM links
            WHERE source_id = 'art2'
              AND target_id = 'art1'
              AND link_type = 'supersedes'
        """)
        row = cursor.fetchone()
        assert row['count'] == 1

    def test_supersede_nonexistent_article(self):
        """Test superseding with nonexistent new article fails"""
        from kb.errors import PermanentError
        with pytest.raises(PermanentError):
            supersede_article('art1', 'nonexistent')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
