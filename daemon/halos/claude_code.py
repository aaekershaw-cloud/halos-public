"""Claude Code engine with NDJSON streaming support for HalOS."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from .config import Config
from .db import Database

logger = logging.getLogger(__name__)

# Location of the knowledge-base project. Memory for each agent is stored in
# the KB's agent_memory table and rendered into the system prompt via the
# `kb memory render` CLI subcommand. Overridable via HALOS_KB_DIR env var so
# tests can point to a fixture KB.
_KB_DIR = os.environ.get(
    "HALOS_KB_DIR",
    str(Path.home() / "Projects" / "knowledge-base"),
)

# Add KB to Python path and import session hooks
if _KB_DIR not in sys.path:
    sys.path.insert(0, _KB_DIR)

try:
    from kb.hooks.session_hooks import on_session_start, on_session_end
    KB_HOOKS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"KB session hooks unavailable: {e}")
    KB_HOOKS_AVAILABLE = False


@dataclass
class StreamEvent:
    type: str           # "init", "text", "tool_use", "result", "error"
    text: Optional[str] = None
    tool_name: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    session_id: Optional[str] = None
    is_error: bool = False
    error_text: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class StreamResult:
    text: str
    cost_usd: float
    duration_ms: int
    num_turns: int
    model: str
    session_id: str
    tool_calls: list[str]
    error: bool = False


TOOL_LABELS = {
    "Read": "Reading file...",
    "Bash": "Running command...",
    "Grep": "Searching code...",
    "Edit": "Editing file...",
    "Write": "Writing file...",
    "WebSearch": "Searching web...",
    "WebFetch": "Fetching page...",
    "Glob": "Finding files...",
    "Agent": "Spawning subagent...",
    "TodoWrite": "Updating todos...",
    "TodoRead": "Reading todos...",
    "NotebookEdit": "Editing notebook...",
}


class ClaudeCodeEngine:
    def __init__(self, config: Config, db: Database, memory=None, skill_evolver=None):
        self.config = config
        self.db = db
        self.memory = memory
        self.skill_evolver = skill_evolver
        self.binary = config.claude_code.binary_path
        self.skip_permissions = config.claude_code.skip_permissions
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._personality: str = ""

        self.project_map = dict(config.claude_code.projects) if config.claude_code.projects else {}
        self.aliases = dict(config.claude_code.aliases) if config.claude_code.aliases else {}

    def load_personality(self, path: str):
        p = Path(path)
        if p.exists():
            self._personality = p.read_text().strip()

    # --- Project resolution (kept from ClaudeCodeBridge) ---

    def resolve_project(self, name: str) -> tuple:
        """Resolve a project name/alias to (canonical_name, directory) or (None, None)."""
        name = name.lower().strip()
        canonical = self.aliases.get(name, name)
        if canonical in self.project_map:
            return canonical, str(Path(self.project_map[canonical]).expanduser())
        return None, None

    def get_project_names(self) -> list[str]:
        return list(self.project_map.keys()) + list(self.aliases.keys())

    def session_id_for(self, project_name: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"halos.{project_name}"))

    def _session_id_for_general(self) -> str:
        today = date.today().isoformat()
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"halos.general.{today}"))

    def _get_lock(self, project_name: str) -> asyncio.Lock:
        if project_name not in self._session_locks:
            import traceback
            caller_stack = ''.join(traceback.format_stack()[-4:-1])
            self._session_locks[project_name] = asyncio.Lock()
            logger.info(f"ClaudeCodeEngine: created new lock for {project_name}, caller:\n{caller_stack}")
        else:
            logger.debug(f"ClaudeCodeEngine: returning existing lock for {project_name} (locked={self._session_locks[project_name].locked()})")
        return self._session_locks[project_name]

    # --- Remote execution helpers ---

    def _get_remote_config(self, project_name: str) -> tuple[bool, str]:
        """Return (is_remote, remote_project_dir) for a project_name.

        Matches against configured agents by both telegram-style name
        (telegram:claude:{agent}) and bare name (used by scheduler tasks).
        Case-insensitive match for agent names since config.yaml uses
        mixed casing (Beta, Alpha, gamma) but callers may not.
        """
        project_lower = project_name.lower()
        for agent_name, agent_cfg in self.config.agents.items():
            if not agent_cfg.remote:
                continue
            agent_lower = agent_name.lower()
            if project_lower == agent_lower or project_lower == f"telegram:claude:{agent_lower}":
                return True, agent_cfg.remote_project_dir
        return False, ""

    def _build_remote_cmd(self, local_cmd: list[str], remote_binary: str, remote_cwd: str = "") -> list[str]:
        """Wrap a claude command for SSH remote execution.

        Uses shlex.join() for POSIX shell escaping (handles the one extra shell
        layer introduced by SSH). Uses -T to disable TTY allocation so progress
        bars / interactive prompts cannot corrupt the NDJSON stream.
        """
        host = self.config.claude_code.remote_host
        if not host:
            raise ValueError("remote_host not configured but remote execution requested")

        # Replace local binary (cmd[0]) with the remote binary path.
        remote_args = [remote_binary] + local_cmd[1:]
        remote_shell_cmd = shlex.join(remote_args)

        # Prepend PATH setup and cd into the remote project dir.
        prefix = "export PATH=/opt/homebrew/bin:$PATH"
        if remote_cwd:
            prefix += f" && cd {shlex.quote(remote_cwd)}"
        remote_shell_cmd = f"{prefix} && {remote_shell_cmd}"

        return [
            "ssh", "-T",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            host, remote_shell_cmd,
        ]

    # --- System prompt ---

    @staticmethod
    def _agent_key_for(project_name: str, project_dir: str) -> str:
        """Derive the KB agent key from project_dir basename or project_name.

        Agent keys in the KB memory table are lowercase (beta, alpha,
        gamma). Session dir basenames already match, so prefer that.
        """
        if project_dir:
            basename = Path(project_dir).expanduser().name
            if basename:
                return basename.strip().lower()
        return (project_name or "").strip().lower()

    async def _load_kb_memory(self, agent_key: str, max_chars: int = 4000) -> str:
        """Render an agent's KB memory as Markdown for prompt injection.

        Shells out to `python3 -m kb.cli memory render` so halos remains
        decoupled from the KB Python package. Returns a safe placeholder
        when the lookup fails, so the turn never crashes on a KB outage.
        """
        placeholder = "## Memory\n(no entries yet)"
        if not agent_key:
            return placeholder
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-m", "kb.cli", "memory", "render",
                "--agent", agent_key, "--max-chars", str(max_chars),
                cwd=_KB_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            logger.warning(f"KB memory load failed for {agent_key}: {e}")
            return placeholder
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"KB memory unexpected error for {agent_key}: {e}")
            return placeholder

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[:200].strip()
            logger.warning(
                f"KB memory render non-zero exit for {agent_key}: {err_text}"
            )
            return placeholder

        rendered = stdout.decode("utf-8", errors="replace").strip()
        if not rendered:
            return placeholder
        if max_chars and len(rendered) > max_chars:
            rendered = rendered[:max_chars].rstrip() + "\n...[truncated]"
        return rendered

    async def _build_system_prompt(self, message: str = "", project_name: str = "",
                                    project_dir: str = "", personality_override: str = "",
                                    source: str = "", session_id: str = "") -> str:
        """Assemble soul + KB memory + short-term memory + recent history.

        Layer order:
          1. Identity/personality — personality_override > soul.md > global _personality
          2. Current datetime
          3. Agent memory — loaded from KB session hooks (on_session_start)
             Falls back to legacy `kb memory render` if hooks unavailable.
          4. Short-term structured memory — keyword-relevant SQLite entries
          5. Recent conversation history
        """
        parts = []

        # 1. Personality / identity layer
        if personality_override:
            logger.info(f"ClaudeCodeEngine [{project_name}]: using personality_override (first 100 chars): {personality_override[:100]}")
            parts.append(personality_override)
        elif project_dir:
            soul_path = Path(project_dir) / "soul.md"
            if soul_path.exists():
                parts.append(soul_path.read_text().strip())
            elif self._personality:
                parts.append(self._personality)
        elif self._personality:
            parts.append(self._personality)

        # 2. Current datetime
        now = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        parts.append(f"Current datetime: {now}")

        # 3. Agent memory from KB (via session hooks or legacy render)
        agent_key = self._agent_key_for(project_name, project_dir)
        if agent_key:
            # Try new session hooks first (loads articles + memory)
            if KB_HOOKS_AVAILABLE:
                try:
                    result = on_session_start(
                        session_id=session_id or "",
                        agent_name=agent_key,
                        project_dir=project_dir or "",
                    )
                    kb_context = result.get("markdown", "")
                    if kb_context:
                        parts.append(kb_context)
                        logger.debug(f"Loaded KB context via session hooks for {agent_key}: {result.get('article_count', 0)} articles")
                    else:
                        # Fallback to legacy method if hooks return empty
                        memory_block = await self._load_kb_memory(agent_key, max_chars=4000)
                        if memory_block:
                            parts.append(memory_block)
                except Exception as e:
                    logger.warning(f"KB session hooks failed for {agent_key}: {e}, falling back to legacy render")
                    memory_block = await self._load_kb_memory(agent_key, max_chars=4000)
                    if memory_block:
                        parts.append(memory_block)
            else:
                # Hooks not available, use legacy method
                memory_block = await self._load_kb_memory(agent_key, max_chars=4000)
                if memory_block:
                    parts.append(memory_block)

        # 3b. Relevant skills (L3 — self-evolved SOPs)
        if self.skill_evolver and agent_key and message:
            try:
                matched = await self.skill_evolver.find_skills(agent_key, message, limit=2)
                if matched:
                    skills_md = await self.skill_evolver.render_skills_for_prompt(matched)
                    if skills_md:
                        parts.append(skills_md)
                        logger.debug(f"Injected {len(matched)} skill(s) for {agent_key}")
            except Exception as e:
                logger.warning(f"Skill injection failed for {agent_key}: {e}")

        # 4. Short-term structured memory (keyword-relevant SQLite entries)
        if self.db and message:
            keywords = [w for w in message.lower().split() if len(w) > 3][:5]
            if keywords:
                seen: set = set()
                structured: list = []
                for kw in keywords:
                    results = await self.db.search_memory(kw)
                    for r in results:
                        mem_key = (r["category"], r["key"])
                        if mem_key not in seen:
                            seen.add(mem_key)
                            structured.append(f"[{r['category']}] {r['key']}: {r['value']}")
                if structured:
                    parts.append("## Relevant Memory\n" + "\n".join(structured[:20]))

        # 5. Recent conversation history
        if self.db:
            recent = await self.db.get_recent_messages(source=source, limit=10)
            if recent:
                history_lines = []
                for msg in recent:
                    role = msg["role"].upper()
                    content = msg["content"][:300]
                    history_lines.append(f"{role}: {content}")
                parts.append("## Recent conversation\n" + "\n".join(history_lines))

        # 6. Telegram-specific capabilities
        if source.startswith("telegram:"):
            parts.append(
                "## Telegram Messaging\n"
                "You are chatting via Telegram. You can send proactive messages to other agents "
                "by running: python -m halos.msg <agent_name> \"<message>\""
            )

        return "\n\n".join(parts)

    # --- Stream parsing ---

    async def _parse_stream(self, stdout: asyncio.StreamReader) -> AsyncIterator[StreamEvent]:
        """Parse NDJSON from stdout, yielding StreamEvents."""
        async for raw_line in stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON stdout: {line[:100]}")
                continue

            event_type = event.get("type", "")

            if event_type == "system" and event.get("subtype") == "init":
                yield StreamEvent(
                    type="init",
                    session_id=event.get("session_id"),
                    raw=event,
                )

            elif event_type == "assistant":
                msg = event.get("message", {})
                content = msg.get("content", [])
                for block in content:
                    block_type = block.get("type", "")
                    if block_type == "text":
                        yield StreamEvent(
                            type="text",
                            text=block.get("text", ""),
                            session_id=event.get("session_id"),
                            raw=event,
                        )
                    elif block_type == "tool_use":
                        yield StreamEvent(
                            type="tool_use",
                            tool_name=block.get("name"),
                            session_id=event.get("session_id"),
                            raw=event,
                        )

            elif event_type == "result":
                cost = event.get("total_cost_usd") or event.get("cost_usd") or 0.0
                duration = event.get("duration_ms") or 0
                is_err = bool(event.get("is_error"))
                errors_list = event.get("errors") or []
                err_text = "; ".join(str(e) for e in errors_list) if errors_list else ""
                yield StreamEvent(
                    type="result",
                    cost_usd=float(cost),
                    duration_ms=int(duration),
                    session_id=event.get("session_id"),
                    is_error=is_err,
                    error_text=err_text,
                    raw=event,
                )

    # --- Main invoke methods ---

    async def invoke_streaming(
        self,
        instruction: str,
        project_name: str,
        project_dir: str,
        model: str = "sonnet",
        on_progress: Optional[Callable] = None,
        session_id: Optional[str] = None,
        disallowed_tools: Optional[str] = None,
        personality_override: str = "",
        source: str = "",
        timeout: Optional[int] = None,
    ) -> StreamResult:
        """Main entry point. Serialized per project.
        
        Args:
            timeout: Override default timeout in seconds. None = no timeout.
        """
        lock = self._get_lock(project_name)
        import traceback
        caller_stack = ''.join(traceback.format_stack()[-4:-1])
        logger.info(f"ClaudeCodeEngine [{project_name}]: acquiring lock... (locked={lock.locked()}) called from:\n{caller_stack}")
        try:
            logger.info(f"ClaudeCodeEngine [{project_name}]: BEFORE await lock.acquire()")
            await asyncio.wait_for(lock.acquire(), timeout=120.0)
            logger.info(f"ClaudeCodeEngine [{project_name}]: AFTER lock.acquire() - lock acquired! (locked={lock.locked()})")
        except asyncio.TimeoutError:
            all_locks = {k: v.locked() for k, v in self._session_locks.items()}
            logger.error(f"ClaudeCodeEngine [{project_name}]: TIMEOUT acquiring lock after 5s! Lock state: locked={lock.locked()}, all locks: {all_locks}")
            raise RuntimeError(f"Lock acquisition timeout for {project_name}")

        try:
            logger.info(f"ClaudeCodeEngine [{project_name}]: calling _invoke_streaming_locked")
            result = await self._invoke_streaming_locked(
                instruction, project_name, project_dir,
                model=model, on_progress=on_progress,
                session_id=session_id, disallowed_tools=disallowed_tools,
                personality_override=personality_override,
                source=source, timeout=timeout,
            )
            logger.info(f"ClaudeCodeEngine [{project_name}]: _invoke_streaming_locked returned")
            # If a previous turn was interrupted or the session became invalid,
            # resuming causes Anthropic to return errors. Detect these patterns
            # and retry with increasing severity:
            #   1. Same session retry — works for transient concurrency errors
            #      where the failed turn is simply dropped from history.
            #   2. Fresh session retry — nukes conversation history, needed when
            #      the stored history itself is corrupted (orphaned tool_use blocks).
            error_lower = result.text.lower()
            _SESSION_ERROR_PATTERNS = (
                "tool use concurrency",
                "tool_use` ids must be unique",
                "duplicate tool_use",
            )
            _FATAL_SESSION_PATTERNS = (
                "no conversation found with session id",
            )
            is_transient = any(p in error_lower for p in _SESSION_ERROR_PATTERNS)
            is_fatal = any(p in error_lower for p in _FATAL_SESSION_PATTERNS)

            if result.error and is_transient and result.session_id:
                # Retry 1: same session — transient concurrency errors often
                # resolve because the CLI drops the failed turn on resume.
                logger.warning(
                    f"ClaudeCodeEngine [{project_name}]: transient session error, "
                    f"retrying with same session {result.session_id}..."
                )
                await asyncio.sleep(1)  # brief backoff
                result = await self._invoke_streaming_locked(
                    instruction, project_name, project_dir,
                    model=model, on_progress=on_progress,
                    session_id=result.session_id,
                    disallowed_tools=disallowed_tools,
                    personality_override=personality_override,
                    source=source, timeout=timeout,
                )
                # If the same-session retry also failed, fall through to fresh.
                retry_lower = result.text.lower()
                is_still_broken = any(
                    p in retry_lower
                    for p in (*_SESSION_ERROR_PATTERNS, *_FATAL_SESSION_PATTERNS)
                )
                if result.error and is_still_broken:
                    is_fatal = True  # escalate to fresh session

            if result.error and is_fatal:
                logger.warning(
                    f"ClaudeCodeEngine [{project_name}]: fatal session error, "
                    f"clearing session and retrying fresh..."
                )
                if self.db:
                    await self.db.terminate_session(project_name)
                result = await self._invoke_streaming_locked(
                    instruction, project_name, project_dir,
                    model=model, on_progress=on_progress,
                    session_id=None, disallowed_tools=disallowed_tools,
                    personality_override=personality_override,
                    source=source, timeout=timeout,
                )
            return result
        finally:
            lock.release()
            logger.info(f"ClaudeCodeEngine [{project_name}]: lock released (locked={lock.locked()})")

    async def _invoke_streaming_locked(
        self,
        instruction: str,
        project_name: str,
        project_dir: str,
        model: str = "sonnet",
        on_progress: Optional[Callable] = None,
        session_id: Optional[str] = None,
        disallowed_tools: Optional[str] = None,
        personality_override: str = "",
        source: str = "",
        timeout: Optional[int] = None,
    ) -> StreamResult:
        logger.info(f"ClaudeCodeEngine [{project_name}]: _invoke_streaming_locked started")
        # Look up real session ID from DB; only resume if one exists AND project_dir matches
        if session_id:
            sid = session_id
        else:
            logger.info(f"ClaudeCodeEngine [{project_name}]: looking up session from DB...")
            stored = await self.db.get_session(project_name) if self.db else None
            logger.info(f"ClaudeCodeEngine [{project_name}]: session lookup complete, stored={stored}")
            if stored and stored.get("project_dir") and project_dir:
                stored_dir = str(Path(stored["project_dir"]).expanduser())
                current_dir = str(Path(project_dir).expanduser())
                if stored_dir != current_dir:
                    logger.warning(
                        f"[{project_name}] project_dir mismatch — clearing stale session "
                        f"(stored={stored_dir}, current={current_dir})"
                    )
                    if self.db:
                        await self.db.db.execute(
                            "DELETE FROM sessions WHERE LOWER(name) = LOWER(?)", (project_name,)
                        )
                        await self.db.db.commit()
                    stored = None
            sid = stored["session_id"] if stored else None

        logger.info(f"ClaudeCodeEngine [{project_name}]: building system prompt...")
        system_prompt = await self._build_system_prompt(instruction, project_name,
                                                        project_dir=project_dir,
                                                        personality_override=personality_override,
                                                        source=source,
                                                        session_id=sid or "")
        logger.info(f"ClaudeCodeEngine [{project_name}]: system prompt built, length={len(system_prompt)}")

        cmd = [self.binary]
        cmd.extend(["--output-format", "stream-json", "--verbose"])
        cmd.extend(["--model", model])

        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        if sid:
            cmd.extend(["--resume", sid])

        if disallowed_tools:
            cmd.extend(["--disallowedTools", disallowed_tools])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # Place -p and the instruction LAST with a `--` separator so user
        # input starting with a dash (e.g. a bullet list) isn't parsed as a flag.
        cmd.extend(["-p", "--", instruction])

        # CRITICAL: `cwd` is the LOCAL project_dir. It must ALWAYS be passed to
        # upsert_session() so the session_mismatch detection (lines ~300-314)
        # finds a stable local-path match on subsequent turns. For remote
        # execution, the subprocess's own cwd becomes ~ (irrelevant — SSH
        # cd's on the remote), but `cwd` itself remains unchanged.
        cwd = str(Path(project_dir).expanduser())
        if not Path(cwd).exists():
            cwd = str(Path.home())

        # --- Remote routing ---
        is_remote, remote_project_dir = self._get_remote_config(project_name)
        local_cmd = cmd  # keep original for fallback
        subprocess_cwd = cwd  # default: local path
        if is_remote:
            try:
                remote_binary = self.config.claude_code.remote_binary_path or "claude"
                cmd = self._build_remote_cmd(cmd, remote_binary, remote_cwd=remote_project_dir)
                subprocess_cwd = str(Path.home())  # local cwd irrelevant for SSH
                logger.info(f"ClaudeCodeEngine [{project_name}]: routing via SSH to {self.config.claude_code.remote_host}")
            except ValueError as e:
                logger.error(f"ClaudeCodeEngine [{project_name}]: remote config error, falling back to local: {e}")
                is_remote = False
                cmd = local_cmd

        logger.info(f"ClaudeCodeEngine [{project_name}] model={model}: {instruction[:80]}...")

        text_parts: list[str] = []
        tool_calls: list[str] = []
        cost_usd = 0.0
        duration_ms = 0
        final_session_id = sid or ""
        num_turns = 0
        timed_out = False
        errored = False
        ssh_failed = False
        ssh_error_text = ""
        process = None

        async def _run_process(_cmd: list[str], _subprocess_cwd: str):
            """Run subprocess with streaming. Closes over outer mutables."""
            nonlocal cost_usd, duration_ms, final_session_id, num_turns, process, timed_out, errored
            process = await asyncio.create_subprocess_exec(
                *_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=_subprocess_cwd,
                limit=8 * 1024 * 1024,  # 8MB per line — stream-json events can be huge
            )
            self._running_processes[project_name] = process

            # Use provided timeout, or default to config. None means no timeout.
            actual_timeout = timeout if timeout is not None else self.config.claude_code.timeout_seconds
            stderr_bytes_holder: list[bytes] = []

            async def read_stream():
                nonlocal cost_usd, duration_ms, final_session_id, num_turns, errored
                async for event in self._parse_stream(process.stdout):
                    if event.type == "init":
                        if event.session_id:
                            # Claude CLI 2.x may return prefixed IDs (e.g. "claude-<uuid>")
                            # but --resume requires a bare UUID. Extract the UUID part.
                            _sid = event.session_id
                            import re as _re
                            _m = _re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', _sid, _re.I)
                            if _m:
                                _sid = _m.group(0)
                            final_session_id = _sid
                            # Persist immediately so timeouts don't lose the session.
                            # Always pass LOCAL cwd (project_dir), never subprocess_cwd.
                            if self.db:
                                await self.db.upsert_session(project_name, _sid, cwd)
                    elif event.type == "text":
                        if event.text:
                            text_parts.append(event.text)
                            num_turns += 1
                    elif event.type == "tool_use":
                        tool_calls.append(event.tool_name or "unknown")
                        if on_progress:
                            try:
                                result = on_progress(event)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception:
                                pass
                    elif event.type == "result":
                        cost_usd = event.cost_usd or 0.0
                        duration_ms = event.duration_ms or 0
                        # A result event with is_error=true (e.g. "No conversation
                        # found with session ID: ...") means the turn failed even
                        # though the subprocess will exit 0. Mark as errored so
                        # the caller clears the stale session and self-heals on
                        # the next turn. Surface the error text to the user so
                        # they see something other than "Completed with no output."
                        if event.is_error:
                            errored = True
                            if event.error_text and not text_parts:
                                text_parts.append(f"[Claude error: {event.error_text}]")
                            logger.warning(
                                f"ClaudeCodeEngine [{project_name}]: result event "
                                f"is_error=true: {event.error_text or '(no detail)'}"
                            )

            async def drain_stderr():
                if process.stderr:
                    err_bytes = await process.stderr.read()
                    if err_bytes:
                        stderr_bytes_holder.append(err_bytes)
                        logger.warning(f"ClaudeCodeEngine [{project_name}] stderr: {err_bytes.decode('utf-8', errors='replace')[:500]}")

            if actual_timeout:
                await asyncio.wait_for(
                    asyncio.gather(read_stream(), drain_stderr()), timeout=actual_timeout
                )
            else:
                # No timeout - wait indefinitely
                await asyncio.gather(read_stream(), drain_stderr())
            await process.wait()
            if process.returncode and process.returncode != 0:
                errored = True
            return stderr_bytes_holder[0] if stderr_bytes_holder else b""

        def _is_ssh_failure(stderr_bytes: bytes, returncode: int) -> bool:
            """Detect SSH-specific failure patterns to trigger fallback."""
            if returncode != 255:  # ssh exit 255 = connection failure
                return False
            err_text = stderr_bytes.decode("utf-8", errors="replace").lower()
            indicators = (
                "connection refused",
                "connection timed out",
                "no route to host",
                "host is down",
                "could not resolve hostname",
                "port 22: operation timed out",
                "ssh: connect to host",
                "permission denied (publickey",
            )
            return any(ind in err_text for ind in indicators)

        try:
            stderr_bytes = await _run_process(cmd, subprocess_cwd)

            # Detect SSH failure and fall back to local execution
            if is_remote and process and _is_ssh_failure(stderr_bytes, process.returncode or 0):
                ssh_failed = True
                ssh_error_text = stderr_bytes.decode("utf-8", errors="replace")[:300].strip()
                logger.error(
                    f"ClaudeCodeEngine [{project_name}]: SSH to "
                    f"{self.config.claude_code.remote_host} failed (exit {process.returncode}). "
                    f"Falling back to local. SSH error: {ssh_error_text}"
                )
                # Reset per-turn state for fallback attempt
                text_parts.clear()
                tool_calls.clear()
                cost_usd = 0.0
                duration_ms = 0
                num_turns = 0
                errored = False
                self._running_processes.pop(project_name, None)
                await _run_process(local_cmd, cwd)

        except asyncio.TimeoutError:
            timed_out = True
            timeout_display = actual_timeout if actual_timeout else self.config.claude_code.timeout_seconds
            logger.warning(f"ClaudeCodeEngine [{project_name}]: timed out after {timeout_display}s")
            if process:
                try:
                    process.kill()
                except Exception:
                    pass

        except FileNotFoundError:
            return StreamResult(
                text=f"Error: Claude Code not found at '{self.binary}'. Is it installed?",
                cost_usd=0, duration_ms=0, num_turns=0,
                model=model, session_id=sid, tool_calls=[], error=True,
            )

        except Exception as e:
            return StreamResult(
                text=f"Error: {type(e).__name__}: {e}",
                cost_usd=0, duration_ms=0, num_turns=0,
                model=model, session_id=sid, tool_calls=[], error=True,
            )

        finally:
            self._running_processes.pop(project_name, None)

        if timed_out:
            timeout_display = actual_timeout if actual_timeout else self.config.claude_code.timeout_seconds
            text_parts.append(f"\n\n[Timed out after {timeout_display}s]")

        combined_text = "".join(text_parts).strip()
        max_chars = getattr(self.config.claude_code, "max_output_chars", 50000)
        if len(combined_text) > max_chars:
            half = max_chars // 2
            combined_text = combined_text[:half] + "\n\n...[truncated]...\n\n" + combined_text[-(max_chars // 4):]

        # Surface SSH failure notice to user (prepended to response so it's visible in Telegram/TUI)
        if ssh_failed:
            notice = (
                f"[SSH to {self.config.claude_code.remote_host} failed — "
                f"fell back to local execution]\n\n"
            )
            combined_text = notice + combined_text

        if timed_out or errored:
            if self.db:
                await self.db.terminate_session(project_name)
        else:
            await self.db.upsert_session(project_name, final_session_id, cwd)
            await self.db.update_session_activity(project_name)

        # Fire-and-forget: pull KB database back from Air after a remote turn
        # so any agent writes land in the authoritative Mac DB.
        if is_remote and not ssh_failed:
            asyncio.create_task(self._pull_kb_from_remote(project_name))

        return StreamResult(
            text=combined_text or "Completed with no output.",
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=num_turns,
            model=model,
            session_id=final_session_id,
            tool_calls=tool_calls,
            error=timed_out or errored,
        )

    async def _pull_kb_from_remote(self, project_name: str) -> None:
        """Background pull of KB database from <remote-alias> after a remote agent turn."""
        sync_script = Path(__file__).parent.parent / "scripts" / "sync-kb-remote.sh"
        if not sync_script.exists():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                str(sync_script), "pull",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.info(f"KB pull-back from remote succeeded after [{project_name}] turn")
            else:
                err = stderr.decode("utf-8", errors="replace")[:200].strip()
                logger.warning(f"KB pull-back failed after [{project_name}] turn: {err}")
        except asyncio.TimeoutError:
            logger.warning(f"KB pull-back timed out after [{project_name}] turn")
        except Exception as e:
            logger.warning(f"KB pull-back error after [{project_name}] turn: {e}")

    async def invoke(self, instruction: str, project_name: str, project_dir: str,
                     model: str = "sonnet", **kwargs) -> str:
        """Backward-compat wrapper."""
        result = await self.invoke_streaming(instruction, project_name, project_dir, model=model)
        return result.text

    async def invoke_chat(
        self,
        message: str,
        model: str = "haiku",
        on_progress: Optional[Callable] = None,
        source: str = "",
    ) -> StreamResult:
        """For general (non-project) chat. Daily-rotating session, read-only tools."""
        general_dir = getattr(self.config.claude_code, "general_session_dir", "~/.halos")
        general_dir_expanded = str(Path(general_dir).expanduser())
        if not Path(general_dir_expanded).exists():
            general_dir_expanded = str(Path.home())

        session_id = self._session_id_for_general()

        return await self.invoke_streaming(
            instruction=message,
            project_name="general",
            project_dir=general_dir_expanded,
            model=model,
            on_progress=on_progress,
            session_id=session_id,
            disallowed_tools="Edit,Write,NotebookEdit",
            source=source,
        )

    async def invoke_ephemeral(
        self,
        prompt: str,
        system: Optional[str] = None,
        model: str = "haiku",
    ) -> str:
        """Replacement for claude_api.complete(). No session persistence."""
        general_dir = getattr(self.config.claude_code, "general_session_dir", "~/.halos")
        general_dir_expanded = str(Path(general_dir).expanduser())
        if not Path(general_dir_expanded).exists():
            general_dir_expanded = str(Path.home())

        full_prompt = prompt if not system else f"{system}\n\n{prompt}"

        cmd = [self.binary]
        cmd.extend(["--output-format", "stream-json", "--verbose"])
        cmd.extend(["--model", model])
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        # -p + prompt last, with `--` separator so dashy prompts aren't mis-parsed.
        cmd.extend(["-p", "--", full_prompt])

        text_parts: list[str] = []
        timeout = min(self.config.claude_code.timeout_seconds, 120)
        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=general_dir_expanded,
                limit=8 * 1024 * 1024,  # 8MB per line — stream-json events can be huge
            )

            async def _read():
                async for event in self._parse_stream(process.stdout):
                    if event.type == "text" and event.text:
                        text_parts.append(event.text)
                await process.wait()

            await asyncio.wait_for(_read(), timeout=timeout)

        except asyncio.TimeoutError:
            if process:
                try:
                    process.kill()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"

        return "".join(text_parts).strip()

    # --- Session management ---

    async def kill_session(self, project_name: str) -> str:
        proc = self._running_processes.get(project_name)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
            self._running_processes.pop(project_name, None)
        await self.db.terminate_session(project_name)
        return f"Session '{project_name}' terminated."

    def get_running(self) -> list[str]:
        return list(self._running_processes.keys())
