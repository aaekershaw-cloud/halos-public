# Running KB Maintenance from Kimi

The knowledge base maintenance agent can be run directly from kimi CLI.

## Quick Usage

### From kimi shell:
```bash
python3 ~/Projects/knowledge-base/kb-maint
```

### With JSON output:
```bash
python3 ~/Projects/knowledge-base/kb-maint --json
```

### Skip auto-fix:
```bash
python3 ~/Projects/knowledge-base/kb-maint --no-auto-fix
```

## What It Does

The maintenance agent runs:
1. ✓ Health check (overall system health score)
2. ✓ Integrity checks (orphaned files, broken links, etc.)
3. ✓ Auto-fix (if enabled, fixes deterministic issues)
4. ✓ PII scan (checks for sensitive data)
5. ✓ Cost tracking (LLM API usage)
6. ✓ Worker status (background automation)

## Example Output

```
🤖 Knowledge Base Maintenance Agent

============================================================
🏥 Checking system health...
  Health Score: 80/100

🔍 Running integrity checks...
  Total Issues: 3

🔧 Auto-fixing 3 issue(s)...
  ✓ Fixed 3 issue(s)

🔒 Scanning for PII...
  Articles with PII: 0

💰 Checking costs (7 days)...
  Total: $0.1545

⚙️  Checking worker...
  Running: False
  ⚠️  Worker not running - automation disabled

============================================================
MAINTENANCE SUMMARY
============================================================

Timestamp: 2026-04-04T10:36:32.802641
Total Tasks: 6
Successful: 6
Failed: 0
Warnings: 1

⚠️  WARNINGS DETECTED - Review recommended

============================================================
```

## Using from Python (kimi agents)

```python
import sys
sys.path.insert(0, '~/Projects/knowledge-base')

from kb.maintenance_agent import MaintenanceAgent

agent = MaintenanceAgent()
results = agent.run_full_maintenance(auto_fix=True)

print(f"Health Score: {results['summary']['successful']}/{results['summary']['total_tasks']}")
print(f"Warnings: {results['summary']['warnings']}")
```

## Add to HalOS Agent

### Add to Gamma's heartbeat.md:

```markdown
## Recurring

Every morning at 8am, run knowledge base maintenance and report if issues found.

## Daily KB Maintenance

```bash
python3 ~/Projects/knowledge-base/kb-maint
```

If warnings detected, notify the user.
```

### Or add as a Python task in soul.md:

```python
from kb.maintenance_agent import MaintenanceAgent

agent = MaintenanceAgent()
results = agent.run_full_maintenance(auto_fix=True)

if results['summary']['warnings'] > 0:
    print(f"⚠️  KB Health Alert: {results['summary']['warnings']} warnings")
    print("Run: kb lint --fix")
```

## Manual Commands

If you need to run specific tasks:

```bash
# Just check health
cd ~/Projects/knowledge-base && python3 -m kb.cli analytics

# Run integrity checks
cd ~/Projects/knowledge-base && python3 -m kb.cli lint

# Auto-fix issues
cd ~/Projects/knowledge-base && python3 -m kb.cli lint --fix

# Scan for PII
cd ~/Projects/knowledge-base && python3 -m kb.cli pii scan-all

# Check worker status
cd ~/Projects/knowledge-base && python3 -m kb.cli worker status

# Start worker
cd ~/Projects/knowledge-base && python3 -m kb.cli worker start --daemon
```

## Return Codes

- `0` - All tasks successful
- `1` - One or more tasks failed

Use in scripts:
```bash
if python3 ~/Projects/knowledge-base/kb-maint; then
    echo "Maintenance successful"
else
    echo "Maintenance failed - manual intervention required"
fi
```
