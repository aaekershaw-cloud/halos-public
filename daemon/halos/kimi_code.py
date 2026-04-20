"""Kimi CLI engine with NDJSON streaming support for HalOS."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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


@dataclass
class StreamEvent:
    type: str
    text: Optional[str] = None
    tool_name: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    session_id: Optional[str] = None
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


class KimiCodeEngine:
    def __init__(self, config: Config, db: Database, memory=None, binary: str = "kimi", skill_evolver=None):
        self.config = config
        self.db = db
        self.memory = memory
        self.binary = binary
        self.skill_evolver = skill_evolver
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._personality: str = ""

        self.project_map = dict(config.claude_code.projects) if config.claude_code.projects else {}
        self.aliases = dict(config.claude_code.aliases) if config.claude_code.aliases else {}

    def load_personality(self, path: str):
        p = Path(path)
        if p.exists():
            self._personality = p.read_text().strip()

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
            self._session_locks[project_name] = asyncio.Lock()
        return self._session_locks[project_name]

    @staticmethod
    def _agent_key_for(project_name: str, project_dir: str) -> str:
        """Derive the KB agent key from project_dir basename or project_name.

        Agent keys in the KB memory table are lowercase (beta, alpha,
        gamma). Session dir basenames already match, so prefer that.
        """
        if project_dir:
            basename = Path(project_dir).name
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
                                    source: str = "") -> str:
        parts = []

        if personality_override:
            parts.append(personality_override)
        elif project_dir:
            soul_path = Path(project_dir) / "soul.md"
            if soul_path.exists():
                parts.append(soul_path.read_text().strip())
            elif self._personality:
                parts.append(self._personality)
        elif self._personality:
            parts.append(self._personality)

        now = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        parts.append(f"Current datetime: {now}")

        # Agent memory from KB
        agent_key = self._agent_key_for(project_name, project_dir)
        loaded_from_kb = False
        if agent_key:
            memory_block = await self._load_kb_memory(agent_key, max_chars=4000)
            if memory_block:
                parts.append(memory_block)
                loaded_from_kb = True

        if not loaded_from_kb and self.memory:
            flat = self.memory.get_flat_memory()
            if flat:
                truncated = flat[:2000] if len(flat) > 2000 else flat
                parts.append(f"## Memory\n{truncated}")

        # Relevant skills (L3 — self-evolved SOPs)
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

        if self.db:
            recent = await self.db.get_recent_messages(source=source, limit=10)
            if recent:
                history_lines = []
                for msg in recent:
                    role = msg["role"].upper()
                    content = msg["content"][:300]
                    history_lines.append(f"{role}: {content}")
                parts.append("## Recent conversation\n" + "\n".join(history_lines))

        if source.startswith("telegram:"):
            parts.append(
                "## Telegram Messaging\n"
                "You are chatting via Telegram. You can send proactive messages to other agents "
                "by running: python -m halos.msg <agent_name> \"<message>\""
            )

        return "\n\n".join(parts)

    async def _parse_stream(self, stdout: asyncio.StreamReader, session_id: str) -> AsyncIterator[StreamEvent]:
        """Parse NDJSON from kimi stdout, yielding StreamEvents."""
        # Kimi doesn't emit an init event; synthesize one for consistency
        yield StreamEvent(type="init", session_id=session_id, raw={})

        async for raw_line in stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON stdout: {line[:100]}")
                continue

            role = event.get("role", "")

            if role == "assistant":
                content = event.get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        yield StreamEvent(
                            type="text",
                            text=block.get("text", ""),
                            session_id=session_id,
                            raw=event,
                        )

                for tc in event.get("tool_calls", []):
                    fn = tc.get("function", {})
                    yield StreamEvent(
                        type="tool_use",
                        tool_name=fn.get("name"),
                        session_id=session_id,
                        raw=event,
                    )

            elif role == "tool":
                # Tool result line — optionally yield for logging
                yield StreamEvent(
                    type="tool_result",
                    text=str(event.get("content", ""))[:200],
                    session_id=session_id,
                    raw=event,
                )

        # Synthesize a result event since kimi doesn't emit one
        yield StreamEvent(type="result", cost_usd=0.0, duration_ms=0, session_id=session_id, raw={})

    async def invoke_streaming(
        self,
        instruction: str,
        project_name: str,
        project_dir: str,
        model: str = "default",
        on_progress: Optional[Callable] = None,
        session_id: Optional[str] = None,
        disallowed_tools: Optional[str] = None,
        personality_override: str = "",
        source: str = "",
    ) -> StreamResult:
        lock = self._get_lock(project_name)
        async with lock:
            return await self._invoke_streaming_locked(
                instruction, project_name, project_dir,
                model=model, on_progress=on_progress,
                session_id=session_id, disallowed_tools=disallowed_tools,
                personality_override=personality_override,
                source=source,
            )

    async def _invoke_streaming_locked(
        self,
        instruction: str,
        project_name: str,
        project_dir: str,
        model: str = "default",
        on_progress: Optional[Callable] = None,
        session_id: Optional[str] = None,
        disallowed_tools: Optional[str] = None,
        personality_override: str = "",
        source: str = "",
    ) -> StreamResult:
        if session_id:
            sid = session_id
        else:
            stored = await self.db.get_session(project_name) if self.db else None
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

        system_prompt = await self._build_system_prompt(instruction, project_name,
                                                        project_dir=project_dir,
                                                        personality_override=personality_override,
                                                        source=source)

        # Prepend system prompt to instruction since kimi has no --append-system-prompt
        full_prompt = instruction
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{instruction}"

        binary = shutil.which(self.binary) or str(Path.home() / ".local" / "bin" / "kimi")
        cmd = [binary, "--print", "--yolo", "-p", full_prompt]
        cmd.extend(["--output-format", "stream-json"])

        if model and model != "default":
            cmd.extend(["--model", model])

        if sid:
            cmd.extend(["--session", sid])

        cwd = str(Path(project_dir).expanduser())
        if not Path(cwd).exists():
            cwd = str(Path.home())

        logger.info(f"KimiCodeEngine [{project_name}] model={model}: {instruction[:80]}...")

        text_parts: list[str] = []
        tool_calls: list[str] = []
        cost_usd = 0.0
        duration_ms = 0
        final_session_id = sid or ""
        num_turns = 0
        timed_out = False
        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            self._running_processes[project_name] = process

            timeout = self.config.claude_code.timeout_seconds

            async def read_stream():
                nonlocal cost_usd, duration_ms, final_session_id, num_turns
                async for event in self._parse_stream(process.stdout, session_id=final_session_id):
                    if event.type == "init":
                        if event.session_id:
                            final_session_id = event.session_id
                            if self.db:
                                await self.db.upsert_session(project_name, event.session_id, cwd)
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

            async def drain_stderr():
                if process.stderr:
                    err_bytes = await process.stderr.read()
                    if err_bytes:
                        logger.warning(f"KimiCodeEngine [{project_name}] stderr: {err_bytes.decode('utf-8', errors='replace')[:500]}")

            await asyncio.wait_for(
                asyncio.gather(read_stream(), drain_stderr()), timeout=timeout
            )
            await process.wait()

        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(f"KimiCodeEngine [{project_name}]: timed out after {self.config.claude_code.timeout_seconds}s")
            if process:
                try:
                    process.kill()
                except Exception:
                    pass

        except FileNotFoundError:
            return StreamResult(
                text=f"Error: Kimi CLI not found at '{self.binary}'. Is it installed?",
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
            text_parts.append(f"\n\n[Timed out after {self.config.claude_code.timeout_seconds}s]")

        combined_text = "".join(text_parts).strip()
        max_chars = getattr(self.config.claude_code, "max_output_chars", 8000)
        if len(combined_text) > max_chars:
            half = max_chars // 2
            combined_text = combined_text[:half] + "\n\n...[truncated]...\n\n" + combined_text[-(max_chars // 4):]

        if timed_out:
            if self.db:
                await self.db.terminate_session(project_name)
        else:
            await self.db.upsert_session(project_name, final_session_id, cwd)
            await self.db.update_session_activity(project_name)

        return StreamResult(
            text=combined_text or "Completed with no output.",
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=num_turns,
            model=model,
            session_id=final_session_id,
            tool_calls=tool_calls,
            error=timed_out,
        )

    async def invoke(self, instruction: str, project_name: str, project_dir: str,
                     model: str = "default", **kwargs) -> str:
        result = await self.invoke_streaming(instruction, project_name, project_dir, model=model)
        return result.text

    async def invoke_chat(
        self,
        message: str,
        model: str = "default",
        on_progress: Optional[Callable] = None,
        source: str = "",
    ) -> StreamResult:
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
            source=source,
        )

    async def invoke_ephemeral(
        self,
        prompt: str,
        system: Optional[str] = None,
        model: str = "default",
    ) -> str:
        general_dir = getattr(self.config.claude_code, "general_session_dir", "~/.halos")
        general_dir_expanded = str(Path(general_dir).expanduser())
        if not Path(general_dir_expanded).exists():
            general_dir_expanded = str(Path.home())

        full_prompt = prompt if not system else f"{system}\n\n{prompt}"

        binary = shutil.which(self.binary) or str(Path.home() / ".local" / "bin" / "kimi")
        cmd = [binary, "--print", "--yolo", "-p", full_prompt]
        cmd.extend(["--output-format", "stream-json"])
        if model and model != "default":
            cmd.extend(["--model", model])

        text_parts: list[str] = []
        timeout = min(self.config.claude_code.timeout_seconds, 120)
        process = None

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=general_dir_expanded,
            )

            async def _read():
                async for event in self._parse_stream(process.stdout, session_id=""):
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
