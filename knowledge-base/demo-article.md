# Knowledge Base System Overview

The Knowledge Base System is an LLM-powered documentation system that transforms raw materials into a searchable wiki.

## Key Features

### Automated Compilation
- Ingests files, PDFs, URLs, and git repositories
- Uses Claude to compile raw materials into structured wiki articles
- Human review queue for approval

### Search & Discovery
- Full-text search with FTS5
- Wiki-style links: [[Article Name]]
- Related article suggestions
- Q&A with RAG (Retrieval-Augmented Generation)

### Data Management
- PII detection and redaction
- Retention policies with soft delete
- Integrity checking and auto-fix
- Background worker for automation

### Security
- Classification levels: public, internal, confidential
- Cost tracking for LLM operations
- Analytics and health monitoring

## Quick Start

1. Ingest source materials: `kb ingest file <path>`
2. Compile to wiki: `kb compile submit <file-id>`
3. Review changes: `kb review list`
4. Search: `kb search <query>`
5. Ask questions: `kb query ask "<question>"`

## Example Use Cases

- Technical documentation repository
- Research notes compilation
- Team knowledge sharing
- Compliance documentation with retention policies
