-- Migration 005: Add agent_memory table
-- Stores per-agent memory entries organized by section.
-- Replaces the per-agent memory.md flat files that used to live under
-- sessions/<agent>/memory.md in the HalOS repo.

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
