# Knowledge Base Quick Start Guide

## Initial Setup

### 1. Set API Key
```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```
Add to `~/.zshrc` or `~/.bashrc` to persist.

### 2. Initialize (already done!)
```bash
kb init
```

## Directory Structure
```
knowledge-base/
├── .kb/              # Database and internal files
├── raw/              # Ingested source materials
│   ├── public/
│   ├── internal/
│   └── confidential/
├── wiki/             # Compiled wiki articles
│   ├── public/
│   ├── internal/
│   └── confidential/
└── archive/          # Archived files
```

## Basic Workflow

### 1. Ingest Source Materials
```bash
# Ingest a local file
kb ingest file path/to/document.md --classification public

# Ingest a PDF
kb ingest pdf path/to/document.pdf --classification internal

# Ingest from URL
kb ingest url https://example.com/article --classification public

# Clone and index a git repo
kb ingest repo https://github.com/user/repo --classification public
```

### 2. Compile to Wiki Articles (requires API key)
```bash
# Compile a specific file
kb compile file <file-id> --auto-approve

# Compile all pending files
kb compile all --model sonnet

# Queue for background processing
kb compile queue <file-id>
```

### 3. Review Queue (if not using --auto-approve)
```bash
# List pending reviews
kb review list

# Review and approve/reject
kb review approve <review-id>
kb review reject <review-id> --reason "..."
```

### 4. Search & Query
```bash
# Full-text search
kb search "machine learning" --tag python

# Ask questions (RAG)
kb query ask "How does the authentication system work?"

# Find related articles
kb query related <article-id>
```

## Advanced Features

### PII Detection
```bash
# Scan a file for PII
kb pii scan-file path/to/file.md

# Scan all articles
kb pii scan-all --confidence 0.8
```

### Retention Policies
```bash
# Apply retention policies (dry run)
kb retention run --dry-run

# Apply for real
kb retention run

# Manually archive an article
kb retention archive <article-id>

# Soft delete with recovery period
kb retention soft-delete <article-id> --grace-days 30

# Recover a soft-deleted article
kb retention recover <article-id>
```

### Background Worker
```bash
# Start worker as daemon
kb worker start --daemon

# Check status
kb worker status

# Stop worker
kb worker stop
```

### Maintenance & Analytics
```bash
# Run integrity checks
kb lint check

# Auto-fix issues
kb lint fix

# Generate analytics report
kb analytics --days 7

# Create backup
kb backup --max-backups 10
```

## Example Session

```bash
# 1. Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 2. Ingest a document
kb ingest file ~/Documents/api-docs.md --classification internal

# Output: ✓ Ingested: abc123.md (ID: abc123)

# 3. Compile it
kb compile file abc123 --auto-approve

# Output: ✓ Compiled article: API Documentation (slug: api-documentation)

# 4. Search for it
kb search "API authentication"

# 5. Ask a question
kb query ask "How do I authenticate API requests?"

# 6. Check system health
kb analytics
```

## Configuration

### Environment Variables
- `ANTHROPIC_API_KEY` - Your Anthropic API key (required for LLM features)
- `KB_DIR` - Knowledge base directory (default: current directory)

### Cost Management
LLM operations are tracked in the costs table:
```bash
kb costs summary --days 7
kb costs by-operation
```

## Tips

1. **Start with small files** - Test the compilation workflow with a simple markdown file
2. **Use --dry-run** - Preview retention policies before applying
3. **Enable the worker** - Automate compilation and maintenance tasks
4. **Monitor costs** - Check `kb costs summary` regularly
5. **Run integrity checks** - Use `kb lint check` to catch issues early
6. **Use tags** - Tag articles for better organization and search

## Troubleshooting

**"No API key" error:**
```bash
export ANTHROPIC_API_KEY="your-key"
```

**Search returns no results:**
```bash
kb lint rebuild-fts  # Rebuild search index
```

**Worker won't start:**
```bash
kb worker status  # Check if already running
rm ~/.kb/worker.pid  # Remove stale PID file
```

## Next Steps

- Ingest your existing documentation
- Set up the background worker for automation
- Configure retention policies for your use case
- Set up regular backups
