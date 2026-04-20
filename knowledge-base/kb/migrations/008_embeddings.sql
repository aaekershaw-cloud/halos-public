-- Migration 008: Add article_embeddings table
-- Stores vector embeddings for semantic search.
-- One embedding per article; keyed by article_id.
-- embedding: raw bytes (BLOB) of the float32 vector
-- embedding_dim: number of dimensions in the vector
-- model_name: the embedding model used (e.g. "text-embedding-3-small")

CREATE TABLE IF NOT EXISTS article_embeddings (
    article_id TEXT PRIMARY KEY,
    embedding BLOB,
    embedding_dim INTEGER,
    model_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

-- ROLLBACK:
-- DROP TABLE IF EXISTS article_embeddings;
