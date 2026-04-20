-- Update review_queue table to support compilation workflow
-- Phase 2 compilation review needs: raw_file_id, changes_summary, status, reviewer_notes, job_id
-- Note: proposed_changes (from original schema) is used to store full compilation output

-- Add new columns for compilation review
ALTER TABLE review_queue ADD COLUMN raw_file_id TEXT;
ALTER TABLE review_queue ADD COLUMN changes_summary TEXT;
ALTER TABLE review_queue ADD COLUMN status TEXT DEFAULT 'pending';
ALTER TABLE review_queue ADD COLUMN reviewer_notes TEXT;
ALTER TABLE review_queue ADD COLUMN job_id TEXT;

-- Create index for status column
CREATE INDEX idx_review_queue_status ON review_queue(status) WHERE status = 'pending';

-- Add foreign key reference to raw_files (can't add after table creation, but document it)
-- FOREIGN KEY (raw_file_id) REFERENCES raw_files(id) ON DELETE CASCADE
-- Note: SQLite doesn't support adding foreign keys after table creation

-- Add foreign key reference to jobs
-- FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
