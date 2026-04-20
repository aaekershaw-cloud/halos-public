-- Migration 010: Add hook_events table
-- Audit log for system hook events (ingest, update, delete, etc.)
-- event_type: the hook event name (e.g. "article.created", "article.deleted")
-- entity_id: id of the affected entity
-- entity_type: type of the entity (e.g. "article", "raw_file")
-- status: outcome of the hook (e.g. "success", "failed", "skipped")
-- data: JSON blob with event payload/details

CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_id TEXT,
    entity_type TEXT,
    status TEXT,
    data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hook_events_type_created ON hook_events(event_type, created_at DESC);

-- ROLLBACK:
-- DROP INDEX IF EXISTS idx_hook_events_type_created;
-- DROP TABLE IF EXISTS hook_events;
