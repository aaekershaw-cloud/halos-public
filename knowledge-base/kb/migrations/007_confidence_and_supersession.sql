-- Migration 007: Add confidence scoring and supersession tracking to articles
-- confidence_score: float in [0,1] indicating how trustworthy this article is
-- source_count: number of distinct sources that back this article
-- last_confirmed_at: when the article was last validated/confirmed accurate
-- superseded_by: article id of the replacement article, if this one is outdated

ALTER TABLE articles ADD COLUMN confidence_score REAL DEFAULT 0.5;
ALTER TABLE articles ADD COLUMN source_count INTEGER DEFAULT 0;
ALTER TABLE articles ADD COLUMN last_confirmed_at TIMESTAMP;
ALTER TABLE articles ADD COLUMN superseded_by TEXT;

CREATE INDEX IF NOT EXISTS idx_articles_confidence ON articles(confidence_score);
CREATE INDEX IF NOT EXISTS idx_articles_last_confirmed ON articles(last_confirmed_at);
CREATE INDEX IF NOT EXISTS idx_articles_superseded ON articles(superseded_by) WHERE superseded_by IS NOT NULL;

-- ROLLBACK:
-- DROP INDEX IF EXISTS idx_articles_confidence;
-- DROP INDEX IF EXISTS idx_articles_last_confirmed;
-- DROP INDEX IF EXISTS idx_articles_superseded;
-- -- In SQLite >= 3.35.0:
-- -- ALTER TABLE articles DROP COLUMN confidence_score;
-- -- ALTER TABLE articles DROP COLUMN source_count;
-- -- ALTER TABLE articles DROP COLUMN last_confirmed_at;
-- -- ALTER TABLE articles DROP COLUMN superseded_by;
