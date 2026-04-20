# HalOS Integration Guide

This guide shows how to integrate the Knowledge Base system with HalOS/kimi so agents can access and manage the knowledge base.

## Quick Setup

### 1. Add Knowledge Base Helper to Agent's CLAUDE.md

Add this section to any agent's `CLAUDE.md` file (e.g., `~/Projects/halos/sessions/gamma/CLAUDE.md`):

```markdown
## Knowledge Base Access

You have access to the knowledge base CLI at `~/Projects/knowledge-base`. Use it to:
- Search documentation: `kb search "<query>"`
- Ask questions: `kb query ask "<question>"`
- Ingest new files: `kb ingest file <path>`
- Check system health: `kb analytics`

The knowledge base contains technical documentation, research notes, and project information.

**Important:** Always run kb commands from the knowledge base directory:
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli <command>
```
```

### 2. Python Integration (Optional)

For programmatic access, agents can use the KBClient wrapper:

```python
# In agent's working directory
import sys
sys.path.insert(0, '~/Projects/knowledge-base')

from kb.kimi_integration import KBClient

# Initialize client
kb = KBClient(kb_dir='~/Projects/knowledge-base')

# Search for articles
results = kb.search("authentication", format="json")
if results["success"]:
    print(f"Found {len(results['data'])} articles")

# Ask a question
answer = kb.ask("How does the authentication system work?")
if answer["success"]:
    print(answer["data"]["answer"])
    print("\nSources:")
    for source in answer["data"]["sources"]:
        print(f"  - {source['title']}")

# Check system health
health = kb.analytics(days=7)
```

## Common Use Cases

### 1. Documentation Search

**User:** "Find documentation about authentication"

**Agent can:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli search "authentication" --format json
```

Or with Python:
```python
kb = KBClient()
results = kb.search("authentication")
```

### 2. Q&A with Context

**User:** "How do I implement OAuth in our system?"

**Agent can:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli query ask "How do I implement OAuth in our system?" --output json
```

Or with Python:
```python
answer = kb.ask("How do I implement OAuth in our system?")
```

### 3. Ingest New Documentation

**User:** "Add this API doc to the knowledge base" (with file attachment)

**Agent can:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli ingest file /path/to/doc.md --classification internal
```

Or with Python:
```python
result = kb.ingest_file("/path/to/doc.md", classification="internal")
```

### 4. Compile Documentation

**User:** "Compile the ingested files"

**Agent can:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli compile all --model sonnet
```

Or with Python:
```python
result = kb.compile_all(model="sonnet")
```

### 5. System Health Check

**User:** "How's the knowledge base doing?"

**Agent can:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli analytics --days 7 --output json
```

Or with Python:
```python
report = kb.analytics(days=7)
health_score = report["data"]["health"]["health_score"]
print(f"Health score: {health_score}/100")
```

## Example Agent Workflow

Here's how Gamma could help with the knowledge base:

**User:** "Search our docs for info about the payment API"

**Gamma:**
```python
from kb.kimi_integration import KBClient

kb = KBClient(kb_dir='~/Projects/knowledge-base')

# Search for payment API docs
results = kb.search("payment API", format="json")

if results["success"] and results["data"]:
    articles = results["data"]
    print(f"Found {len(articles)} relevant articles:")
    for article in articles[:5]:
        print(f"\n**{article['title']}** (slug: {article['slug']})")
        print(f"   Tags: {', '.join(article.get('tags', []))}")
        print(f"   Updated: {article['updated_at']}")

    # Ask a follow-up question with context
    answer = kb.ask("What are the authentication requirements for the payment API?")
    if answer["success"]:
        print("\n" + answer["data"]["answer"])
else:
    print("No articles found. Would you like me to search the web or check if we need to ingest documentation?")
```

## Agent Session Setup

### Add to Gamma (Technical Agent)

File: `~/Projects/halos/sessions/gamma/CLAUDE.md`

```markdown
### Knowledge Base Integration

The knowledge base is available at `~/Projects/knowledge-base`. Use it for:

- **Documentation search:** When the user asks about technical documentation, APIs, or architecture
- **Q&A:** When you need context from our documentation to answer questions
- **Ingestion:** When the user shares new documentation or wants to add files
- **Health checks:** Periodic checks on documentation quality and coverage

**Quick commands:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli search "<query>" --format json
cd ~/Projects/knowledge-base && python3 -m kb.cli query ask "<question>" --output json
cd ~/Projects/knowledge-base && python3 -m kb.cli analytics --output json
```

**Python integration:**
```python
from kb.kimi_integration import KBClient
kb = KBClient(kb_dir='~/Projects/knowledge-base')
results = kb.search("topic")
answer = kb.ask("question")
```

Always check the knowledge base BEFORE searching the web for technical questions about our systems.
```

### Add to Alpha (Personal Assistant)

File: `~/Projects/halos/sessions/alpha/CLAUDE.md`

```markdown
### Knowledge Base Access

The knowledge base at `~/Projects/knowledge-base` contains the user's technical documentation and notes.

Use it when the user asks:
- "What's in my knowledge base about X?"
- "Do we have docs on Y?"
- "Add this to the knowledge base"

**Commands:**
```bash
cd ~/Projects/knowledge-base && python3 -m kb.cli search "<topic>"
cd ~/Projects/knowledge-base && python3 -m kb.cli ingest file <path>
```
```

## API Reference

### KBClient Methods

```python
# Search & Query
kb.search(query, tag=None, classification=None, format="json")
kb.ask(question, max_articles=5, cost_limit=0.25)
kb.related_articles(article_id, limit=5)

# Ingestion
kb.ingest_file(filepath, classification="internal")
kb.ingest_url(url, classification="internal")

# Compilation
kb.compile_file(file_id, model="sonnet", auto_approve=False)
kb.compile_all(model="sonnet")

# Review Queue
kb.review_list(format="json")
kb.review_approve(review_id)
kb.review_reject(review_id, reason="")

# Analytics & Health
kb.analytics(days=7, format="json")
kb.integrity_check()
kb.auto_fix()

# PII Detection
kb.scan_file_for_pii(filepath, confidence=0.7)
kb.scan_all_pii(confidence=0.7)

# Retention
kb.apply_retention(dry_run=True)

# Worker
kb.worker_status()
kb.worker_start(daemon=True)
kb.worker_stop()

# Costs
kb.get_costs(days=7)
```

### Response Format

All methods return a dict with:
```python
{
    "success": bool,          # True if command succeeded
    "output": str,            # Raw output (if not JSON)
    "data": dict,             # Parsed JSON data (if capture_json=True)
    "error": str,             # Error message (if failed)
    "returncode": int         # Process return code
}
```

## Automation Ideas

### 1. Daily Documentation Health Check

Add to Gamma's `heartbeat.md`:

```markdown
## Recurring

Every morning at 8am, check knowledge base health and report if issues found.

## Knowledge Base Health Check

Run daily health check on the knowledge base:
```python
from kb.kimi_integration import KBClient
kb = KBClient(kb_dir='~/Projects/knowledge-base')

# Get analytics
report = kb.analytics(days=7)
if report["success"]:
    health = report["data"]["health"]
    score = health["health_score"]
    issues = health["integrity"]["total_issues"]

    if score < 80 or issues > 5:
        # Send alert
        message = f"⚠️ KB Health Alert\n\n"
        message += f"Health Score: {score}/100\n"
        message += f"Integrity Issues: {issues}\n"
        if issues > 0:
            message += f"  Errors: {health['integrity']['errors']}\n"
            message += f"  Warnings: {health['integrity']['warnings']}\n"

        # Auto-fix if possible
        if health['integrity']['fixable'] > 0:
            kb.auto_fix()
            message += f"\n✓ Auto-fixed {health['integrity']['fixable']} issues"

        # Send to Telegram
        print(message)
```
```

### 2. Automatic Ingestion Monitor

Watch for new files in a directory and auto-ingest:

```python
import os
from pathlib import Path
from kb.kimi_integration import KBClient

watch_dir = Path("~/Documents/to-ingest").expanduser()
kb = KBClient()

for file in watch_dir.glob("*.md"):
    result = kb.ingest_file(str(file), classification="internal")
    if result["success"]:
        # Move to processed
        file.rename(watch_dir / "processed" / file.name)
```

## Troubleshooting

**"Command not found" errors:**
- Make sure you're running from the kb directory: `cd ~/Projects/knowledge-base`
- Or use full path: `python3 -m kb.cli` instead of `kb`

**"No API key" errors:**
- LLM features (compile, ask) require `ANTHROPIC_API_KEY` environment variable
- Search, ingest, analytics work without API key

**JSON parsing errors:**
- Some commands output text, not JSON
- Use `format="text"` or `output="text"` for those commands
- Or check `result["success"]` and read `result["output"]` directly

## Next Steps

1. Add KB integration to relevant agents' CLAUDE.md files
2. Test with simple searches: `kb.search("test")`
3. Set up daily health check in Gamma's heartbeat
4. Configure automatic ingestion for documentation directories
