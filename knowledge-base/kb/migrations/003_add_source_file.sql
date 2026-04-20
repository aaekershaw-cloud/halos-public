-- Add source_file column to articles table
-- Phase 2 compilation needs to track which raw file generated each article

ALTER TABLE articles ADD COLUMN source_file TEXT;

-- Create index for faster lookups
CREATE INDEX idx_articles_source_file ON articles(source_file);

-- Add foreign key reference (documented, can't enforce in SQLite after table creation)
-- FOREIGN KEY (source_file) REFERENCES raw_files(id) ON DELETE SET NULL
