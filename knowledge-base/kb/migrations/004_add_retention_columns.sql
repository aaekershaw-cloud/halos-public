-- Migration 004: Add retention policy columns
-- Adds columns for soft delete, archiving, and retention management

-- Add deleted_at column for soft delete tracking
ALTER TABLE articles ADD COLUMN deleted_at TEXT;

-- Add archived_at column for archive tracking
ALTER TABLE articles ADD COLUMN archived_at TEXT;

-- Add grace_period_days column for soft delete grace period
ALTER TABLE articles ADD COLUMN grace_period_days INTEGER;

-- Create indexes for retention queries
CREATE INDEX IF NOT EXISTS idx_articles_deleted_at ON articles(deleted_at);
CREATE INDEX IF NOT EXISTS idx_articles_archived_at ON articles(archived_at);
