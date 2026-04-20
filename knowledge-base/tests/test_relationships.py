"""Tests for typed relationships module"""

import os
import sys
import tempfile
import shutil
import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb.db import init_database, get_connection
from kb.relationships import (
    RELATIONSHIP_TYPES,
    add_relationship,
    get_relationships,
    remove_relationship
)
from kb.links import add_typed_link, LINK_TYPE_VOCABULARY


class TestRelationships:
    """Test typed relationships functionality"""

    def setup_method(self):
        """Set up test database"""
        self.test_dir = tempfile.mkdtemp(prefix='kb_test_relationships_')
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
            INSERT INTO articles (id, title, slug, content_path, classification, checksum)
            VALUES
                ('art1', 'Article 1', 'article-1', '/tmp/art1.md', 'internal', 'abc123'),
                ('art2', 'Article 2', 'article-2', '/tmp/art2.md', 'internal', 'def456'),
                ('art3', 'Article 3', 'article-3', '/tmp/art3.md', 'internal', 'ghi789')
        """)
        conn.execute("COMMIT")

    def teardown_method(self):
        """Clean up test database"""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        if 'KB_DIR' in os.environ:
            del os.environ['KB_DIR']

    def test_relationship_types_constant(self):
        """Test that RELATIONSHIP_TYPES contains expected types"""
        assert 'supersedes' in RELATIONSHIP_TYPES
        assert 'depends_on' in RELATIONSHIP_TYPES
        assert 'uses' in RELATIONSHIP_TYPES
        assert len(RELATIONSHIP_TYPES) >= 8

    def test_add_relationship(self):
        """Test adding a relationship"""
        rel_id = add_relationship('art1', 'art2', 'depends_on')
        assert rel_id is not None

        # Verify it was stored
        rels = get_relationships('art1', direction='outgoing')
        assert len(rels) == 1
        assert rels[0]['to_article_id'] == 'art2'
        assert rels[0]['relationship_type'] == 'depends_on'

    def test_get_relationships_outgoing(self):
        """Test getting outgoing relationships"""
        add_relationship('art1', 'art2', 'uses')
        add_relationship('art1', 'art3', 'refines')

        rels = get_relationships('art1', direction='outgoing')
        assert len(rels) == 2
        assert {r['to_article_id'] for r in rels} == {'art2', 'art3'}

    def test_get_relationships_incoming(self):
        """Test getting incoming relationships"""
        add_relationship('art1', 'art3', 'uses')
        add_relationship('art2', 'art3', 'supports')

        rels = get_relationships('art3', direction='incoming')
        assert len(rels) == 2
        assert {r['from_article_id'] for r in rels} == {'art1', 'art2'}

    def test_get_relationships_filtered_by_type(self):
        """Test filtering relationships by type"""
        add_relationship('art1', 'art2', 'uses')
        add_relationship('art1', 'art3', 'depends_on')

        uses_rels = get_relationships('art1', direction='outgoing', rel_type='uses')
        assert len(uses_rels) == 1
        assert uses_rels[0]['relationship_type'] == 'uses'

    def test_remove_relationship(self):
        """Test removing a relationship"""
        rel_id = add_relationship('art1', 'art2', 'uses')
        assert rel_id is not None

        # Remove it
        remove_relationship('art1', 'art2', 'uses')

        # Verify it's gone
        rels = get_relationships('art1', direction='outgoing')
        assert len(rels) == 0

    def test_link_type_vocabulary(self):
        """Test that LINK_TYPE_VOCABULARY contains expected types"""
        assert 'wiki_link' in LINK_TYPE_VOCABULARY
        assert 'related' in LINK_TYPE_VOCABULARY
        assert 'supersedes' in LINK_TYPE_VOCABULARY
        assert 'depends_on' in LINK_TYPE_VOCABULARY
        assert len(LINK_TYPE_VOCABULARY) == 12

    def test_add_typed_link_valid(self):
        """Test adding a typed link with valid type"""
        add_typed_link('art1', 'art2', 'depends_on')

        # Verify in links table
        conn = get_connection()
        cursor = conn.execute("""
            SELECT link_type FROM links
            WHERE source_id = 'art1' AND target_id = 'art2'
        """)
        row = cursor.fetchone()
        assert row is not None
        assert row['link_type'] == 'depends_on'

    def test_add_typed_link_invalid_type(self):
        """Test that invalid link type raises ValueError"""
        with pytest.raises(ValueError, match="Invalid link type"):
            add_typed_link('art1', 'art2', 'invalid_type')

    def test_add_typed_link_nonexistent_article(self):
        """Test that linking to nonexistent article raises error"""
        from kb.errors import PermanentError
        with pytest.raises(PermanentError, match="does not exist"):
            add_typed_link('art1', 'nonexistent', 'uses')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
