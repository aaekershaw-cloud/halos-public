-- Migration 009: Add article_relationships table
-- Stores typed, directional relationships between articles beyond simple links.
-- relationship_type examples: "supersedes", "contradicts", "elaborates", "cites"
-- metadata: JSON blob for any extra relationship attributes

CREATE TABLE IF NOT EXISTS article_relationships (
    id TEXT PRIMARY KEY,
    from_article_id TEXT NOT NULL,
    to_article_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_article_id) REFERENCES articles(id) ON DELETE CASCADE,
    FOREIGN KEY (to_article_id) REFERENCES articles(id) ON DELETE CASCADE,
    UNIQUE (from_article_id, to_article_id, relationship_type)
);

CREATE INDEX IF NOT EXISTS idx_article_relationships_from ON article_relationships(from_article_id);
CREATE INDEX IF NOT EXISTS idx_article_relationships_to ON article_relationships(to_article_id);
CREATE INDEX IF NOT EXISTS idx_article_relationships_type ON article_relationships(relationship_type);

-- ROLLBACK:
-- DROP INDEX IF EXISTS idx_article_relationships_from;
-- DROP INDEX IF EXISTS idx_article_relationships_to;
-- DROP INDEX IF EXISTS idx_article_relationships_type;
-- DROP TABLE IF EXISTS article_relationships;
