# Knowledge Base System

LLM-powered knowledge base with human-supervised compilation and persistent memory.

**Inspired by:** Andrej Karpathy's LLM knowledge base workflow

## Features

- **SQLite + FTS5** - Fast full-text search across all articles
- **Human-in-the-loop** - Review queue for LLM structural changes
- **Security classification** - Public/internal/confidential levels
- **Cost tracking** - Budget limits and spending reports
- **Git versioned** - All markdown articles tracked, database local
- **Job queue** - Background compilation with retry logic
- **Enhanced PDF extraction** - OCR, tables, and formatting with marker-pdf

## Installation

```bash
# Clone repository
cd knowledge-base

# Install dependencies
pip install -e .

# Optional: PII detection (requires ~500MB models)
pip install -e .[pii]

# Optional: Full install with marker-pdf
pip install -e .[full]
```

## Quick Start

```bash
# Initialize knowledge base
kb init

# Ingest a file
kb ingest file ~/Documents/article.md --classification internal

# Search
kb search "machine learning"

# Compile (Phase 2)
kb compile

# Query (Phase 2)
kb query "What is the difference between X and Y?"
```

## Project Status

**Phase 1 (Weeks 1-2):** Core infrastructure ✅ IN PROGRESS
- SQLite schema + migrations
- CLI framework
- Basic ingest
- FTS5 search
- Git setup

**Phase 2 (Weeks 3-4):** LLM compilation + review queue (Upcoming)

**Phase 3 (Weeks 5-6):** Q&A interface + integrity checks (Upcoming)

## Documentation

See `docs/` for detailed architecture:
- `knowledge-base-system-plan-v2.md` - Full system design
- `knowledge-base-v3-critical-fixes.md` - Implementation details
- `knowledge-base-v3-edge-cases-resolved.md` - Edge case handling

## License

MIT
