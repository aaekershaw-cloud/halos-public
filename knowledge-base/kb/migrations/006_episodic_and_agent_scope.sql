-- Migration 006: Add agent_scope column to articles
-- Supports episodic/agent-scoped knowledge alongside shared (global) articles.
-- NULL agent_scope means the article is shared across all agents.

ALTER TABLE articles ADD COLUMN agent_scope TEXT;

-- Index for efficient filtered queries by agent scope (including soft-delete awareness)
CREATE INDEX IF NOT EXISTS idx_articles_agent_scope ON articles(agent_scope, deleted_at);

-- ROLLBACK:
-- DROP INDEX IF EXISTS idx_articles_agent_scope;
-- -- SQLite does not support DROP COLUMN in older versions; use table rebuild if needed.
-- -- In SQLite >= 3.35.0:
-- -- ALTER TABLE articles DROP COLUMN agent_scope;
