-- Initial schema for Knowledge Base System
-- Creates all core tables

-- Articles table (wiki articles metadata)
CREATE TABLE articles (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  slug TEXT UNIQUE NOT NULL,
  content_path TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  classification TEXT DEFAULT 'public',
  tags JSON,
  sources JSON,
  checksum TEXT NOT NULL
);

CREATE INDEX idx_articles_slug ON articles(slug);
CREATE INDEX idx_articles_updated ON articles(updated_at DESC);
CREATE INDEX idx_articles_classification ON articles(classification);

-- Links table (cross-references between articles)
CREATE TABLE links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  link_type TEXT DEFAULT 'related',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (source_id) REFERENCES articles(id) ON DELETE CASCADE,
  FOREIGN KEY (target_id) REFERENCES articles(id) ON DELETE CASCADE,
  UNIQUE(source_id, target_id, link_type)
);

CREATE INDEX idx_links_source ON links(source_id);
CREATE INDEX idx_links_target ON links(target_id);

-- Raw files table (ingested source materials)
CREATE TABLE raw_files (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  path TEXT UNIQUE NOT NULL,
  classification TEXT NOT NULL,
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source_url TEXT,
  archived_at TIMESTAMP,
  archive_path TEXT,
  processed_at TIMESTAMP,
  checksum TEXT NOT NULL
);

CREATE INDEX idx_raw_files_processed ON raw_files(processed_at);
CREATE INDEX idx_raw_files_classification ON raw_files(classification);

-- Review queue table (human review for LLM changes)
CREATE TABLE review_queue (
  id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  article_id TEXT,
  proposed_changes JSON NOT NULL,
  llm_confidence REAL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  reviewed_at TIMESTAMP,
  reviewer TEXT,
  decision TEXT,
  notes TEXT,
  params JSON
);

CREATE INDEX idx_review_queue_pending ON review_queue(reviewed_at) WHERE reviewed_at IS NULL;

-- Jobs table (background task queue)
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  params JSON,
  result JSON,
  error TEXT,
  retry_count INTEGER DEFAULT 0,
  last_error TEXT,
  review_id TEXT
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX idx_jobs_review ON jobs(review_id) WHERE review_id IS NOT NULL;

-- Costs table (LLM API cost tracking)
CREATE TABLE costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  operation TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  job_id TEXT,
  FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX idx_costs_timestamp ON costs(timestamp DESC);

-- FTS5 search index (no triggers, application-managed)
CREATE VIRTUAL TABLE articles_fts USING fts5(
  title,
  content,
  tags
);

-- Note: FTS index is updated by application code in kb/search.py
-- No triggers are used to maintain portability
