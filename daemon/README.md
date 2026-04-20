# HalOS

An always-on AI daemon that manages multiple concurrent Claude Code sessions via Telegram. Message Hal from your phone, and he routes your request to the right project, spins up Claude Code sessions, runs scheduled tasks, and keeps you posted.

## Architecture

```
Telegram <-> TelegramBot <-> Router <-> Claude API (chat)
                                    \-> Claude Code (dev sessions)
                              Scheduler -> Notifier -> Telegram
```

**Core modules:**

- `telegram_bot.py` — Telegram interface with typing indicators, message chunking
- `router.py` — Routes messages to Claude API (general chat) or Claude Code (dev work)
- `claude_api.py` — OpenRouter-compatible chat completions with conversation memory
- `claude_code.py` — Spawns and manages Claude Code subprocesses per project
- `scheduler.py` — APScheduler-based task runner (cron + interval)
- `notifier.py` — Sends proactive Telegram notifications with quiet hours
- `memory.py` — Persistent memory manager backed by markdown + SQLite
- `db.py` — SQLite database for conversations, sessions, and task history

**Scheduled tasks** (`halos/tasks/`):

- `digest.py` — Daily project summary
- `health_check.py` — System health monitoring
- `code_watch.py` — Git activity watcher
- `market_check.py` — Market data alerts

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export OPENROUTER_API_KEY="your-key"
export TELEGRAM_BOT_TOKEN="your-token"

# Configure
# Edit config/config.yaml with your Telegram user ID and project paths

# Run (use the absolute interpreter path for consistency with launchd/cron)
/usr/bin/python3 -m halos
```

## Configuration

All config lives in `config/`:

- `config.yaml` — Daemon settings, API config, project map, scheduler, notifications
- `personality.md` — Hal's personality and system prompt
- `tasks.yaml` — Scheduled task definitions

## Project Map

Hal knows your projects by name. Message "myproject: run the tests" and he'll open a Claude Code session in the right directory. Aliases are configurable per project.

## Requirements

- Python 3.11+
- Claude Code CLI installed
- Telegram Bot (via BotFather)
- OpenRouter or Anthropic API key
