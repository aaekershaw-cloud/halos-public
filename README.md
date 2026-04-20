# HalOS + Knowledge Base

> An always-on AI daemon that runs multiple self-evolving Claude Code agents — each with their own Telegram bot, memory system, and skill tree.

HalOS is a multi-agent orchestration framework built around Claude Code. It gives you persistent, stateful agents that remember, schedule, message each other, and **evolve their own skills** over time.

This repository contains two tightly-coupled projects:

- **`daemon/`** — HalOS: the orchestration engine, scheduler, TUI, and Telegram bots
- **`knowledge-base/`** — The persistent memory backend (SQLite + FTS + embeddings) that agents read and write

---

## What It Does

```
Telegram (per-agent bots)
    └── AgentBot → ClaudeCodeEngine → claude -p (subprocess)
                                          └── reads CLAUDE.md from project_dir
                                          └── --resume <session_id>
                                          └── --append-system-prompt (soul + KB memory + skills)

Terminal TUI
    └── SessionProcess → ClaudeCodeEngine (same engine, same DB sessions)

Scheduler (APScheduler)
    └── TaskScheduler → tasks.yaml tasks → NotifierAgentBot
```

**Core idea:** Instead of one chatbot, you run a fleet of specialized agents. Each agent has:
- A **dedicated Telegram bot** (message Alpha for calendar stuff, Gamma for code, Beta for social media)
- A **persistent Claude Code session** with `--resume` continuity
- A **personality** (`soul.md`) and **operating manual** (`CLAUDE.md`)
- **Shared long-term memory** via the Knowledge Base
- **Self-evolving skills** that crystallize from successful multi-tool turns

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/halos.git
cd halos
```

### 2. Install dependencies

```bash
# HalOS daemon
cd daemon
pip install -r requirements.txt

# Knowledge Base
cd ../knowledge-base
pip install -r requirements.txt
# Optional but recommended for semantic skill search:
pip install sentence-transformers
```

### 3. Configure

```bash
cd daemon
# Copy and edit the example config
cp config/config.yaml.example config/config.yaml
# Fill in your Telegram bot tokens, API keys, and allowed user IDs

# Copy and edit scheduled tasks
cp config/tasks.yaml.example config/tasks.yaml
# Customize to your needs
```

You'll need:
- **Telegram Bot Tokens** — one per agent, plus an optional main bot ([BotFather](https://t.me/botfather))
- **OpenRouter API key** (or Anthropic) — for fallback API calls and skill crystallization
- **Claude Code CLI** installed and authenticated

### 4. Create your first agent

```bash
# Copy the template
mkdir -p sessions/myagent
cp sessions/template/CLAUDE.md sessions/myagent/CLAUDE.md
cp sessions/template/soul.md sessions/myagent/soul.md

# Edit both files to define your agent's personality and operating procedures
# Then add the agent to config/config.yaml
```

### 5. Run

```bash
cd daemon
python -m halos
```

Or with the TUI:

```bash
python -m halos tui
```

---

## Architecture

### HalOS Daemon (`daemon/`)

| Module | Purpose |
|--------|---------|
| `halos/main.py` | Entry point — wires bots, scheduler, DB, engines |
| `halos/claude_code.py` | Spawns `claude -p`, builds system prompts, manages session IDs |
| `halos/kimi_code.py` | Same for Kimi CLI (alternative engine) |
| `halos/agent_bot.py` | Per-agent Telegram bot with commands and message handling |
| `halos/scheduler.py` | APScheduler cron runner — loads `tasks.yaml` |
| `halos/skill_evolution.py` | **Self-evolving skill layer** — crystallization + semantic search |
| `halos/tui.py` | Textual terminal dashboard |
| `halos/db.py` | SQLite — conversations, sessions, skills, archives |
| `halos/memory.py` | Short-term structured memory (keyword search) |
| `halos/router.py` | Routes main bot messages to Claude Code or Claude API |

### Knowledge Base (`knowledge-base/`)

| Module | Purpose |
|--------|---------|
| `kb/memory.py` | `MemoryStore` — CRUD for per-agent memory sections |
| `kb/embeddings.py` | Sentence-transformer embeddings for semantic search |
| `kb/consolidation.py` | Compresses short-term memory into episodic summaries |
| `kb/hooks/session_hooks.py` | `on_session_start` / `on_session_end` — called by HalOS |
| `kb/cli.py` | `kb memory` CLI subcommands |

---

## Self-Evolving Skills

HalOS agents automatically crystallize successful workflows into reusable **skills**.

```
[Novel multi-tool task] → [Background LLM distillation] → [Skill stored with embedding]
                                                              │
                                                              ▼
                                                    [Next similar request]
                                                              │
                                                              ▼
                                                    [Semantic similarity match]
                                                              │
                                                              ▼
                                                    [Skill injected into system prompt]
                                                              │
                                                              ▼
                                                    [Agent runs SOP directly — no exploration]
```

- **Automatic:** Fires after every successful turn with ≥2 tools
- **Semantic retrieval:** Uses `all-MiniLM-L6-v2` embeddings (384-dim) + cosine similarity
- **Commands:** `/skills` to list, `/forget_skill <name>` to delete
- **Fallback:** Keyword overlap on trigger phrases if embeddings aren't available

See [`daemon/halos/skill_evolution.py`](daemon/halos/skill_evolution.py) for the full implementation.

---

## System Prompt Stack

On every turn, HalOS builds the system prompt in this order:

1. **Identity** — `soul.md` (who the agent IS)
2. **Datetime** — current time
3. **KB Memory** — `kb memory render --agent <name>` output
4. **Relevant Skills** — semantic similarity against evolved skills
5. **Short-term Memory** — keyword-relevant SQLite entries
6. **Recent Conversation** — last 10 messages
7. **Telegram capabilities** — if source is Telegram

This keeps context tight (<30K tokens typical) while ensuring the right knowledge is always in scope.

---

## Agent Messaging

Agents can message each other:

**From the TUI:**
```
@Beta draft a tweet about the project launch
@Alpha what's on my calendar tomorrow?
```

**From a Claude subprocess (AI-initiated):**
```bash
halos-msg Beta "Alpha here — can you draft a tweet?"
```

Messages are routed via SQLite queue with <2s delivery latency.

---

## Adding a New Agent

1. Create `sessions/{name}/` with `CLAUDE.md` and `soul.md`
2. Add to `config/config.yaml` under `agents:` with `bot_token` and `project_dir`
3. Add to `config/config.yaml` under `claude_code.projects:`
4. Seed memory: `kb memory add --agent <name> --section identity --content "..."`
5. Add recurring tasks to `config/tasks.yaml` with `session: <name>`
6. Restart the daemon

---

## Directory Layout

```
.
├── daemon/                          # HalOS orchestration engine
│   ├── halos/                       # Core Python modules
│   │   ├── skill_evolution.py       # Self-evolving skill layer
│   │   ├── claude_code.py           # Claude Code subprocess engine
│   │   ├── kimi_code.py             # Kimi CLI engine
│   │   ├── agent_bot.py             # Per-agent Telegram bots
│   │   ├── scheduler.py             # Cron task scheduler
│   │   ├── tui.py                   # Terminal dashboard
│   │   ├── db.py                    # SQLite schema + helpers
│   │   └── ...
│   ├── config/
│   │   ├── config.yaml.example      # Main daemon config template
│   │   ├── tasks.yaml.example       # Scheduled tasks template
│   │   ├── personality.md           # Global personality fallback
│   │   └── memory.md                # Runtime memory notes
│   ├── sessions/
│   │   └── template/                # CLAUDE.md + soul.md templates
│   ├── requirements.txt
│   └── README.md
│
├── knowledge-base/                  # Persistent memory backend
│   ├── kb/
│   │   ├── memory.py                # Agent memory CRUD + rendering
│   │   ├── embeddings.py            # Semantic embeddings (sentence-transformers)
│   │   ├── consolidation.py         # Session compression
│   │   ├── hooks/session_hooks.py   # HalOS lifecycle hooks
│   │   └── commands/memory.py       # `kb memory` CLI
│   ├── tests/
│   ├── wiki/                        # Knowledge articles (public)
│   └── README.md
│
└── README.md                        # You are here
```

---

## Requirements

- Python 3.11+
- macOS or Linux (developed on macOS)
- [Claude Code CLI](https://github.com/anthropropics/anthropic-cookbook/tree/main/skills/claude-code) installed
- Telegram Bot(s) via [BotFather](https://t.me/botfather)
- OpenRouter or Anthropic API key (for fallback + skill crystallization)
- Optional: [Kimi CLI](https://www.moonshot.cn/) for alternative engine
- Optional: `sentence-transformers` for semantic skill search

---

## Inspiration

HalOS draws ideas from several projects:

- **GenericAgent** — The self-evolving skill crystallization concept
- **Claude Code** — The underlying CLI engine that actually does the work
- **OpenClaw / OpenManus** — Multi-agent orchestration patterns

We didn't adopt GenericAgent's code (~3K lines) because HalOS is architecturally different: it's built around Claude Code's rich toolset and multi-agent Telegram interface rather than a minimal atomic-tool loop. But the **L3 skill crystallization** idea is directly ported and adapted.

---

## License

MIT — see [LICENSE](LICENSE)
