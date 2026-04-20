# HalOS

Always-on AI daemon. Multiple named agents, each with their own Telegram bot and Claude Code session. Interfaces: Telegram (per-agent bots) and a terminal TUI.

Run with: `cd ~/Projects/halos && python -m halos`
Config at: `config/config.yaml`
DB + memory at: `~/.halos/`
PID file: `~/.halos/daemon.pid`

---

## Architecture

```
Telegram (per-agent bots)
    └── AgentBot → ClaudeCodeEngine → claude -p (subprocess)
                                          └── reads CLAUDE.md from project_dir
                                          └── --resume <session_id> (conversation continuity)
                                          └── --append-system-prompt (soul.md + KB memory + SQLite)

Terminal TUI
    └── SessionProcess → ClaudeCodeEngine (same engine, same DB sessions)

Scheduler (APScheduler)
    └── TaskScheduler → tasks.yaml tasks → NotifierAgentBot
```

**Key modules:**

| File | Role |
|------|------|
| `main.py` | Daemon entry point — wires everything together |
| `agent_bot.py` | Per-agent Telegram bot (one per agent in config.agents) |
| `claude_code.py` | Spawns `claude -p` subprocesses, builds system prompts, manages session IDs |
| `skill_evolution.py` | Self-evolving skill layer — L3 crystallization + L1 insight index + semantic search |
| `memory.py` | SQLite keyword search (short-term conversation context) |
| `db.py` | SQLite — conversations, sessions, scheduled tasks, structured memory, agent_messages |
| `scheduler.py` | APScheduler cron runner — loads from `config/tasks.yaml` at startup |
| `notifier.py` | Proactive push notifications — routes through each agent's own bot |
| `tui.py` | Textual terminal dashboard — sessions sidebar, output pane, cron management |
| `telegram_bot.py` | Main Hal bot (optional, for general routing) |
| `router.py` | Routes Hal bot messages to Claude Code or Claude API |

---

## Agent Formula

Every agent lives in `sessions/{name}/` and uses 2 files:

```
sessions/{name}/
├── CLAUDE.md      — Operating instructions + startup/shutdown/idle checklists
└── soul.md        — Identity and personality (injected via --append-system-prompt)
```

Persistent memory lives in the shared KB (`~/Projects/knowledge-base/.kb/kb.db`,
table `agent_memory`) and is injected into the system prompt on every call.

### CLAUDE.md
Operating instructions — mode, domain, conventions, Telegram bot credentials, multi-session routing, Memory section, Startup/Idle/Shutdown checklists. Includes a `### Core Files — Read These First` section pointing to soul.md. No personality here; that lives in soul.md.

### soul.md
Who the agent IS: identity, voice, values, relationship to the user. This is loaded as `personality_override` by the daemon and passed to `ClaudeCodeEngine.invoke_streaming()`. Not instructions — character.

### Memory (KB agent_memory table)
Persistent memory is stored in the shared KB, keyed by agent name, with 10 canonical sections:
`short_term`, `long_term`, `identity`, `family`, `upcoming`, `preferences`,
`fitness_log`, `learned`, `decisions`, `open_questions`.

On every call, the daemon runs `kb memory render --agent <name> --max-chars 4000` and
appends the result to the system prompt via `--append-system-prompt`. Agents read and
write their own memory using the `kb memory` CLI.

**Two tiers:**
- `short_term` — rolling 24-hour window. Agent updates at the END of every session.
  Promoted or cleared at the start of each new day.
- Long-term sections — persistent forever. Updated only when the user says "remember this".

### Recurring tasks (config/tasks.yaml)
All scheduled agent work lives in `config/tasks.yaml` as structured entries
(`name`, `type`, `session`, `cron`, `description`, `prompt`, `notify`). Tasks with
`session: <agent>` run in that agent's Claude Code session with their soul + KB
memory loaded.

---

## System Prompt Construction

`ClaudeCodeEngine._build_system_prompt()` assembles in this order:

1. **Identity layer** — `personality_override` → `soul.md` → global `_personality` (first wins)
2. **Current datetime**
3. **Agent memory (KB)** — `kb memory render --agent <name> --max-chars 4000` output
4. **Relevant Skills** — semantic similarity search against evolved skills (new!)
5. **Short-term context** — keyword-relevant SQLite entries for the current message
6. **Recent conversation** — last 10 messages from DB

Passed to `claude` via `--append-system-prompt`. The CLAUDE.md in the project dir is read automatically by the Claude Code CLI.

---

## Session Continuity

Sessions are persisted in SQLite (`sessions` table). The real Claude-assigned session ID is captured from the `system:init` event and stored on first turn. Subsequent calls use `--resume <session_id>` to continue the same conversation.

**Important:** `--resume` only works if the stored `project_dir` matches the current one. The engine detects mismatches, logs a warning, clears the stale session_id, and starts fresh.

TUI sessions and Telegram messages share the same session — both use the same `project_name` key so they resume into the same Claude conversation.

---

## Self-Evolving Skills (New)

HalOS now includes a skill crystallization layer inspired by GenericAgent:

```
[Novel Task] → [Autonomous exploration] → [Crystallize into Skill] → [Write to Memory] → [Direct reuse next time]
```

- **Automatic crystallization:** After any successful multi-tool turn, a background task distills the execution path into a reusable skill
- **Semantic retrieval:** Skills are stored with 384-dim sentence-transformer embeddings. Incoming messages are matched via cosine similarity
- **Skill injection:** Relevant skills are injected into the system prompt before short-term memory
- **Commands:** `/skills` to list, `/forget_skill <name>` to delete

See `halos/skill_evolution.py` for implementation.

---

## Scheduling

Tasks are loaded from `config/tasks.yaml` at startup (single source of truth).

Task types: `custom` (LLM prompt), `health_check`, `code_watch`, `market_check`, `digest`.

Custom tasks with `session: agent_name` run in that agent's session with their personality loaded, and route notifications through the agent's own Telegram bot.

Manage at runtime via Telegram: `/tasks`, `/pause {name}`, `/resume {name}`, `/add {name} "{cron}" {description}`.

---

## Memory Loop

```
INJECT (every call)
  ├── soul.md (identity)
  ├── kb memory render (agent's full KB memory)
  ├── Relevant Skills (self-evolved SOPs)
  └── SQLite keyword search (relevant structured conversation entries)

WRITE (agent → KB)
  └── kb memory add/update/delete/promote/clear-short-term --agent <name> ...

SESSION END (agent runs shutdown checklist)
  └── Agent writes session summary into short_term

DAY START (agent runs startup checklist)
  └── Agent promotes short_term → appropriate long-term section, clears the rest

CLEAR (/clear)
  └── Kills Claude session → next message starts fresh (KB memory + skills survive)
```

---

## Config Reference

```yaml
agents:
  {name}:
    bot_token: "..."           # Telegram bot token for this agent
    project_dir: "..."         # Path to sessions/{name}/ dir

claude_code:
  binary_path: claude          # Claude Code CLI binary
  default_model: sonnet        # Model for agent messages
  skip_permissions: true       # --dangerously-skip-permissions
  timeout_seconds: 300
  projects:                    # TUI session → project dir mapping
    {name}: ~/Projects/...

scheduler:
  timezone: America/New_York

notifications:
  telegram_chat_id: YOUR_CHAT_ID

telegram:
  allowed_user_ids: [YOUR_CHAT_ID]
  max_message_length: 4000
```

---

## Adding a New Agent

1. Create `sessions/{name}/` with CLAUDE.md and soul.md
2. Add to `config.yaml` under `agents:` with bot_token and project_dir
3. Add to `config.yaml` under `claude_code.projects:`
4. Seed memory: `kb memory add --agent <name> --section identity --content "..."`
5. Add recurring tasks to `config/tasks.yaml` with `session: <name>`
6. Restart the daemon

---

## Agent Messaging

Agents can send messages to each other with near-zero latency when idle.

### From the TUI
```
@Beta hey can you draft a tweet about the milestone
@Alpha what's on my calendar tomorrow?
```

### From a Claude subprocess (AI-initiated)
```bash
halos-msg Beta "Alpha here — can you draft a tweet?"
```

---

## TUI Key Bindings

| Key | Action |
|-----|--------|
| `↑/↓` | Navigate sessions/cron sidebar |
| `Enter` | Switch to session |
| `e` | Edit session files (soul.md, CLAUDE.md) |
| `n` | New session |
| `k` | Kill active session |
| `ctrl+r` | Restart daemon |
| `?` | Help |
| `ctrl+c` | Quit |

**TUI commands:**

| Command | Action |
|---------|--------|
| `/model <name>` | Switch active session model |
| `/reload` | Reload TUI |
| `/restart` | Restart daemon |
| `@AgentName <msg>` | Send a message to another agent's session |
