"""HalOS Terminal Dashboard -- Textual-based TUI for managing Claude Code sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.strip import Strip
from rich.markup import escape as rich_escape
from rich.segment import Segment
from rich.style import Style
from rich.text import Text as RichText
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    TextArea,
)

from .config import load_config, Config
from .db import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class OutputLine:
    """A single parsed output line from a Claude Code subprocess."""
    type: str           # assistant, tool_use, tool_result, system, result, partial, raw, user, error
    text: str           # Displayable text
    timestamp: float = field(default_factory=time.time)
    raw: dict | None = None


# ---------------------------------------------------------------------------
# File drag-and-drop support (macOS bracketed-paste mechanism)
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".tif", ".heic", ".heif", ".avif"}


def _is_image_file(path: str) -> bool:
    """Check if a file path points to an image based on extension."""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def _resolve_unicode_path(path: str) -> str | None:
    """Resolve a path handling macOS Unicode whitespace quirks.

    macOS screenshot filenames use U+202F (narrow no-break space) between
    the time and AM/PM, but Terminal.app may normalize it to a regular space
    during bracketed paste. If the literal path doesn't exist, scan the
    parent directory for a filename that matches when both characters are
    treated as equivalent.
    """
    if os.path.isfile(path) or os.path.isdir(path):
        return path
    # Try swapping regular space ↔ U+202F
    for alt in (path.replace(" ", "\u202f"), path.replace("\u202f", " ")):
        if os.path.isfile(alt) or os.path.isdir(alt):
            return alt
    # Fallback: scan parent directory with whitespace normalization
    try:
        parent = os.path.dirname(path)
        target = os.path.basename(path)
        if not os.path.isdir(parent):
            return None
        target_norm = target.replace("\u202f", " ")
        for entry in os.listdir(parent):
            if entry.replace("\u202f", " ") == target_norm:
                return os.path.join(parent, entry)
    except OSError:
        pass
    return None


def _parse_dropped_files(text: str) -> list[str]:
    """Parse bracketed-paste text and return valid file/dir paths.

    macOS terminals (Terminal.app, iTerm2) inject dropped file paths as
    bracketed paste, shell-escaping spaces with backslashes. shlex.split()
    undoes that escaping, then we validate each token against the filesystem.
    Returns an empty list if the paste contains no real paths (i.e. it's
    regular text being pasted by the user).
    """
    try:
        parts = shlex.split(text.replace("\x00", ""))
    except ValueError:
        return []
    if not parts:
        return []
    # Require ALL tokens to be valid filesystem paths — if any token is plain
    # text (e.g. " / " in "frontend / backend" produces "/" as a token which
    # happens to be the root dir), treat the whole paste as regular text.
    valid = []
    for part in parts:
        clean = part.replace("\x00", "")
        resolved = _resolve_unicode_path(clean)
        if resolved:
            valid.append(resolved)
        else:
            return []  # mixed tokens → regular text, not a file drop
    return valid


# ---------------------------------------------------------------------------
# SessionInput -- Wrapping chat input with Enter-to-submit and file drop support
# ---------------------------------------------------------------------------

class SessionInput(TextArea):
    """Wrapping chat input built on TextArea.

    Enter submits, Shift+Enter inserts a newline. Soft-wrap is enabled so
    long messages wrap visually instead of scrolling horizontally.

    Paste handling lives in HalOSTUI.on_event (must intercept before Textual
    forwards the Paste event to this widget via App.on_event).
    """

    class FileDropped(Message):
        """Posted when files are drag-dropped onto the input."""
        def __init__(self, files: list[str]) -> None:
            super().__init__()
            self.files = files

    class Submitted(Message):
        """Emitted when the user presses Enter to submit."""
        def __init__(self, value: str, input: "SessionInput") -> None:
            super().__init__()
            self.value = value
            self.input = input

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        # Strip Input-specific kwargs that callers may pass
        kwargs.pop("value", None)
        super().__init__(
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            theme="css",
            placeholder=placeholder,
            **kwargs,
        )
        self._paste_store: str = ""  # holds the last collapsed multi-line paste
        self._pending_images: list[str] = []  # image paths from drag-and-drop

    # --- Compat with Input API (used by _update_input_state and on_submit) ---

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, v: str) -> None:
        self.text = v

    def insert_text_at_cursor(self, text: str) -> None:
        """Insert text at the current cursor position."""
        self.insert(text, self.cursor_location)

    # --- Key handling ---
    # TextArea maps Enter → newline in _on_key BEFORE bindings are checked,
    # so we must override _on_key directly to intercept Enter for submit.

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            # Plain Enter submits the message
            event.prevent_default()
            event.stop()
            self._submit()
            return
        if event.key == "shift+enter":
            # Shift+Enter inserts a newline
            event.prevent_default()
            event.stop()
            self.insert("\n", self.cursor_location)
            return
        await super()._on_key(event)

    def _submit(self) -> None:
        """Submit the current text."""
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(text, self))
        self.text = ""

    # --- Paste handling (overrides TextArea._on_paste) ---

    async def _on_paste(self, event) -> None:
        """Handle paste events directly, overriding TextArea's default.

        TextArea._on_paste inserts pasted text unconditionally. We override it
        to detect file drops (macOS bracketed paste) and multi-line pastes.
        """
        raw = event.text.replace("\x00", "")
        files = _parse_dropped_files(raw)
        if files:
            images = [f for f in files if _is_image_file(f)]
            other = [f for f in files if not _is_image_file(f)]
            if images:
                self._pending_images.extend(images)
                img_labels = " ".join(f"[image: {os.path.basename(f)}]" for f in images)
                self.insert_text_at_cursor(img_labels)
            if other:
                self.insert_text_at_cursor(
                    " ".join(f'"{f}"' if " " in f else f for f in other)
                )
            self.post_message(self.FileDropped(files))
        elif "\n" in raw:
            self._paste_store = raw
            self.insert_text_at_cursor("[paste]")
        else:
            # Single-line paste: insert directly
            self.insert_text_at_cursor(raw)

    # --- Paste/image helpers ---

    def expand_paste(self, text: str) -> str:
        """Replace [paste] with the stored content. Clears store after use."""
        if "[paste]" in text and self._paste_store:
            expanded = text.replace("[paste]", self._paste_store, 1)
            self._paste_store = ""
            return expanded
        return text

    def expand_images(self, text: str) -> str:
        """Wrap prompt with image reading instructions if images are pending."""
        if not self._pending_images:
            return text
        images = self._pending_images
        self._pending_images = []

        # Remove [image: filename] placeholders from the text
        clean_text = text
        for img in images:
            placeholder = f"[image: {os.path.basename(img)}]"
            clean_text = clean_text.replace(placeholder, "").strip()

        # Build image instruction
        if len(images) == 1:
            img_instruction = f"Read this image file: {images[0]}"
        else:
            paths = "\n".join(f"- {img}" for img in images)
            img_instruction = f"Read these image files:\n{paths}"

        user_msg = clean_text if clean_text else "What do you see in this image?"
        return f"{img_instruction}\n\n{user_msg}"


# ---------------------------------------------------------------------------
# SelectableRichLog -- colored log with mouse line-selection
# ---------------------------------------------------------------------------

class SelectableRichLog(RichLog):
    """A RichLog that supports click-drag line selection and copy."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._selecting = False
        self._sel_start: int | None = None
        self._sel_end: int | None = None

    def _on_mouse_down(self, event: MouseDown) -> None:
        # Clear prior selection on plain click; start new selection on drag
        self._selecting = True
        self.capture_mouse()
        self._sel_start = max(0, self.scroll_offset.y + event.y)
        self._sel_end = self._sel_start
        self._line_cache.clear()
        self.refresh()

    def _on_mouse_move(self, event: MouseMove) -> None:
        if self._selecting:
            new_end = self.scroll_offset.y + event.y
            if new_end != self._sel_end:
                self._sel_end = new_end
                self._line_cache.clear()
                self.refresh()

    def _on_mouse_up(self, event: MouseUp) -> None:
        if self._selecting:
            self._selecting = False
            self.release_mouse()
            final = self.scroll_offset.y + event.y
            # Plain click without drag → clear selection
            if self._sel_start == self._sel_end == final:
                self._sel_start = None
                self._sel_end = None
            else:
                self._sel_end = final
            self._line_cache.clear()
            self.refresh()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self._sel_start is None or self._sel_end is None:
            return strip
        virtual_y = self.scroll_offset.y + y
        start, end = sorted((self._sel_start, self._sel_end))
        if start <= virtual_y <= end:
            highlighted = []
            for segment in strip:
                style = (segment.style or Style()) + Style(bgcolor="bright_blue", color="white")
                highlighted.append(Segment(segment.text, style, segment.control))
            return Strip(highlighted, strip.cell_length)
        return strip

    @property
    def selected_text(self) -> str:
        if self._sel_start is None or self._sel_end is None:
            return ""
        start, end = sorted((self._sel_start, self._sel_end))
        start = max(0, start)
        end = min(end, len(self.lines) - 1)
        lines: list[str] = []
        for y in range(start, end + 1):
            if 0 <= y < len(self.lines):
                lines.append("".join(seg.text for seg in self.lines[y]))
        return "\n".join(lines)

    def clear_selection(self) -> None:
        if self._sel_start is not None:
            self._sel_start = None
            self._sel_end = None
            self.refresh()


# ---------------------------------------------------------------------------
# SessionProcess -- manages a single Claude Code subprocess
# ---------------------------------------------------------------------------

class SessionProcess:
    """Manages a Claude Code session as a series of per-turn subprocesses.

    Each user message spawns a fresh `claude -p` process. The actual session_id
    returned by Claude in the system:init event is captured after the first turn
    and used for --resume on all subsequent turns, keeping the conversation
    history intact. The confirmed session ID is also persisted to DB so Telegram
    agent bots can resume the same conversation.
    """

    def __init__(self, name: str, project_dir: str, session_id: str, config: Config, db: Database, binary: str = "claude", from_db: bool = False, remote: bool = False, remote_project_dir: str = ""):
        self.name = name
        self.project_dir = project_dir
        self.session_id = session_id          # initial hint; may be stale uuid5
        self._confirmed_session_id: Optional[str] = None  # real ID from Claude's init event
        self.config = config
        self.db = db
        self.binary = binary
        self.is_kimi = binary == "kimi"
        self.remote = remote
        self.remote_project_dir = remote_project_dir

        # If session came from DB, it's a real session_id from a previous run - treat as confirmed
        # This allows resuming sessions after TUI reload
        if self.is_kimi or from_db:
            self._confirmed_session_id = session_id
        self.process: asyncio.subprocess.Process | None = None
        self.output_buffer: deque[OutputLine] = deque(maxlen=10000)  # Increased for verbose tool outputs
        self.listeners: list = []
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)  # recent stderr for post-exit diagnosis
        self._telegram_poll_task: asyncio.Task | None = None
        self._last_telegram_msg_id: int = 0
        self.model: str | None = None  # overrides config.claude_code.default_model when set
        self.is_busy = False
        self.is_alive = False  # True once the first turn has been dispatched
        self._inbox: asyncio.Queue = asyncio.Queue()  # queued inter-agent messages
        self._pending_messages: list[str] = []  # queue for user messages sent while busy

    async def start(self, initial_prompt: Optional[str] = None) -> None:
        """Mark session live and optionally kick off the first turn."""
        self.is_alive = True
        # Load recent Telegram history so the TUI reflects past messages
        await self._load_telegram_history(limit=50)
        self._telegram_poll_task = asyncio.create_task(self._telegram_poll_loop())
        if initial_prompt:
            user_line = OutputLine(type="user", text=initial_prompt)
            self.output_buffer.append(user_line)
            for listener in self.listeners:
                listener(user_line)
            await self._run_turn(initial_prompt)

    async def send(self, text: str) -> None:
        """Send a user message (spawns a new claude process for this turn).
        
        If busy, queues the message to be sent when the current turn completes.
        """
        if not self.is_alive:
            return
        
        # If busy, queue the message and return immediately
        if self.is_busy:
            self._pending_messages.append(text)
            # Notify listeners that message is queued
            for listener in self.listeners:
                listener(OutputLine(type="system", text=f"[queued: message will send when {('Kimi' if self.is_kimi else 'Claude')} finishes]"))
            return
        
        user_line = OutputLine(type="user", text=text)
        self.output_buffer.append(user_line)
        for listener in self.listeners:
            listener(user_line)
        await self._run_turn(text)

    async def _run_turn(self, prompt: str) -> None:
        """Spawn a claude -p or kimi process for one turn."""
        if self.is_kimi:
            # Resolve full path to kimi binary since PATH may differ in spawned processes
            kimi_path = shutil.which("kimi") or str(Path.home() / ".local" / "bin" / "kimi")
            cmd = [kimi_path, "--print", "--yolo", "-p", prompt, "--output-format", "stream-json"]
            model = self.model or ""
            if model and model != "default":
                cmd.extend(["--model", model])
            if self.session_id:
                cmd.extend(["--session", self.session_id])
        else:
            # Refresh session_id from DB before every turn. AgentBot (Telegram)
            # writes the latest Claude-assigned session_id to the same
            # `telegram:claude:{name}` key after each reply; without this
            # refresh the TUI would --resume a stale cached id, fork the
            # conversation and lose context written by the bot. This is the
            # mechanism that keeps TUI and Telegram on the same session.
            try:
                db_row = await self.db.get_session(f"telegram:claude:{self.name.lower()}")
                if db_row:
                    db_sid = db_row.get("session_id") or ""
                    # Only accept real session ids (not our uuid5 placeholder).
                    # The uuid5 placeholder is the initial self.session_id.
                    if (
                        db_sid
                        and db_sid != self.session_id
                        and db_sid != self._confirmed_session_id
                    ):
                        logger.info(
                            f"[{self.name}] Refreshing session_id from DB: "
                            f"{(self._confirmed_session_id or '')[:8]}… -> {db_sid[:8]}…"
                        )
                        self._confirmed_session_id = db_sid
            except Exception:
                logger.exception(f"[{self.name}] Failed to refresh session_id from DB")

            binary = self.config.claude_code.binary_path
            cmd = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]

            if self.config.claude_code.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            # Model: session override takes priority, else config default
            model = self.model or self.config.claude_code.default_model
            if model:
                cmd.extend(["--model", model])

            # Only resume with a session_id that Claude itself assigned (not our uuid5)
            if self._confirmed_session_id:
                cmd.extend(["--resume", self._confirmed_session_id])

        cwd = self.project_dir
        if not Path(cwd).exists():
            cwd = str(Path.home())

        # Remote SSH wrapping: preserve local project_dir for DB session persistence
        subprocess_cwd = cwd
        if self.remote and not self.is_kimi:
            remote_host = self.config.claude_code.remote_host
            remote_binary = self.config.claude_code.remote_binary_path or "claude"
            if remote_host:
                # Replace the local binary with the remote binary path
                remote_args = [remote_binary] + cmd[1:]
                remote_shell_cmd = shlex.join(remote_args)
                prefix = "export PATH=/opt/homebrew/bin:$PATH"
                if self.remote_project_dir:
                    prefix += f" && cd {shlex.quote(self.remote_project_dir)}"
                remote_shell_cmd = f"{prefix} && {remote_shell_cmd}"
                cmd = [
                    "ssh", "-T",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10",
                    remote_host, remote_shell_cmd,
                ]
                subprocess_cwd = str(Path.home())

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=subprocess_cwd,
            limit=8 * 1024 * 1024,  # 8MB — stream-json events can exceed the 64KB default
        )
        self.is_busy = True
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def _read_loop(self) -> None:
        """Read stdout line by line, parse JSON, notify listeners."""
        try:
            while self.process and self.process.stdout:
                line_bytes = await self.process.stdout.readline()
                if not line_bytes:
                    # Check if process died unexpectedly
                    if self.process.returncode is not None:
                        exit_code = self.process.returncode
                        if exit_code != 0:
                            logger.error(f"[{self.name}] Subprocess exited with code {exit_code}")
                            self._emit(OutputLine(type="error", text=f"[subprocess crashed: exit code {exit_code}]"))
                            # Self-heal: if claude rejected the --resume id, clear it so
                            # the next turn starts fresh instead of looping on the bad id.
                            await self._maybe_clear_stale_session()
                    # For Kimi, synthesize a result event so the UI unlocks
                    if self.is_kimi:
                        parsed = OutputLine(type="result", text="--- Turn complete ---")
                        self.output_buffer.append(parsed)
                        for listener in self.listeners:
                            listener(parsed)
                        self.is_busy = False
                    break

                # Skip very large lines (browser screenshots, base64 image data).
                # These block json.loads() for seconds, freezing the event loop.
                if len(line_bytes) > 500_000:  # 500KB — text results are always under this
                    size_kb = len(line_bytes) // 1024
                    logger.info(f"[{self.name}] Skipping large line ({size_kb}KB, likely image data)")
                    self._emit(OutputLine(type="tool_result", text=f"[image/large content: {size_kb}KB, skipped for display]"))
                    continue

                raw_line = line_bytes.decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue

                # For moderately large lines, parse JSON in a thread to avoid
                # blocking the event loop (and thus Textual's render cycle).
                if len(raw_line) > 100_000:
                    logger.debug(f"[{self.name}] Parsing large line in executor: {len(raw_line)} chars")
                    loop = asyncio.get_running_loop()
                    parsed = await loop.run_in_executor(None, self._parse_line, raw_line)
                else:
                    parsed = self._parse_line(raw_line)
                if parsed:
                    # Capture Claude's actual session_id from the init event
                    if parsed.raw and parsed.raw.get("type") == "system" and parsed.raw.get("subtype") == "init":
                        sid = parsed.raw.get("session_id", "")
                        # Claude CLI 2.x may return prefixed IDs (e.g. "claude-<uuid>")
                        # but --resume requires a bare UUID. Extract the UUID part.
                        if sid:
                            import re
                            m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', sid, re.I)
                            if m:
                                sid = m.group(0)
                        if sid:
                            self._confirmed_session_id = sid
                            self.session_id = sid
                            # Persist so Telegram agent bots can resume the same session
                            # Use same project_name as AgentBot: telegram:claude:{name}
                            db_session_name = f"telegram:claude:{self.name.lower()}" if not self.is_kimi else self.name
                            await self.db.upsert_session(db_session_name, sid, self.project_dir)
                    # Detect claude rejecting --resume and self-heal by clearing the id.
                    # The error arrives as a stdout JSON event, not on stderr or a non-zero exit.
                    if parsed.raw and parsed.raw.get("subtype") == "error_during_execution":
                        await self._maybe_clear_stale_session(parsed.raw.get("errors") or [])
                    self.output_buffer.append(parsed)
                    for listener in self.listeners:
                        listener(parsed)
                    if parsed.type in ("result", "error"):
                        self.is_busy = False
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception(f"[{self.name}] Read loop error")
            self._emit(OutputLine(type="error", text=f"[read error: {e}]"))
        finally:
            self.is_busy = False
            # is_alive stays True — session persists between turns
            if not self._inbox.empty():
                asyncio.create_task(self._drain_inbox())
            # Process any pending user messages that were queued while busy
            if self._pending_messages:
                asyncio.create_task(self._drain_pending_messages())

    async def _stderr_loop(self) -> None:
        """Read stderr, log it, and keep a tail buffer for post-exit diagnosis."""
        try:
            while self.process and self.process.stderr:
                line_bytes = await self.process.stderr.readline()
                if not line_bytes:
                    break
                text = line_bytes.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug(f"[{self.name} stderr] {text}")
        except (asyncio.CancelledError, Exception):
            pass

    async def _maybe_clear_stale_session(self, errors: list | None = None) -> None:
        """If --resume hit a missing session, clear the cached id.

        Without this, every subsequent turn keeps retrying the same bad id and
        the session stays wedged until the user manually restarts the TUI. Looks
        first at the structured error list from claude's stdout event, then
        falls back to the stderr tail for cases where claude exits before
        emitting a result event.
        """
        if not self._confirmed_session_id:
            return
        stale_markers = (
            "no conversation found with session id",
            "session not found",
            "session does not exist",
        )
        haystack = " ".join(errors or []).lower()
        if not haystack:
            haystack = " ".join(self._stderr_tail).lower()
        if not any(m in haystack for m in stale_markers):
            return
        bad_id = self._confirmed_session_id
        logger.warning(
            f"[{self.name}] Claude rejected session_id {bad_id}; clearing so next turn starts fresh."
        )
        self._confirmed_session_id = None
        self.session_id = None
        if self.db:
            try:
                db_session_name = f"telegram:claude:{self.name.lower()}" if not self.is_kimi else self.name
                await self.db.terminate_session(db_session_name)
            except Exception:
                logger.exception(f"[{self.name}] failed to terminate stale session in DB")
        self._emit(OutputLine(
            type="error",
            text=f"[session {bad_id[:8]}… rejected — next message will start fresh]",
        ))

    async def _load_telegram_history(self, limit: int = 50) -> None:
        """Load recent Telegram conversation history into the output buffer."""
        if not self.db:
            return
        source = f"telegram:{self.name}"
        try:
            rows = await self.db.get_messages_since(source, 0)
            # Only keep the most recent N to avoid flooding the buffer
            rows = rows[-limit:] if len(rows) > limit else rows
            for row in rows:
                self._last_telegram_msg_id = row["id"]
                if row["role"] == "user":
                    line = OutputLine(type="user", text=f"[telegram] {row['content']}")
                else:
                    line = OutputLine(type="assistant", text=row["content"])
                self.output_buffer.append(line)
        except Exception:
            pass

    async def _telegram_poll_loop(self) -> None:
        """Poll DB for messages that arrived via Telegram and inject them into the output buffer."""
        source = f"telegram:{self.name}"
        # Seed last_id so we only show messages that arrive after TUI opens
        if self._last_telegram_msg_id == 0:
            try:
                rows = await self.db.get_messages_since(source, 0)
                if rows:
                    self._last_telegram_msg_id = rows[-1]["id"]
            except Exception:
                pass

        while self.is_alive:
            await asyncio.sleep(2)
            try:
                rows = await self.db.get_messages_since(source, self._last_telegram_msg_id)
                for row in rows:
                    self._last_telegram_msg_id = row["id"]
                    if row["role"] == "user":
                        line = OutputLine(type="user", text=f"[telegram] {row['content']}")
                    else:
                        line = OutputLine(type="assistant", text=row["content"])
                    self._emit(line)
            except asyncio.CancelledError:
                return
            except Exception:
                pass

            # Check for agent messages written by external processes (e.g. halos-msg CLI)
            try:
                agent_rows = await self.db.get_pending_agent_messages(self.name)
                if agent_rows:
                    ids = [r["id"] for r in agent_rows]
                    await self.db.mark_agent_messages_delivered(ids)
                    for row in agent_rows:
                        await self.inject_agent_message(row["sender"], row["content"])
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    def _emit(self, line: OutputLine) -> None:
        self.output_buffer.append(line)
        for listener in self.listeners:
            listener(line)

    async def inject_agent_message(self, sender: str, content: str) -> None:
        """Queue an inter-agent message. Displays immediately; fires as a turn when idle."""
        self._emit(OutputLine(type="agent_message", text=f"[From {sender}]: {content}"))
        await self._inbox.put({"sender": sender, "content": content})
        if not self.is_busy and self.is_alive:
            asyncio.create_task(self._drain_inbox())

    async def _drain_inbox(self) -> None:
        """Process one queued agent message. _read_loop.finally re-triggers this if more remain."""
        if self.is_busy or self._inbox.empty():
            return
        item = await self._inbox.get()
        await self.send(f"[Message from {item['sender']}]: {item['content']}")

    async def _drain_pending_messages(self) -> None:
        """Process queued user messages that were sent while busy.
        
        Sends messages one at a time, waiting for each turn to complete.
        """
        while self._pending_messages and not self.is_busy:
            text = self._pending_messages.pop(0)
            await self.send(text)
            # Small delay to allow state to update
            await asyncio.sleep(0.1)

    def _parse_line(self, raw: str) -> OutputLine | None:
        """Parse a stream-json line into an OutputLine."""
        if self.is_kimi:
            return self._parse_kimi_line(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            if raw.strip():
                # Log more details for debugging
                logger.warning(f"[{self.name}] JSON parse error at pos {e.pos}: {raw[:300]}...")
                return OutputLine(type="raw", text=f"[parse error] {raw[:200]}")
            return None

        msg_type = data.get("type", "unknown")

        if msg_type == "system":
            subtype = data.get("subtype", "")
            if subtype == "init":
                sid = data.get("session_id", "")
                return OutputLine(type="system", text=f"Session started (id: {sid[:8]}...)", raw=data)
            elif subtype in ("hook_started", "hook_response"):
                return None  # suppress hook noise
            return OutputLine(type="system", text=f"[system:{subtype}]", raw=data)

        elif msg_type == "assistant":
            message = data.get("message", {})
            content_blocks = message.get("content", [])
            texts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "tool")
                    tool_input = block.get("input", {})
                    # Summarize tool input
                    input_preview = ""
                    if isinstance(tool_input, dict):
                        if "command" in tool_input:
                            input_preview = tool_input["command"][:120]
                        elif "file_path" in tool_input:
                            input_preview = tool_input["file_path"]
                        elif "pattern" in tool_input:
                            input_preview = tool_input["pattern"]
                        else:
                            input_preview = str(tool_input)[:120]
                    texts.append(f"[tool] {tool_name}: {input_preview}")
            full_text = "\n".join(texts)
            if full_text:
                return OutputLine(type="assistant", text=full_text, raw=data)
            return None

        elif msg_type == "result":
            subtype = data.get("subtype", "")
            duration = data.get("duration_ms", 0)
            cost = data.get("total_cost_usd", 0)
            result_text = data.get("result", "")
            if subtype == "success":
                summary = f"--- Turn complete ({duration}ms, ${cost:.4f}) ---"
                return OutputLine(type="result", text=summary, raw=data)
            elif subtype == "error":
                error_text = data.get("error", result_text or "Unknown error")
                return OutputLine(type="error", text=f"[error] {error_text}", raw=data)
            elif subtype == "error_during_execution":
                errors = data.get("errors", [])
                error_text = "; ".join(errors) if errors else result_text or "execution error"
                return OutputLine(type="error", text=f"[error] {error_text}", raw=data)
            return OutputLine(type="result", text=f"[result:{subtype}] {result_text[:200]}", raw=data)

        elif msg_type == "tool_use":
            tool_name = data.get("name", data.get("tool", "tool"))
            return OutputLine(type="tool_use", text=f"[tool_use] {tool_name}", raw=data)

        elif msg_type == "tool_result":
            content = data.get("content", "")

            # Handle structured content blocks (common for Read tool with images)
            if isinstance(content, list):
                text_parts = []
                has_image = False
                for b in content:
                    if isinstance(b, dict):
                        block_type = b.get("type", "")
                        if block_type == "image":
                            has_image = True
                            # Get image metadata if available
                            source = b.get("source", {})
                            media_type = source.get("media_type", "image")
                            data_size = len(source.get("data", "")) if isinstance(source, dict) else 0
                            if data_size > 0:
                                text_parts.append(f"[{media_type}, {data_size} bytes base64]")
                            else:
                                text_parts.append("[image]")
                        elif block_type == "text":
                            text_parts.append(b.get("text", "")[:300])
                        else:
                            # Unknown block type, show limited preview
                            text_parts.append(f"[{block_type}]")

                if has_image and len(text_parts) == 1:
                    # If only an image, that's the whole result
                    preview = " ".join(text_parts)
                else:
                    preview = " ".join(text_parts)[:500]
            else:
                # Plain string content
                preview = str(content)[:500]
                # Detect very large content
                if len(str(content)) > 50000:
                    preview = f"[large: {len(str(content))} chars] {preview[:200]}..."

            return OutputLine(type="tool_result", text=f"[tool_result] {preview}", raw=None)  # Don't keep raw for large results

        elif msg_type == "rate_limit_event":
            return OutputLine(type="error", text="[rate limited] Waiting for rate limit...", raw=data)

        else:
            return OutputLine(type="raw", text=f"[{msg_type}] {str(data)[:200]}", raw=data)

    def _parse_kimi_line(self, raw: str) -> OutputLine | None:
        """Parse Kimi's NDJSON stream format."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if raw.strip():
                return OutputLine(type="raw", text=raw)
            return None

        role = data.get("role", "")
        if role == "assistant":
            content = data.get("content", [])
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "think":
                        texts.append(f"[think] {block.get('think', '')[:200]}")
            for tc in data.get("tool_calls", []):
                fn = tc.get("function", {})
                texts.append(f"[tool] {fn.get('name', 'tool')}")
            full_text = "\n".join(texts)
            if full_text:
                return OutputLine(type="assistant", text=full_text, raw=data)
            return None
        elif role == "tool":
            content = data.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            preview = str(content)[:300]
            return OutputLine(type="tool_result", text=f"[tool_result] {preview}", raw=data)
        else:
            return OutputLine(type="raw", text=f"[{role}] {str(data)[:200]}", raw=data)

    async def interrupt(self) -> None:
        """Kill the current turn's subprocess but keep the session alive."""
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self.process and self.process.returncode is None:
            try:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except Exception:
                pass
        self.is_busy = False
        # Don't resume a session that was interrupted during tool use —
        # Anthropic's backend will reject it with "tool use concurrency" errors.
        self._confirmed_session_id = None
        if self.db:
            try:
                db_session_name = f"telegram:claude:{self.name.lower()}" if not self.is_kimi else self.name
                await self.db.terminate_session(db_session_name)
            except Exception:
                pass
        self._emit(OutputLine(type="error", text="[Interrupted]"))

    async def kill(self) -> None:
        """Terminate the subprocess."""
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._telegram_poll_task:
            self._telegram_poll_task.cancel()
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
            except Exception:
                pass
        self.is_alive = False
        self.is_busy = False
        self._confirmed_session_id = None


# ---------------------------------------------------------------------------
# SessionManager -- dict of SessionProcess instances
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages all Claude Code and Kimi session subprocesses."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._sessions: dict[str, SessionProcess] = {}
        self.engine_override: dict[str, str] = {}  # session_name -> "claude" | "kimi"
        # Build project map from config
        self.project_map: dict[str, str] = {}
        if config.claude_code.projects:
            for name, path in config.claude_code.projects.items():
                self.project_map[name] = str(Path(path).expanduser())

    def session_id_for(self, name: str) -> str:
        """Generate a deterministic UUID from the project name."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"halos.{name}"))

    def get(self, name: str) -> SessionProcess | None:
        return self._sessions.get(name)

    def list_names(self) -> list[str]:
        return [n for n, sp in self._sessions.items() if sp.is_alive]

    async def get_or_create(self, name: str) -> SessionProcess:
        """Get existing session or create a new one."""
        if name in self._sessions and self._sessions[name].is_alive:
            return self._sessions[name]

        # Gate to configured names only. Stale callers (e.g. "general"/"halos")
        # silently spawned zombie poll loops that hammered SQLite for 16 h.
        known = {n.lower() for n in self.config.agents.keys()} | {
            n.lower() for n in self.config.claude_code.projects.keys()
        }
        if name.lower() not in known:
            raise ValueError(
                f"Unknown session {name!r}: not in config.agents or config.claude_code.projects"
            )

        project_dir = self.project_map.get(name, str(Path.home()))
        session_id = self.session_id_for(name)
        from_db = False  # Track if session_id came from database

        # Check DB for existing session and engine preference
        # Look for both old format (name) and new format (telegram:claude:{name}) for backwards compat
        active_sessions = await self.db.get_active_sessions()
        db_session_name = f"telegram:claude:{name.lower()}"
        
        # First priority: look for new format (telegram:claude:{name})
        # Second priority: look for old format (name)
        # Third priority: look for lowercase variant
        found_session = None
        for s in active_sessions:
            if s["name"] == db_session_name:
                found_session = s
                break
        if not found_session:
            for s in active_sessions:
                if s["name"] == name:
                    found_session = s
                    break
        if not found_session:
            for s in active_sessions:
                if s["name"].lower() == name.lower():
                    found_session = s
                    break
        
        if found_session:
            session_id = found_session["session_id"]
            project_dir = found_session.get("project_dir", project_dir)
            from_db = True  # This session_id is from a previous session
            if found_session.get("engine") == "kimi":
                self.engine_override[name] = "kimi"

        is_kimi = self.engine_override.get(name) == "kimi"
        if is_kimi:
            session_id = f"kimi-{session_id}"

        binary = "kimi" if is_kimi else self.config.claude_code.binary_path

        # Look up remote settings from agents config (match by case-insensitive name)
        remote = False
        remote_project_dir = ""
        name_lower = name.lower()
        for agent_name, agent_cfg in self.config.agents.items():
            if agent_cfg.remote and agent_name.lower() == name_lower:
                remote = True
                remote_project_dir = agent_cfg.remote_project_dir
                break

        sp = SessionProcess(
            name, project_dir, session_id, self.config, self.db,
            binary=binary, from_db=from_db,
            remote=remote, remote_project_dir=remote_project_dir,
        )
        self._sessions[name] = sp
        return sp

    async def start_session(self, name: str, initial_prompt: str | None = None) -> SessionProcess:
        """Create and start a session subprocess."""
        sp = await self.get_or_create(name)
        if not sp.is_alive:
            await sp.start(initial_prompt)
            # Persist to DB with engine - use same naming as AgentBot
            # IMPORTANT: Normalize to lowercase to match agent_bot.py
            engine = self.engine_override.get(name, "claude")
            db_session_name = f"telegram:claude:{name.lower()}" if engine == "claude" else name
            await self.db.upsert_session(db_session_name, sp.session_id, sp.project_dir, engine=engine)
        return sp

    async def kill_session(self, name: str) -> None:
        sp = self._sessions.get(name)
        if sp:
            await sp.kill()
            # Use same naming as AgentBot for Claude sessions
            db_session_name = f"telegram:claude:{name.lower()}" if not sp.is_kimi else name
            await self.db.terminate_session(db_session_name)
            # Drop the dead entry so it isn't replayed by poll loops or list_names.
            self._sessions.pop(name, None)

    async def kill_all(self) -> None:
        for name in list(self._sessions.keys()):
            await self.kill_session(name)

    async def shutdown(self) -> None:
        """Stop all subprocesses on TUI exit WITHOUT terminating DB session rows.

        Preserves session_id → conversation continuity for next TUI run and for
        Telegram agent bots. Use kill_all() or kill_session() for explicit termination.
        """
        for name, sp in list(self._sessions.items()):
            try:
                await sp.kill()
            except Exception:
                logger.exception(f"Error killing session {name} during shutdown")

    def get_status(self, name: str) -> str:
        """Return status string for a session."""
        sp = self._sessions.get(name)
        if sp and sp.is_alive:
            if sp.is_busy:
                return "running"
            return "live"
        return "idle"

    def all_names(self) -> list[str]:
        """All known session names from config."""
        return list(self.project_map.keys())


# ---------------------------------------------------------------------------
# Custom messages for Textual
# ---------------------------------------------------------------------------

class OutputReceived(Message):
    """Fired when a session produces output."""
    def __init__(self, session_name: str, line: OutputLine) -> None:
        super().__init__()
        self.session_name = session_name
        self.line = line


class SessionStatusChanged(Message):
    """Fired when a session's status changes."""
    def __init__(self, session_name: str, status: str) -> None:
        super().__init__()
        self.session_name = session_name
        self.status = status


# ---------------------------------------------------------------------------
# Sidebar widgets
# ---------------------------------------------------------------------------

class SessionListItem(ListItem):
    """A single session entry in the sidebar."""

    def __init__(self, name: str, status: str = "idle") -> None:
        super().__init__()
        self.session_name = name
        self.status = status

    def _render_label(self, status: str) -> str:
        return {"live": "[green]●[/]", "running": "[yellow]●[/]", "idle": "[dim]○[/]"}.get(
            status, "[dim]○[/]"
        ) + f" {self.session_name}"

    def compose(self) -> ComposeResult:
        yield Static(self._render_label(self.status), classes="session-label")

    def update_status(self, status: str, is_active: bool = False) -> None:
        self.status = status
        self.set_class(is_active, "-active")
        try:
            label = self.query_one(".session-label", Static)
            label.update(self._render_label(status))
        except Exception:
            pass


class QuickSessionButton(Static):
    """Clickable session button shown in the quick-session-bar when sidebar is collapsed."""

    def __init__(self, session_name: str, status: str = "idle", is_active: bool = False) -> None:
        super().__init__(classes="quick-session-btn")
        self.session_name = session_name
        self.status = status
        self.update(self._make_label(status))
        if is_active:
            self.add_class("-active")

    def _make_label(self, status: str) -> str:
        dot = {"live": "[green]●[/]", "running": "[yellow]●[/]", "idle": "[dim]○[/]"}.get(
            status, "[dim]○[/]"
        )
        return f" {dot} {self.session_name} "

    def update_status(self, status: str, is_active: bool = False) -> None:
        self.status = status
        self.set_class(is_active, "-active")
        self.update(self._make_label(status))


class CronListItem(ListItem):
    """A single cron job entry in the sidebar."""

    def __init__(self, task: dict) -> None:
        super().__init__()
        self.task_data = task
        self.task_name = task["name"]

    def _get_description(self) -> str:
        """Extract human-readable description from task payload or generate from name."""
        import json
        
        payload = self.task_data.get("payload", {}) or {}
        # Parse payload if it's a JSON string
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        
        # Try description field first, then prompt
        desc = payload.get("description", "")
        if not desc:
            desc = payload.get("prompt", "")
        
        # If we have a description, clean it up
        if desc:
            # Remove newlines and extra spaces
            desc = " ".join(desc.split())
            # Truncate long descriptions (allow more characters)
            if len(desc) > 90:
                desc = desc[:87] + "..."
            return desc
        
        # Generate description from task name if no description field
        name = self.task_name.lower()
        name_clean = name.replace("_", " ").replace("-", " ")
        
        # Map common task name patterns to descriptions
        if "reddit" in name:
            if "scout" in name or "scouting" in name:
                return "Scout Reddit for engagement opportunities"
            return "Reddit-related task"
        elif "twitter" in name or "tweet" in name:
            if "scout" in name:
                return "Scout Twitter for reply opportunities"
            return "Generate or post tweets"
        elif "follow" in name:
            return "Follow accounts on social media"
        elif "morning" in name and "brief" in name:
            return "Morning briefing with calendar, emails, messages"
        elif "meat" in name and "remind" in name:
            return "Reminder to take out meat for dinner"
        elif "cardio" in name:
            return "Cardio workout reminder"
        elif "calendar" in name:
            return "Calendar refresh and reminder scheduling"
        elif "kb" in name or "knowledge" in name:
            return "Knowledge base maintenance"
        elif "analytics" in name:
            return "Check analytics and metrics"
        elif "research" in name:
            return "Research and signal hunting"
        elif "publisher" in name:
            return "Publish approved content"
        elif "health" in name:
            return "Health check"
        elif "digest" in name:
            return "Daily digest/summary"
        elif "remind" in name:
            return "Reminder task"
        elif "weekly" in name:
            return "Weekly summary/preview"
        elif "gift" in name and "radar" in name:
            return "Gift radar tracking"
        elif "birthday" in name:
            return "Birthday reminder"
        elif "indie" in name or "hn" in name or "hacker" in name:
            return "Check Indie Hackers / Hacker News"
        elif "project" in name and "summary" in name:
            return "Weekly project summary"
        elif "urine" in name:
            return "Medical sample reminder"
        elif "sample" in name:
            return "Sample/reminder task"
        
        # Default: use task type if available
        task_type = self.task_data.get("task_type", "custom")
        if task_type == "health_check":
            return "Health check"
        elif task_type == "digest":
            return "Daily digest"
        elif task_type != "custom":
            return f"{task_type.replace('_', ' ').title()} task"
        
        return ""

    def _get_cron_human(self, cron: str) -> str:
        """Convert cron expression to human-readable format."""
        if cron == "0 * * * *":
            return "hourly"
        elif cron == "0 0 * * *":
            return "daily @ midnight"
        elif cron == "0 6 * * *":
            return "daily @ 6am"
        elif cron == "0 7 * * *":
            return "daily @ 7am"
        elif cron == "0 8 * * *":
            return "daily @ 8am"
        elif cron == "0 9 * * *":
            return "daily @ 9am"
        elif cron == "0 12 * * *":
            return "daily @ noon"
        elif cron == "0 15 * * *":
            return "daily @ 3pm"
        elif cron == "0 17 * * *":
            return "daily @ 5pm"
        elif cron == "0 18 * * *":
            return "daily @ 6pm"
        elif cron == "0 19 * * *":
            return "daily @ 7pm"
        elif cron == "0 20 * * *":
            return "daily @ 8pm"
        elif cron == "0 21 * * *":
            return "daily @ 9pm"
        elif cron == "0 22 * * *":
            return "daily @ 10pm"
        elif cron == "0 * * * *":
            return "hourly"
        elif cron == "*/30 * * * *":
            return "every 30 min"
        elif cron == "0 0 * * 0":
            return "sundays @ midnight"
        elif cron == "0 20 * * 0":
            return "sundays @ 8pm"
        elif cron == "0 21 * * 0":
            return "sundays @ 9pm"
        elif cron == "0 15 * * 3":
            return "wednesdays @ 3pm"
        elif cron == "0 18 * * 5":
            return "fridays @ 6pm"
        elif cron.startswith("0 "):
            parts = cron.split()
            if len(parts) == 5:
                return f"daily @ {parts[1]}:{parts[0]}"
        return cron

    def _get_session(self) -> str:
        """Extract which agent/session this task runs under."""
        payload = self.task_data.get("payload", {}) or {}
        import json
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return ""
        session = payload.get("session", "")
        if session:
            return session.replace("beta-", "").replace("Beta-", "").title()
        return ""

    def compose(self) -> ComposeResult:
        enabled = self.task_data.get("enabled", False)
        status = "[green]●[/]" if enabled else "[red]●[/]"
        cron = self.task_data.get("cron_expression", "?")
        cron_human = self._get_cron_human(cron)
        desc = self._get_description()
        session = self._get_session()
        
        # First line: status + name + schedule
        name_display = self.task_name.replace("_", " ")
        if len(name_display) > 25:
            name_display = name_display[:22] + "..."
        line1 = f"{status} [b]{name_display}[/b]"
        yield Static(line1, classes="cron-label")
        
        # Second line: description only
        if desc:
            line2 = f"    [dim]{desc}[/dim]"
        else:
            # Fallback: show session and cron if no description
            parts = []
            if session:
                parts.append(f"[@{session}]")
            parts.append(f"({cron_human})")
            line2 = "    [dim]" + " ".join(parts) + "[/dim]"
        yield Static(line2, classes="cron-desc")

    def update_task(self, task: dict) -> None:
        self.task_data = task
        enabled = task.get("enabled", False)
        status = "[green]●[/]" if enabled else "[red]●[/]"
        cron = task.get("cron_expression", "?")
        cron_human = self._get_cron_human(cron)
        desc = self._get_description()
        session = self._get_session()
        name_display = self.task_name.replace("_", " ")
        if len(name_display) > 25:
            name_display = name_display[:22] + "..."
        
        try:
            label = self.query_one(".cron-label", Static)
            label.update(f"{status} [b]{name_display}[/b]")
            
            # Update description line
            desc_labels = self.query(".cron-desc")
            if desc_labels:
                if desc:
                    line2 = f"    [dim]{desc}[/dim]"
                else:
                    # Fallback: show session and cron if no description
                    parts = []
                    if session:
                        parts.append(f"[@{session}]")
                    parts.append(f"({cron_human})")
                    line2 = "    [dim]" + " ".join(parts) + "[/dim]"
                desc_labels[0].update(line2)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------

class NewTaskScreen(ModalScreen[Optional[dict]]):
    """Modal dialog for creating a new scheduled task."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    NewTaskScreen {
        align: center middle;
    }
    #new-task-dialog {
        width: 70;
        height: auto;
        max-height: 35;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #new-task-dialog Label {
        margin-top: 1;
    }
    #new-task-dialog Input {
        margin-bottom: 0;
    }
    #task-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, session_names: list[str]) -> None:
        super().__init__()
        self.session_names = session_names

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            yield Label("[b]New Scheduled Task[/b]")
            yield Label("Name:")
            yield Input(placeholder="task-name", id="task-name-input")
            yield Label("Cron expression:")
            yield Input(placeholder="0 9 * * *", id="task-cron-input")
            yield Label("Type:")
            yield Select(
                [
                    ("custom", "custom"),
                    ("health_check", "health_check"),
                    ("digest", "digest"),
                ],
                value="custom",
                id="task-type-select",
            )
            yield Label("Session (agent):")
            session_options = [("None", "")] + [(s, s) for s in self.session_names]
            yield Select(session_options, value="", id="task-session-select")
            yield Label("Description / prompt:")
            yield Input(placeholder="What should this task do?", id="task-desc-input")
            with Horizontal(id="task-buttons"):
                yield Static("[b][Enter][/b] Create  [b][Esc][/b] Cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name_input = self.query_one("#task-name-input", Input)
        cron_input = self.query_one("#task-cron-input", Input)
        type_select = self.query_one("#task-type-select", Select)
        session_select = self.query_one("#task-session-select", Select)
        desc_input = self.query_one("#task-desc-input", Input)

        name = name_input.value.strip()
        cron = cron_input.value.strip()

        if not name or not cron:
            return

        result = {
            "name": name,
            "cron": cron,
            "task_type": str(type_select.value),
            "session": str(session_select.value) if session_select.value else "",
            "description": desc_input.value.strip(),
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditTaskScreen(ModalScreen[Optional[dict]]):
    """Modal dialog for editing an existing scheduled task."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    EditTaskScreen {
        align: center middle;
    }
    #edit-task-dialog {
        width: 70;
        height: auto;
        max-height: 35;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #edit-task-dialog Label {
        margin-top: 1;
    }
    #edit-task-dialog Input {
        margin-bottom: 0;
    }
    #task-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, task_data: dict, session_names: list[str]) -> None:
        super().__init__()
        self.task_data = task_data
        self.session_names = session_names

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-task-dialog"):
            yield Label("[b]Edit Scheduled Task[/b]")
            yield Label("Name:")
            yield Input(
                value=self.task_data.get("name", ""),
                placeholder="task-name",
                id="task-name-input",
                disabled=True,  # Name cannot be changed
            )
            yield Label("Cron expression:")
            yield Input(
                value=self.task_data.get("cron_expression", ""),
                placeholder="0 9 * * *",
                id="task-cron-input",
            )
            yield Label("Type:")
            current_type = self.task_data.get("task_type", "custom")
            yield Select(
                [
                    ("custom", "custom"),
                    ("health_check", "health_check"),
                    ("digest", "digest"),
                ],
                value=current_type,
                id="task-type-select",
            )
            yield Label("Session (agent):")
            payload = self.task_data.get("payload", {}) or {}
            if isinstance(payload, str):
                try:
                    import json
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            current_session = payload.get("session", "")
            session_options = [("None", "")] + [(s, s) for s in self.session_names]
            # Only set value if it exists in options (case-insensitive); otherwise use blank
            session_lower = current_session.lower()
            matching_session = next((s for s in self.session_names if s.lower() == session_lower), "")
            select_value = matching_session
            yield Select(session_options, value=select_value, id="task-session-select")
            yield Label("Description / prompt:")
            current_desc = payload.get("description", "")
            yield Input(
                value=current_desc,
                placeholder="What should this task do?",
                id="task-desc-input",
            )
            with Horizontal(id="task-buttons"):
                yield Static("[b][Enter][/b] Save  [b][Esc][/b] Cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name_input = self.query_one("#task-name-input", Input)
        cron_input = self.query_one("#task-cron-input", Input)
        type_select = self.query_one("#task-type-select", Select)
        session_select = self.query_one("#task-session-select", Select)
        desc_input = self.query_one("#task-desc-input", Input)

        name = name_input.value.strip()
        cron = cron_input.value.strip()

        if not name or not cron:
            return

        result = {
            "name": name,
            "cron": cron,
            "task_type": str(type_select.value),
            "session": str(session_select.value) if session_select.value else "",
            "description": desc_input.value.strip(),
            "enabled": self.task_data.get("enabled", True),
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DeleteConfirmScreen(ModalScreen[bool]):
    """Modal dialog for confirming task deletion."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    DEFAULT_CSS = """
    DeleteConfirmScreen {
        align: center middle;
    }
    #delete-dialog {
        width: 50;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #delete-dialog Label {
        text-align: center;
    }
    #delete-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, task_name: str) -> None:
        super().__init__()
        self.task_name = task_name

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Label(f"[b]Delete task '{self.task_name}'?[/b]")
            yield Label("[dim]This cannot be undone.[/dim]")
            with Horizontal(id="delete-buttons"):
                yield Static("[b][Y][/b] Yes  [b][N/Esc][/b] No")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class FileEditorScreen(ModalScreen[None]):
    """Full-screen editor for a session file (claude.md, soul.md, etc.)."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    FileEditorScreen {
        align: center middle;
    }
    #editor-container {
        width: 90%;
        height: 85%;
        border: thick $accent;
        background: $surface;
        padding: 0;
    }
    #editor-title {
        background: $accent;
        color: $text;
        padding: 0 1;
        height: 1;
    }
    #editor-textarea {
        width: 100%;
        height: 1fr;
    }
    #editor-footer {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, file_path: Path) -> None:
        super().__init__()
        self.file_path = file_path
        self._original = ""

    def compose(self) -> ComposeResult:
        content = self.file_path.read_text() if self.file_path.exists() else ""
        self._original = content
        with Vertical(id="editor-container"):
            yield Static(f" {self.file_path.name}", id="editor-title")
            yield TextArea(content, id="editor-textarea", language="markdown")
            yield Static(
                "[b]Ctrl+S[/b] Save  [b]Esc[/b] Cancel  "
                f"[dim]{self.file_path}[/dim]",
                id="editor-footer",
            )

    def action_save(self) -> None:
        text = self.query_one("#editor-textarea", TextArea).text
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(text)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Key binding help screen."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("f1", "close", "Close"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 50;
        height: auto;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("[b]HalOS TUI -- Key Bindings[/b]\n")
            yield Static(
                "[b]F1[/b]       Help (this screen)\n"
                "[b]F2[/b]       Start session (select from list)\n"
                "[b]F3[/b]       Kill active session\n"
                "[b]F4[/b]       New scheduled task\n"
                "[b]F5[/b]       Refresh sidebar\n"
                "[b]Tab[/b]      Switch focus\n"
                "[b]Ctrl+N[/b]   Next session\n"
                "[b]Ctrl+P[/b]   Previous session\n"
                "[b]Enter[/b]    Toggle task / Switch to session\n"
                "[b]Ctrl+C[/b]   Quit (kills all sessions)\n"
                "[b]q[/b]        Quit (when sidebar focused)\n"
                "[b]e[/b]        Edit highlighted item (session file or task)\n"
                "[b]d[/b]        Delete highlighted task\n\n"
                "[b]Tasks:[/b] Click to select, [b]Enter[/b] to toggle, [b]e[/b] to edit, [b]d[/b] to delete\n\n"
                "[b]/model <name>[/b]     Switch model (haiku|sonnet|opus); context preserved\n"
                "[b]/switch kimi[/b]     Switch active session to Kimi CLI\n"
                "[b]/switch claude[/b]   Switch active session back to Claude Code\n"
                "[b]/edit <file>[/b]     Edit session file\n"
                "[dim]  files: claude.md soul.md[/dim]\n"
                "[b]/telegram <token>[/b] Configure Telegram bot\n"
                "[b]/reload[/b]          Restart TUI\n"
                "[b]/restart[/b]         Restart daemon\n"
            )
            yield Static("[dim]Press Esc or F1 to close[/dim]")

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmKillScreen(ModalScreen[bool]):
    """Confirm kill dialog."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    DEFAULT_CSS = """
    ConfirmKillScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 40;
        height: 7;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, session_name: str) -> None:
        super().__init__()
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(f"[b]Kill session [red]{self.session_name}[/red]?[/b]")
            yield Static("[b]y[/b] Yes   [b]n[/b] No   [b]Esc[/b] Cancel")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SessionFilePickerScreen(ModalScreen[Optional[str]]):
    """Pick a file to edit from a session directory."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SessionFilePickerScreen {
        align: center middle;
    }
    #file-picker-dialog {
        width: 44;
        height: auto;
        max-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #file-picker-list {
        height: auto;
        max-height: 12;
    }
    """

    def __init__(self, session_name: str, project_dir: Path, known_files: dict) -> None:
        super().__init__()
        self.session_name = session_name
        self.project_dir = project_dir
        self.known_files = known_files  # {filename: template}

    def compose(self) -> ComposeResult:
        with Vertical(id="file-picker-dialog"):
            yield Label(f"[b]Edit file — {self.session_name}[/b]")
            items = []
            for fname in self.known_files:
                exists = (self.project_dir / fname).exists()
                tag = "[green]●[/]" if exists else "[dim]○[/]"
                items.append((f"{tag} {fname}", fname))
            yield Select(items, prompt="Choose file...", id="file-select")
            yield Static("[b][Enter][/b] Open  [b][Esc][/b] Cancel")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value and event.value != Select.BLANK:
            self.dismiss(str(event.value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class StartSessionScreen(ModalScreen[Optional[str]]):
    """Select a project to start a session for."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    StartSessionScreen {
        align: center middle;
    }
    #start-dialog {
        width: 40;
        height: auto;
        max-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, project_names: list[str]) -> None:
        super().__init__()
        self.project_names = project_names

    def compose(self) -> ComposeResult:
        with Vertical(id="start-dialog"):
            yield Label("[b]Start Session[/b]")
            yield Select(
                [(name, name) for name in self.project_names],
                prompt="Select project...",
                id="project-select",
            )
            yield Static("[b][Enter][/b] Start  [b][Esc][/b] Cancel")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value and event.value != Select.BLANK:
            self.dismiss(str(event.value))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main TUI App
# ---------------------------------------------------------------------------

class HalOSTUI(App):
    """HalOS Terminal Dashboard."""

    TITLE = "HalOS"
    SUB_TITLE = "Claude Code Session Manager"

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 1fr;
    }

    #sidebar {
        width: 33;
        border-right: solid $primary-background;
        height: 100%;
    }

    #sidebar.collapsed {
        display: none;
    }

    #sidebar-collapse-btn {
        color: $text;
        text-style: bold;
        padding: 0 1;
        background: $boost;
        width: 100%;
        height: 1;
        content-align: right middle;
    }

    #sidebar-collapse-btn:hover {
        background: $accent;
        color: $text;
    }

    #quick-session-bar {
        height: 1;
        width: 100%;
        background: $primary-background;
        display: none;
        layout: horizontal;
    }

    #quick-session-bar.visible {
        display: block;
    }

    #quick-expand-btn {
        padding: 0 1;
        background: $boost;
        color: $text;
        text-style: bold;
        width: auto;
        height: 1;
    }

    #quick-expand-btn:hover {
        background: $accent;
    }

    .quick-session-btn {
        padding: 0 1;
        width: auto;
        height: 1;
        color: $text;
    }

    .quick-session-btn:hover {
        background: $boost;
    }

    .quick-session-btn.-active {
        color: $accent;
        text-style: bold;
        background: $boost;
    }

    #sidebar-sessions-header {
        color: $text;
        text-style: bold;
        padding: 0 1;
        background: $primary-background;
        width: 100%;
        height: 1;
    }

    #session-list {
        height: auto;
        max-height: 45%;
        min-height: 5;
    }

    #sidebar-cron-header {
        color: $text;
        text-style: bold;
        padding: 0 1;
        background: $primary-background;
        width: 100%;
        height: 1;
    }

    #cron-list {
        height: 1fr;
        min-height: 10;
    }

    #sidebar-status {
        dock: bottom;
        height: 3;
        padding: 0 1;
        color: $text-muted;
    }

    #main-pane {
        width: 1fr;
        height: 100%;
    }

    #output-pane {
        height: 1fr;
        border-bottom: solid $primary-background;
    }

    #input-bar {
        height: 4;
        dock: bottom;
    }

    #message-input {
        width: 1fr;
        height: 4;
        border: none;
    }

    Footer {
        dock: top;
    }

    #input-session-label {
        width: auto;
        min-width: 12;
        padding: 0 1;
        color: $text-muted;
        content-align: right middle;
    }

    .session-label {
        padding: 0 1;
    }

    .cron-label {
        padding: 0 1;
        height: 1;
    }

    .cron-desc {
        padding: 0 1;
        height: 1;
        color: $text-muted;
    }

    ListItem {
        height: auto;
        min-height: 1;
    }

    CronListItem {
        height: auto;
        min-height: 2;
        max-height: 4;
    }

    ListView > ListItem.--highlight {
        background: $accent;
    }

    SessionListItem.-active > .session-label {
        color: $accent;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "start_session", "Start"),
        Binding("f3", "kill_session", "Kill"),
        Binding("f4", "new_task", "New Task"),
        Binding("f5", "refresh", "Refresh"),
        Binding("enter", "toggle_selected", "Select/Toggle", show=False),
        Binding("escape", "interrupt", "Interrupt", show=False),
        Binding("e", "edit_selected", "Edit", show=True),
        Binding("d", "delete_selected", "Delete", show=True),
        Binding("ctrl+n", "next_session", "Next", show=False),
        Binding("ctrl+p", "prev_session", "Prev", show=False),
        Binding("ctrl+c", "copy_output", "Copy", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar", show=True, priority=True),
    ]

    active_session: reactive[str | None] = reactive(None)

    def __init__(self) -> None:
        super().__init__()
        self.config: Config | None = None
        self.db: Database | None = None
        self.session_mgr: SessionManager | None = None
        self._session_names: list[str] = []
        self._db_sessions: list[dict] = []
        self._reload_requested: bool = False
        self._switch_context: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            with Vertical(id="sidebar"):
                yield Static("◀ Collapse", id="sidebar-collapse-btn", markup=True)
                yield Static("SESSIONS", id="sidebar-sessions-header")
                yield ListView(id="session-list")
                yield Static("TASKS", id="sidebar-cron-header")
                yield ListView(id="cron-list")
                yield Static("", id="sidebar-status")
            with Vertical(id="main-pane"):
                with Horizontal(id="quick-session-bar"):
                    yield Static("▶", id="quick-expand-btn", markup=True)
                output = SelectableRichLog(
                    id="output-pane",
                    highlight=True,
                    markup=True,
                    wrap=True,
                    auto_scroll=True,
                )
                output.can_focus = False
                yield output
                with Horizontal(id="input-bar"):
                    yield SessionInput(
                        placeholder="Type a message... (Enter to send)",
                        id="message-input",
                        disabled=True,
                    )
                    yield Static("[dim]no session[/dim]", id="input-session-label")
        yield Footer()

    async def on_mount(self) -> None:
        """Initialize config, DB, and populate sidebar."""
        # Load config relative to project root
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        self.config = load_config(str(config_path))
        self.title = f"HalOS"
        self.sub_title = "Dashboard"

        # Connect to DB
        self.db = Database(self.config.db_path)
        await self.db.connect()

        # Session manager
        self.session_mgr = SessionManager(self.config, self.db)

        # Populate sidebar
        await self._populate_sessions()
        await self._populate_cron()
        self._update_status_bar()

        # Auto-start telegram poll loops for every known session so that
        # messages arriving via AgentBot (Telegram) show up in the TUI output
        # pane even when the user hasn't explicitly opened that session. A
        # plain sp.start() does NOT spawn a claude subprocess — it only flips
        # is_alive, loads recent telegram history into the output buffer and
        # launches the _telegram_poll_task. We deliberately avoid
        # start_session() here because that upserts to DB and would pollute
        # fresh rows with the uuid5 placeholder session_id.
        for name in list(self._session_names):
            try:
                sp = await self.session_mgr.get_or_create(name)
                if not sp.is_alive:
                    await sp.start()
            except Exception:
                logger.exception(f"Failed to auto-start telegram poll for {name}")

        # Show welcome
        output = self.query_one("#output-pane", SelectableRichLog)
        output.write("[b]HalOS Dashboard[/b]")
        output.write("[dim]Select a session from the sidebar to begin.[/dim]")
        output.write("[dim]Press F1 for help, F2 to start a session.[/dim]")

        # Periodic status refresh
        self.set_interval(15, self._periodic_refresh)

    async def _populate_sessions(self) -> None:
        """Populate the session list from config + DB."""
        session_list = self.query_one("#session-list", ListView)
        await session_list.clear()

        self._session_names = self.session_mgr.all_names() if self.session_mgr else []

        # Also check DB for active sessions
        self._db_sessions = await self.db.get_active_sessions() if self.db else []
        db_names = {s["name"] for s in self._db_sessions}

        # Restore engine preferences from DB
        if self.session_mgr:
            for s in self._db_sessions:
                if s.get("engine") == "kimi":
                    self.session_mgr.engine_override[s["name"]] = "kimi"

        # Add any DB sessions not in config (skip synthetic telegram/kimi backends)
        seen_lower = {n.lower() for n in self._session_names}
        for name in db_names:
            if not name.startswith(("telegram:", "kimi:")) and name.lower() not in seen_lower:
                self._session_names.append(name)
                seen_lower.add(name.lower())

        for name in self._session_names:
            status = self.session_mgr.get_status(name) if self.session_mgr else "idle"
            if status == "idle" and name in db_names:
                status = "idle"  # was active in DB but no subprocess
            item = SessionListItem(name, status)
            await session_list.append(item)
            if name == self.active_session:
                item.update_status(status, is_active=True)

        await self._populate_quick_session_bar()

    async def _populate_quick_session_bar(self) -> None:
        """Populate the quick-session-bar (visible when sidebar is collapsed)."""
        try:
            quick_bar = self.query_one("#quick-session-bar")
        except Exception:
            return
        # Remove existing session buttons (keep the expand button)
        for child in list(quick_bar.children):
            if child.id != "quick-expand-btn":
                await child.remove()
        for name in self._session_names:
            status = self.session_mgr.get_status(name) if self.session_mgr else "idle"
            is_active = name == self.active_session
            await quick_bar.mount(QuickSessionButton(name, status, is_active))

    async def _populate_cron(self) -> None:
        """Populate the cron job list from DB."""
        cron_list = self.query_one("#cron-list", ListView)
        await cron_list.clear()

        if not self.db:
            return

        tasks = await self.db.get_scheduled_tasks()
        for task in tasks:
            await cron_list.append(CronListItem(task))

    def _update_status_bar(self) -> None:
        """Update the sidebar status area."""
        status = self.query_one("#sidebar-status", Static)
        live_count = 0
        if self.session_mgr:
            for name in self._session_names:
                if self.session_mgr.get_status(name) in ("live", "running"):
                    live_count += 1
        total = len(self._session_names)
        status.update(f"[dim]Sessions: {live_count}/{total}[/dim]")

    def _update_session_statuses(self) -> None:
        """Refresh all session status indicators in sidebar and quick-bar."""
        session_list = self.query_one("#session-list", ListView)
        for item in session_list.children:
            if isinstance(item, SessionListItem) and self.session_mgr:
                new_status = self.session_mgr.get_status(item.session_name)
                is_active = item.session_name == self.active_session
                item.update_status(new_status, is_active=is_active)
        try:
            quick_bar = self.query_one("#quick-session-bar")
            for btn in quick_bar.children:
                if isinstance(btn, QuickSessionButton) and self.session_mgr:
                    new_status = self.session_mgr.get_status(btn.session_name)
                    is_active = btn.session_name == self.active_session
                    btn.update_status(new_status, is_active=is_active)
        except Exception:
            pass
        self._update_status_bar()

    async def _periodic_refresh(self) -> None:
        """Periodic background refresh."""
        self._update_session_statuses()

    # ---- Session selection ----

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle click/selection on a session or cron item."""
        item = event.item
        if isinstance(item, SessionListItem):
            await self._switch_to_session(item.session_name)
        elif isinstance(item, CronListItem):
            # Clicking a task just highlights it; use Enter to toggle
            pass

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Handle when an item is highlighted (arrow key navigation)."""
        # This event fires when highlight changes via keyboard navigation
        pass  # Just track the highlight for Enter key handling

    async def action_toggle_selected(self) -> None:
        """Toggle the currently selected/highlighted task or switch to session."""
        # Check cron list first (try highlighted_child first for click selection)
        try:
            cron_list = self.query_one("#cron-list", ListView)
            highlighted = cron_list.highlighted_child
            if isinstance(highlighted, CronListItem):
                await self._toggle_cron(highlighted)
                return
        except Exception:
            pass

        # Try by index for keyboard navigation
        try:
            cron_list = self.query_one("#cron-list", ListView)
            if cron_list.children:
                index = cron_list.index
                if index is not None and 0 <= index < len(cron_list.children):
                    item = cron_list.children[index]
                    if isinstance(item, CronListItem):
                        await self._toggle_cron(item)
                        return
        except Exception:
            pass

        # Fallback for session list - switch to session
        try:
            session_list = self.query_one("#session-list", ListView)
            highlighted = session_list.highlighted_child
            if isinstance(highlighted, SessionListItem):
                await self._switch_to_session(highlighted.session_name)
                return
        except Exception:
            pass

    async def _switch_to_session(self, name: str) -> None:
        """Switch the main pane to show the given session."""
        if not self.session_mgr:
            return

        # Detach listener from current session
        if self.active_session:
            old_sp = self.session_mgr.get(self.active_session)
            if old_sp:
                old_sp.listeners = [
                    l for l in old_sp.listeners if l != self._on_session_output
                ]

        self.active_session = name
        sp = await self.session_mgr.get_or_create(name)
        if not sp.is_alive:
            await sp.start()

        # Update output pane
        output = self.query_one("#output-pane", SelectableRichLog)
        output.clear()

        # Replay buffer (includes Telegram history loaded by start())
        for line in sp.output_buffer:
            self._render_line(output, line)
        sp.listeners.append(self._on_session_output)

        # Update input bar
        self._update_input_state()
        self._update_session_statuses()

        # Update header
        self.sub_title = name

    def _on_session_output(self, line: OutputLine) -> None:
        """Callback when the active session produces output."""
        # This is called from the asyncio read loop; post to the UI thread
        self.call_later(self._handle_output_line, line)

    def _handle_output_line(self, line: OutputLine) -> None:
        """Handle an output line on the UI thread."""
        if not self.active_session:
            return
        sp = self.session_mgr.get(self.active_session) if self.session_mgr else None
        if sp and self._on_session_output in sp.listeners:
            output = self.query_one("#output-pane", SelectableRichLog)
            self._render_line(output, line)
            self._update_input_state()
            self._update_session_statuses()

    def _render_line(self, output: SelectableRichLog, line: OutputLine) -> None:
        """Render an OutputLine to the RichLog widget with distinct colors per type."""
        # Debug logging for errors
        if line.type == "error":
            logger.error(f"[{self.active_session}] Error line: {line.text[:200]}, raw={line.raw}")

        # Escape text so literal [ ] characters in LLM/tool output are not
        # misinterpreted as Rich markup tags — malformed tags corrupt width
        # calculations and break text wrapping.
        safe = rich_escape(line.text)

        if line.type == "assistant":
            output.write(f"[spring_green1]{safe}[/spring_green1]")
        elif line.type == "tool_use":
            output.write(f"[bright_yellow]⚡ {safe}[/bright_yellow]")
        elif line.type == "tool_result":
            output.write(f"[bright_blue]↳ {safe}[/bright_blue]")
        elif line.type == "result":
            output.write(f"[bold bright_green]✓ {safe}[/bold bright_green]")
        elif line.type == "system":
            output.write(f"[gold1]● {safe}[/gold1]")
        elif line.type == "error":
            output.write(f"[bright_red]✗ {safe}[/bright_red]")
        elif line.type == "user":
            output.write(f"[bold bright_cyan]> {safe}[/bold bright_cyan]")
        elif line.type == "agent_message":
            output.write(f"[bold bright_magenta]✉ {safe}[/bold bright_magenta]")
        elif line.type == "raw":
            output.write(f"[dim]{safe}[/dim]")
        else:
            logger.warning(f"[{self.active_session}] Unknown line type: {line.type}, text={line.text[:100]}")
            output.write(safe)

    def _update_input_state(self) -> None:
        """Enable/disable input based on session state.
        
        Input is always enabled for active sessions so users can queue messages
        while the agent is thinking. Messages are sent when the agent becomes idle.
        """
        msg_input = self.query_one("#message-input", SessionInput)
        label = self.query_one("#input-session-label", Static)

        if not self.active_session or not self.session_mgr:
            msg_input.disabled = True
            msg_input.placeholder = "No session selected"
            label.update("[dim]no session[/dim]")
            return

        sp = self.session_mgr.get(self.active_session)
        if sp and sp.is_alive:
            # Keep input enabled even when busy - messages queue automatically
            msg_input.disabled = False
            if sp.is_busy:
                agent_name = "Kimi" if sp.is_kimi else "Claude"
                pending = len(sp._pending_messages)
                if pending > 0:
                    msg_input.placeholder = f"{agent_name} thinking... ({pending} queued)"
                else:
                    msg_input.placeholder = f"{agent_name} is thinking... (type to queue)"
                label.update(f"[yellow]{self.active_session}[/yellow]")
            else:
                msg_input.placeholder = "Type a message... (Enter to send)"
                label.update(f"[green]{self.active_session}[/green]")
        else:
            msg_input.disabled = False
            msg_input.placeholder = "Type to start session..."
            label.update(f"[dim]{self.active_session}[/dim]")

    # ---- Input handling ----

    async def on_session_input_submitted(self, event: SessionInput.Submitted) -> None:
        """Handle Enter press in the message input."""
        text = event.value.strip()
        if not text:
            return

        if not self.active_session or not self.session_mgr:
            return

        if text == "/reload":
            output = self.query_one("#output-pane", SelectableRichLog)
            output.write("[dim yellow]Reloading TUI (session context will be preserved)...[/dim yellow]")
            await self._do_reload()
            return

        if text == "/restart":
            await self._do_restart_daemon()
            return

        if text.startswith("/telegram"):
            parts = text.split(None, 1)
            token = parts[1].strip() if len(parts) > 1 else ""
            await self._do_setup_telegram(token)
            return

        if text.startswith("/edit"):
            parts = text.split(None, 1)
            filename = parts[1].strip() if len(parts) > 1 else ""
            self._do_edit_file(filename)
            return

        if text.startswith("/model"):
            parts = text.split(None, 1)
            model_name = parts[1].strip() if len(parts) > 1 else ""
            self._do_set_model(model_name)
            return

        if text.startswith("/switch"):
            parts = text.split(None, 1)
            engine = parts[1].strip().lower() if len(parts) > 1 else ""
            await self._do_switch_engine(engine)
            return

        # @AgentName <message> — route to another agent's session
        if text.startswith("@"):
            parts = text[1:].split(None, 1)
            if len(parts) == 2:
                await self._do_send_agent_message(parts[0], parts[1])
                return

        # Expand [paste] token back to full content before sending
        try:
            msg_input = self.query_one("#message-input", SessionInput)
            text = msg_input.expand_paste(text)
            text = msg_input.expand_images(text)
        except Exception:
            pass

        # Inject compacted context from engine switch (one-time)
        context_prefix = self._switch_context.pop(self.active_session, "")
        if context_prefix:
            text = f"{context_prefix}\n\n{text}"

        sp = self.session_mgr.get(self.active_session)

        # If session not started, start it with this as the initial prompt
        if not sp or not sp.is_alive:
            output = self.query_one("#output-pane", SelectableRichLog)
            output.clear()
            output.write(f"[bold cyan]> {text[:200]}{'...' if len(text) > 200 else ''}[/bold cyan]")
            output.write(f"[dim yellow]Starting session {self.active_session}...[/dim yellow]")

            sp = await self.session_mgr.start_session(self.active_session, initial_prompt=text)
            sp.listeners.append(self._on_session_output)
            self._update_input_state()
            self._update_session_statuses()
            return

        # Send to running session
        await sp.send(text)
        self._update_input_state()

    # ---- File drag-and-drop ----

    async def on_event(self, event) -> None:
        """Intercept Paste events before Textual forwards them to the focused widget.

        Multi-line pastes are collapsed to [paste] in the input and the raw
        content is saved in SessionInput._paste_store so it can be expanded
        when the user submits. File-drop pastes (macOS drag-and-drop) are
        converted to paths inline instead.
        """
        from textual import events as _ev

        if isinstance(event, _ev.Paste):
            try:
                msg_input = self.query_one("#message-input", SessionInput)
            except Exception:
                msg_input = None
            if msg_input is not None and not msg_input.disabled:
                raw = event.text.replace("\x00", "")
                files = _parse_dropped_files(raw)
                if files:
                    images = [f for f in files if _is_image_file(f)]
                    other = [f for f in files if not _is_image_file(f)]
                    if images:
                        msg_input._pending_images.extend(images)
                        img_labels = " ".join(f"[image: {os.path.basename(f)}]" for f in images)
                        msg_input.insert_text_at_cursor(img_labels)
                    if other:
                        msg_input.insert_text_at_cursor(
                            " ".join(f'"{f}"' if " " in f else f for f in other)
                        )
                    msg_input.post_message(SessionInput.FileDropped(files))
                elif "\n" in raw:
                    msg_input._paste_store = raw
                    msg_input.insert_text_at_cursor("[paste]")
                else:
                    msg_input.insert_text_at_cursor(raw)
                return  # consume paste — we've handled it
        await super().on_event(event)

    def on_session_input_file_dropped(self, event: SessionInput.FileDropped) -> None:
        """Show a notification when files are drag-dropped onto the input."""
        try:
            msg_input = self.query_one("#message-input", SessionInput)
            output = self.query_one("#output-pane", SelectableRichLog)
        except Exception:
            return
        # Always allow file drops - input is never disabled for active sessions
        images = [f for f in event.files if _is_image_file(f)]
        other = [f for f in event.files if not _is_image_file(f)]
        if images:
            names = ", ".join(os.path.basename(f) for f in images)
            output.write(f"[dim cyan]image attached: {names} (type a message or press Enter)[/dim cyan]")
        if other:
            names = ", ".join(os.path.basename(f) for f in other)
            output.write(f"[dim cyan]dropped: {names}[/dim cyan]")

    # ---- Cron toggle ----

    async def _toggle_cron(self, item: CronListItem) -> None:
        """Toggle a cron job's enabled status."""
        if not self.db:
            return
        current = item.task_data.get("enabled", False)
        new_val = not current
        await self.db.set_scheduled_task_enabled(item.task_name, new_val)
        item.task_data["enabled"] = new_val
        item.update_task(item.task_data)

        # Request daemon to reload scheduler
        await self.db.request_scheduler_reload()

    # ---- Key bindings ----

    def action_toggle_sidebar(self) -> None:
        """Toggle sidebar visibility (ctrl+b)."""
        sidebar = self.query_one("#sidebar")
        quick_bar = self.query_one("#quick-session-bar")
        sidebar.toggle_class("collapsed")
        quick_bar.toggle_class("visible")
        logger.info(f"Toggled sidebar; sidebar={sidebar.classes} quick_bar={quick_bar.classes}")

    async def on_click(self, event: Click) -> None:
        """Handle clicks on the collapse button, expand button, and quick-bar sessions."""
        widget = event.widget
        if widget is None:
            return
        if widget.id in ("sidebar-collapse-btn", "quick-expand-btn"):
            self.action_toggle_sidebar()
            event.stop()
            return
        if isinstance(widget, QuickSessionButton):
            await self._switch_to_session(widget.session_name)
            event.stop()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_start_session(self) -> None:
        if not self.session_mgr:
            return
        names = self.session_mgr.all_names()

        def on_result(result: str | None) -> None:
            if result:
                self.run_worker(self._do_start_session(result))

        self.push_screen(StartSessionScreen(names), callback=on_result)

    async def _do_start_session(self, name: str) -> None:
        """Start a session and switch to it."""
        if not self.session_mgr:
            return
        await self._switch_to_session(name)

    def action_kill_session(self) -> None:
        if not self.active_session:
            self.notify("No active session to kill", severity="warning")
            return

        def on_result(confirmed: bool) -> None:
            if confirmed and self.active_session:
                self.run_worker(self._do_kill_session(self.active_session))

        self.push_screen(ConfirmKillScreen(self.active_session), callback=on_result)

    async def _do_kill_session(self, name: str) -> None:
        if not self.session_mgr:
            return
        await self.session_mgr.kill_session(name)

        output = self.query_one("#output-pane", SelectableRichLog)
        output.write(f"[red][b]Session {name} killed.[/b][/red]")

        self._update_input_state()
        self._update_session_statuses()

    def action_copy_output(self) -> None:
        """Copy the stream pane selection (or whole buffer if nothing selected)."""
        text = ""
        try:
            output = self.query_one("#output-pane", SelectableRichLog)
            text = output.selected_text
        except Exception:
            pass
        if not text and self.active_session and self.session_mgr:
            sp = self.session_mgr.get(self.active_session)
            if sp:
                text = "\n".join(line.text for line in sp.output_buffer)
        if not text:
            return
        # Try native clipboard tools first (OSC 52 doesn't work on macOS Terminal)
        copied = False
        import sys, subprocess
        if sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
                copied = True
            except Exception:
                pass
        elif sys.platform == "win32":
            try:
                subprocess.run(["clip.exe"], input=text, text=True, check=True, timeout=5)
                copied = True
            except Exception:
                pass
        else:
            for tool in ["wl-copy", "xclip", "xsel"]:
                try:
                    if tool == "wl-copy":
                        subprocess.run([tool], input=text, text=True, check=True, timeout=5)
                    elif tool == "xclip":
                        subprocess.run([tool, "-selection", "clipboard"], input=text, text=True, check=True, timeout=5)
                    elif tool == "xsel":
                        subprocess.run([tool, "-b"], input=text, text=True, check=True, timeout=5)
                    copied = True
                    break
                except Exception:
                    pass
        if not copied:
            # Fallback to Textual's internal clipboard (OSC 52)
            self.app.copy_to_clipboard(text)

    def action_interrupt(self) -> None:
        """Escape key — interrupt the current turn if a session is busy."""
        # If a modal/screen is open, let its own escape binding handle dismissal
        if len(self._screen_stack) > 1:
            return

        if not self.active_session or not self.session_mgr:
            return

        sp = self.session_mgr.get(self.active_session)
        if sp and sp.is_alive and sp.is_busy:
            self.run_worker(self._do_interrupt_session(self.active_session))

    async def _do_interrupt_session(self, name: str) -> None:
        if not self.session_mgr:
            return
        sp = self.session_mgr.get(name)
        if sp:
            await sp.interrupt()
        self._update_input_state()
        self._update_session_statuses()

    def action_new_task(self) -> None:
        def on_result(result: dict | None) -> None:
            if result:
                self.run_worker(self._do_create_task(result))

        # Get available sessions from config
        session_names = list(self.config.agents.keys()) if self.config else []
        self.push_screen(NewTaskScreen(session_names), callback=on_result)

    async def _do_create_task(self, task_data: dict) -> None:
        if not self.db:
            return

        # Build payload with session if specified
        payload = {"notify": True}
        if task_data.get("description"):
            payload["prompt"] = task_data["description"]
            payload["description"] = task_data["description"]
        if task_data.get("session"):
            payload["session"] = task_data["session"]

        # Save to database
        await self.db.upsert_scheduled_task(
            name=task_data["name"],
            task_type=task_data["task_type"],
            cron_expression=task_data["cron"],
            payload=payload,
            enabled=True,
        )

        # Save to YAML for persistence across restarts
        await self._save_task_to_yaml(
            name=task_data["name"],
            task_type=task_data["task_type"],
            cron=task_data["cron"],
            payload=payload,
        )

        # Request daemon to reload scheduler
        await self.db.request_scheduler_reload()

        await self._populate_cron()
        self.notify(f"Task '{task_data['name']}' created and saved", severity="information")

    async def _save_task_to_yaml(self, name: str, task_type: str, cron: str, payload: dict) -> None:
        """Save task to tasks.yaml for persistence across daemon restarts."""
        try:
            import yaml
            tasks_path = Path(__file__).parent.parent / "config" / "tasks.yaml"

            # Build task dict matching tasks.yaml format
            task_dict = {"type": task_type, "cron": cron}
            if payload.get("session"):
                task_dict["session"] = payload["session"]
            if payload.get("description"):
                task_dict["description"] = payload["description"]
            if payload.get("prompt"):
                task_dict["prompt"] = payload["prompt"]
            if payload.get("notify"):
                task_dict["notify"] = payload["notify"]

            if tasks_path.exists():
                content = tasks_path.read_text()
                data = yaml.safe_load(content) or {}
            else:
                data = {"tasks": {}}

            tasks = data.get("tasks", {})
            tasks[name] = task_dict
            data["tasks"] = tasks

            tasks_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        except Exception as e:
            logger.error(f"Failed to save task to YAML: {e}")

    def action_edit_session_file(self) -> None:
        """Open a file picker for the highlighted or active session."""
        # Prefer the highlighted item in the session list
        session_name = None
        try:
            session_list = self.query_one("#session-list", ListView)
            highlighted = session_list.highlighted_child
            if isinstance(highlighted, SessionListItem):
                session_name = highlighted.session_name
        except Exception:
            pass
        if not session_name:
            session_name = self.active_session
        if not session_name or not self.session_mgr:
            self.notify("Select a session first", severity="warning")
            return

        project_dir = Path(
            self.session_mgr.project_map.get(session_name)
            or f"~/Projects/halos/sessions/{session_name}"
        ).expanduser()

        def on_file_chosen(filename: Optional[str]) -> None:
            if not filename:
                return
            file_path = project_dir / filename
            if not file_path.exists() and filename in self._SESSION_FILES:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(self._SESSION_FILES[filename])

            def on_close(_) -> None:
                output = self.query_one("#output-pane", SelectableRichLog)
                output.write(f"[dim cyan]saved: {file_path.name}[/dim cyan]")

            self.push_screen(FileEditorScreen(file_path), callback=on_close)

        self.push_screen(
            SessionFilePickerScreen(session_name, project_dir, self._SESSION_FILES),
            callback=on_file_chosen,
        )

    def action_edit_selected(self) -> None:
        """Edit the currently highlighted item (task or session)."""
        # First check: is a task highlighted in the cron list?
        # Try highlighted_child first (works when clicked)
        try:
            cron_list = self.query_one("#cron-list", ListView)
            highlighted = cron_list.highlighted_child
            if isinstance(highlighted, CronListItem):
                self._edit_task(highlighted.task_data)
                return
        except Exception:
            pass

        # Try by index (works with keyboard navigation)
        try:
            cron_list = self.query_one("#cron-list", ListView)
            if cron_list.children:
                index = cron_list.index
                if index is not None and 0 <= index < len(cron_list.children):
                    item = cron_list.children[index]
                    if isinstance(item, CronListItem):
                        self._edit_task(item.task_data)
                        return
        except Exception:
            pass

        # Second check: is a session highlighted?
        try:
            session_list = self.query_one("#session-list", ListView)
            highlighted = session_list.highlighted_child
            if isinstance(highlighted, SessionListItem):
                self.action_edit_session_file()
                return
        except Exception:
            pass

        # Fallback: use active session
        if self.active_session:
            self.action_edit_session_file()
        else:
            self.notify("Select a task or session first", severity="warning")

    def _edit_task(self, task_data: dict) -> None:
        """Open the edit dialog for a task."""
        def on_result(result: dict | None) -> None:
            if result:
                self.run_worker(self._do_update_task(result))

        session_names = list(self.config.agents.keys()) if self.config else []
        self.push_screen(EditTaskScreen(task_data, session_names), callback=on_result)

    async def _do_update_task(self, task_data: dict) -> None:
        """Update an existing task in DB and YAML."""
        if not self.db:
            return

        name = task_data["name"]

        # Build payload
        payload = {"notify": True}
        if task_data.get("description"):
            payload["prompt"] = task_data["description"]
            payload["description"] = task_data["description"]
        if task_data.get("session"):
            payload["session"] = task_data["session"]

        # Update in database
        await self.db.upsert_scheduled_task(
            name=name,
            task_type=task_data["task_type"],
            cron_expression=task_data["cron"],
            payload=payload,
            enabled=task_data.get("enabled", True),
        )

        # Update in YAML
        await self._save_task_to_yaml(
            name=name,
            task_type=task_data["task_type"],
            cron=task_data["cron"],
            payload=payload,
        )

        await self._populate_cron()
        self.notify(f"Task '{name}' updated", severity="information")

    def action_delete_selected(self) -> None:
        """Delete the currently highlighted task."""
        # Check if cron list has a highlighted item (try highlighted_child first)
        try:
            cron_list = self.query_one("#cron-list", ListView)
            highlighted = cron_list.highlighted_child
            if isinstance(highlighted, CronListItem):
                self._confirm_delete_task(highlighted.task_data)
                return
        except Exception:
            pass

        # Try by index (works with keyboard navigation)
        try:
            cron_list = self.query_one("#cron-list", ListView)
            if cron_list.children:
                index = cron_list.index
                if index is not None and 0 <= index < len(cron_list.children):
                    item = cron_list.children[index]
                    if isinstance(item, CronListItem):
                        self._confirm_delete_task(item.task_data)
                        return
        except Exception:
            pass

        self.notify("Select a task to delete first", severity="warning")

    def _confirm_delete_task(self, task_data: dict) -> None:
        """Show delete confirmation dialog."""
        def on_confirmed(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(self._do_delete_task(task_data["name"]))

        self.push_screen(DeleteConfirmScreen(task_data["name"]), callback=on_confirmed)

    async def _do_delete_task(self, name: str) -> None:
        """Delete a task from DB and YAML."""
        if not self.db:
            return

        # Delete from database
        await self.db.delete_scheduled_task(name)

        # Delete from YAML
        await self._delete_task_from_yaml(name)

        # Request daemon to reload scheduler
        await self.db.request_scheduler_reload()

        await self._populate_cron()
        self.notify(f"Task '{name}' deleted", severity="information")

    async def _delete_task_from_yaml(self, name: str, tasks_path: str = "config/tasks.yaml") -> None:
        """Remove a task from tasks.yaml."""
        try:
            import yaml
            path = Path(__file__).parent.parent / "config" / "tasks.yaml"

            if not path.exists():
                return

            content = path.read_text()
            data = yaml.safe_load(content) or {}
            tasks = data.get("tasks", {})

            if name in tasks:
                del tasks[name]
                data["tasks"] = tasks
                path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
                logger.info(f"Deleted task '{name}' from {path}")
        except Exception as e:
            logger.error(f"Failed to delete task from YAML: {e}")

    # Known session files and their starter templates
    _SESSION_FILES: dict[str, str] = {
        "claude.md": "# Claude instructions\n\nSystem prompt / instructions for this session.\n",
        "soul.md": "# Soul\n\nCore values, beliefs, and essence of this agent.\n",
    }

    def _do_set_model(self, model_name: str) -> None:
        """Switch the active session to a different model, preserving conversation history."""
        output = self.query_one("#output-pane", SelectableRichLog)
        valid = {"haiku", "sonnet", "opus",
                 "claude-haiku-4-5", "claude-sonnet-4-6",
                 "claude-opus-4-6", "claude-opus-4-7",
                 "claude-haiku-4-5-20251001"}
        if not model_name:
            sp = self.session_mgr.get(self.active_session) if self.session_mgr else None
            current = (sp.model if sp and sp.model else
                       (self.config.claude_code.default_model if self.config else "haiku"))
            output.write(f"[dim]Current model: [b]{current}[/b]  —  usage: /model haiku|sonnet|opus[/dim]")
            return
        if not self.active_session or not self.session_mgr:
            output.write("[red]No active session.[/red]")
            return
        sp = self.session_mgr.get(self.active_session)
        if not sp:
            output.write("[red]Session not started yet — start it first, then switch model.[/red]")
            return
        sp.model = model_name
        output.write(f"[dim cyan]model → [b]{model_name}[/b]  (takes effect on next message; context preserved via --resume)[/dim cyan]")

    async def _do_switch_engine(self, engine: str) -> None:
        """Switch the active session between Claude and Kimi, transferring compacted context."""
        output = self.query_one("#output-pane", SelectableRichLog)
        if engine not in ("kimi", "claude"):
            output.write("[red]Usage: /switch kimi | /switch claude[/red]")
            return
        if not self.active_session or not self.session_mgr:
            output.write("[red]No active session.[/red]")
            return

        current = self.session_mgr.engine_override.get(self.active_session, "claude")
        if current == engine:
            output.write(f"[dim]Already using {engine}.[/dim]")
            return

        sp = self.session_mgr.get(self.active_session)

        # Compact recent context
        summary = ""
        if sp:
            lines = []
            for line in sp.output_buffer:
                if line.type == "user":
                    lines.append(f"USER: {line.text[:400]}")
                elif line.type == "assistant":
                    lines.append(f"ASSISTANT: {line.text[:400]}")
            if len(lines) >= 2:
                conversation = "\n".join(lines[-20:])
                prompt = (
                    "Summarize the following conversation into a tight paragraph (max 300 words). "
                    "Preserve key facts, decisions, and any open tasks or questions:\n\n"
                    f"{conversation}"
                )
                try:
                    if current == "kimi":
                        binary = shutil.which("kimi") or str(Path.home() / ".local" / "bin" / "kimi")
                        cmd = [binary, "--print", "--yolo", "-p", prompt, "--output-format", "stream-json"]
                    else:
                        binary = self.config.claude_code.binary_path
                        cmd = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]
                        if self.config.claude_code.skip_permissions:
                            cmd.append("--dangerously-skip-permissions")

                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
                    for raw in out.decode("utf-8", errors="replace").strip().splitlines():
                        try:
                            data = json.loads(raw)
                            if data.get("type") == "assistant" or data.get("role") == "assistant":
                                msg = data.get("message", {})
                                content_blocks = msg.get("content", []) if msg else data.get("content", [])
                                texts = []
                                for block in content_blocks:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        texts.append(block.get("text", ""))
                                if texts:
                                    summary = "".join(texts).strip()
                                    break
                        except json.JSONDecodeError:
                            if raw.strip():
                                summary = raw.strip()
                                break
                except Exception as e:
                    logger.warning(f"Context compaction failed: {e}")

        # Kill current session and remove it so the new engine spawns fresh
        if sp:
            await sp.kill()
        self.session_mgr._sessions.pop(self.active_session, None)

        # Set override for next creation and persist to DB
        self.session_mgr.engine_override[self.active_session] = engine
        if self.db:
            db_session_name = f"telegram:claude:{self.active_session.lower()}" if engine == "claude" else self.active_session
            await self.db.upsert_session(db_session_name, f"{engine}-{uuid.uuid5(uuid.NAMESPACE_DNS, f'halos.{self.active_session}')}", sp.project_dir if sp else "", engine=engine)
        output.write(f"[bold cyan]Switched {self.active_session} to {engine}[/bold cyan]")
        if summary:
            self._switch_context[self.active_session] = f"[Context from previous {current} conversation: {summary}]"
            output.write("[dim]Compacted context will be injected into your next message.[/dim]")
        else:
            output.write("[dim]No prior context to transfer.[/dim]")

        self._update_input_state()
        self._update_session_statuses()

    async def _do_send_agent_message(self, recipient: str, content: str) -> None:
        """Route @AgentName message to another agent's session."""
        output = self.query_one("#output-pane", SelectableRichLog)
        if not self.session_mgr or not self.db:
            output.write("[red]Session manager not ready.[/red]")
            return
        sender = self.active_session or "TUI"
        target_sp = await self.session_mgr.get_or_create(recipient)
        if not target_sp.is_alive:
            await target_sp.start()
        await self.db.enqueue_agent_message(sender, recipient, content)
        await target_sp.inject_agent_message(sender, content)
        output.write(f"[dim magenta]→ {recipient}: {content}[/dim magenta]")

    def _do_edit_file(self, filename: str) -> None:
        """Open the file editor for a session file."""
        output = self.query_one("#output-pane", SelectableRichLog)

        if not self.active_session:
            output.write("[red]No active session selected.[/red]")
            return

        # Resolve filename — accept bare name (claude) or with extension
        name = filename.lower().strip()
        if name and "." not in name:
            name = name + ".md"
        if not name:
            files = ", ".join(self._SESSION_FILES.keys())
            output.write(f"[dim]Usage: /edit <file>  —  known files: {files}[/dim]")
            return

        project_dir = Path(
            (self.session_mgr.project_map.get(self.active_session) if self.session_mgr else None)
            or f"~/Projects/halos/sessions/{self.active_session}"
        ).expanduser()

        file_path = project_dir / name

        # Seed with template if new
        if not file_path.exists() and name in self._SESSION_FILES:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(self._SESSION_FILES[name])

        def on_close(_) -> None:
            if file_path.exists():
                output.write(f"[dim cyan]saved: {file_path.name}[/dim cyan]")

        self.push_screen(FileEditorScreen(file_path), callback=on_close)

    async def _do_setup_telegram(self, token: str) -> None:
        """Configure a Telegram bot for the active session via /telegram <TOKEN>."""
        import yaml as _yaml
        output = self.query_one("#output-pane", SelectableRichLog)

        if not self.active_session:
            output.write("[red]No active session selected.[/red]")
            return
        if not token:
            output.write("[red]Usage: /telegram <BOT_TOKEN>[/red]")
            return

        session = self.active_session
        project_dir = Path(
            (self.session_mgr.project_map.get(session) if self.session_mgr else None)
            or f"~/Projects/halos/sessions/{session}"
        ).expanduser()

        # Create session directory
        project_dir.mkdir(parents=True, exist_ok=True)

        # Create soul.md template if missing
        soul_path = project_dir / "soul.md"
        if not soul_path.exists():
            soul_path.write_text(
                f"# {session.capitalize()} — soul\n\n"
                f"You are {session.capitalize()}, a HalOS agent.\n\n"
                f"## Role\n\nDescribe your role here.\n\n"
                f"## Personality\n\nDescribe your personality here.\n"
            )
            output.write(f"[dim]created soul template: {soul_path}[/dim]")

        # Update config.yaml
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        with open(config_path) as f:
            raw = _yaml.safe_load(f) or {}

        if "agents" not in raw or not isinstance(raw["agents"], dict):
            raw["agents"] = {}

        raw["agents"][session] = {
            "bot_token": token,
            "project_dir": str(project_dir),
        }

        with open(config_path, "w") as f:
            _yaml.dump(raw, f, default_flow_style=False, allow_unicode=True)

        output.write(f"[green]Telegram bot configured for [b]{session}[/b][/green]")
        output.write(f"[dim]soul: {soul_path}[/dim]")
        output.write("[dim]Run /restart to activate the bot.[/dim]")

    async def _do_restart_daemon(self) -> None:
        """Kill ALL running daemon instances and relaunch a fresh one."""
        import signal as _signal
        import subprocess
        output = self.query_one("#output-pane", SelectableRichLog)
        pid_path = Path.home() / ".halos" / "daemon.pid"
        project_dir = Path(__file__).parent.parent
        my_pid = os.getpid()

        # Collect all daemon PIDs: from PID file + process scan
        pids_to_kill: set[int] = set()

        if pid_path.exists():
            try:
                pids_to_kill.add(int(pid_path.read_text().strip()))
            except ValueError:
                pass

        # Scan for any other python -m halos processes (but not TUI = "halos tui")
        try:
            result = subprocess.run(
                ["pgrep", "-f", "python.*-m halos"],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                try:
                    pid = int(line.strip())
                    if pid != my_pid:
                        pids_to_kill.add(pid)
                except ValueError:
                    pass
        except Exception:
            pass

        # Filter out TUI process (has "tui" in its cmdline)
        verified_kill: set[int] = set()
        for pid in pids_to_kill:
            try:
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    cmd = cmdline_path.read_bytes().replace(b"\x00", b" ").decode()
                else:
                    # macOS: use ps
                    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                       capture_output=True, text=True)
                    cmd = r.stdout
                if "tui" not in cmd.lower():
                    verified_kill.add(pid)
            except Exception:
                verified_kill.add(pid)  # kill it anyway if we can't check

        if verified_kill:
            output.write(f"[dim yellow]daemon: killing PIDs {sorted(verified_kill)}...[/dim yellow]")
            for pid in verified_kill:
                try:
                    os.kill(pid, _signal.SIGTERM)
                except ProcessLookupError:
                    pass

            # Wait up to 5s for all to exit
            for _ in range(10):
                await asyncio.sleep(0.5)
                still_alive = set()
                for pid in verified_kill:
                    try:
                        os.kill(pid, 0)  # signal 0 = check existence
                        still_alive.add(pid)
                    except ProcessLookupError:
                        pass
                if not still_alive:
                    break
                verified_kill = still_alive

            # Force kill anything that didn't respond to SIGTERM
            for pid in verified_kill:
                try:
                    os.kill(pid, _signal.SIGKILL)
                    output.write(f"[dim red]daemon: force-killed PID {pid}[/dim red]")
                except ProcessLookupError:
                    pass

            # Verify all targeted PIDs are actually dead before proceeding
            await asyncio.sleep(0.5)
            for pid in pids_to_kill:
                try:
                    os.kill(pid, 0)
                    # Still alive after SIGKILL — wait a bit more
                    await asyncio.sleep(2)
                    try:
                        os.kill(pid, 0)
                        output.write(f"[bold red]daemon: PID {pid} still alive after SIGKILL![/bold red]")
                    except ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass
        else:
            output.write("[dim]daemon: no running daemon found[/dim]")

        # Clear stale Telegram webhooks so the new daemon doesn't hit 409 conflicts
        if hasattr(self, "config") and self.config and hasattr(self.config, "agents"):
            tokens: set[str] = set()
            for agent_cfg in self.config.agents.values():
                if hasattr(agent_cfg, "bot_token") and agent_cfg.bot_token:
                    tokens.add(agent_cfg.bot_token)
            if hasattr(self.config, "telegram") and self.config.telegram.bot_token:
                tokens.add(self.config.telegram.bot_token)
            for token in tokens:
                try:
                    subprocess.run(
                        ["curl", "-sf", f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=false"],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass
            if tokens:
                output.write(f"[dim]daemon: cleared Telegram webhooks for {len(tokens)} bot(s)[/dim]")

        # Relaunch
        log_path = Path.home() / ".halos" / "daemon.log"
        # Preserve prior log for post-mortem — previous behavior truncated on every
        # restart, which wiped crash evidence.
        if log_path.exists() and log_path.stat().st_size > 0:
            archive_path = log_path.with_name(f"daemon.log.{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                log_path.rename(archive_path)
            except OSError:
                pass
        proc = subprocess.Popen(
            [sys.executable, "-m", "halos"],
            cwd=str(project_dir),
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        # Verify the new daemon actually started
        await asyncio.sleep(2)
        new_pid = None
        if pid_path.exists():
            try:
                new_pid = int(pid_path.read_text().strip())
                os.kill(new_pid, 0)  # confirm it's alive
                output.write(f"[green]daemon: started and verified (PID {new_pid})[/green]")
            except (ProcessLookupError, ValueError):
                output.write(f"[bold red]daemon: launched but PID {new_pid or '?'} is not alive — check daemon.log[/bold red]")
        else:
            output.write(f"[yellow]daemon: launched (PID {proc.pid}) but no PID file yet — may still be starting[/yellow]")

    async def _do_reload(self) -> None:
        """Restart the TUI in-process, preserving session context in database."""
        # Ensure all session data is persisted before reloading
        if self.session_mgr and self.db:
            for name in self.session_mgr.all_names():
                sp = self.session_mgr.get(name)
                if sp and sp._confirmed_session_id:
                    # Only persist session ids whose conversation file still exists.
                    # Writing a stale id back to the DB caused a reload loop where every
                    # subsequent --resume failed with "No conversation found".
                    if not sp.is_kimi and sp.project_dir:
                        slug = str(Path(sp.project_dir).expanduser()).replace("/", "-")
                        jsonl = Path.home() / ".claude" / "projects" / slug / f"{sp._confirmed_session_id}.jsonl"
                        if not jsonl.exists():
                            logger.warning(
                                f"[reload] dropping stale session_id for {name}: {jsonl} missing"
                            )
                            continue
                    engine = "kimi" if sp.is_kimi else "claude"
                    db_session_name = f"telegram:claude:{name.lower()}" if engine == "claude" else name
                    await self.db.upsert_session(db_session_name, sp._confirmed_session_id, sp.project_dir, engine=engine)

            # Ensure all writes are committed to disk
            if self.db.db:  # aiosqlite connection
                await self.db.db.commit()

        if self.db:
            await self.db.close()
            self.db = None

        # Brief pause so aiosqlite's worker thread finishes and releases the
        # SQLite write lock before the new instance's on_mount calls connect().
        await asyncio.sleep(0.2)
        # Set flag so main() restarts the app after run() returns cleanly.
        # Using self.exit() lets Textual restore the terminal before we relaunch —
        # os.execv / os._exit bypass that and leave the terminal in raw mode.
        self._reload_requested = True
        self.exit()

    async def action_refresh(self) -> None:
        # Reload config
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        self.config = load_config(str(config_path))
        if self.session_mgr:
            self.session_mgr.config = self.config
            if self.config.claude_code.projects:
                self.session_mgr.project_map = {
                    name: str(Path(path).expanduser())
                    for name, path in self.config.claude_code.projects.items()
                }
        await self._populate_sessions()
        await self._populate_cron()
        self._update_status_bar()
        self.notify("Refreshed", severity="information")

    def action_next_session(self) -> None:
        if not self._session_names:
            return
        if self.active_session is None:
            idx = 0
        else:
            try:
                idx = (self._session_names.index(self.active_session) + 1) % len(self._session_names)
            except ValueError:
                idx = 0
        self.run_worker(self._switch_to_session(self._session_names[idx]))

    def action_prev_session(self) -> None:
        if not self._session_names:
            return
        if self.active_session is None:
            idx = len(self._session_names) - 1
        else:
            try:
                idx = (self._session_names.index(self.active_session) - 1) % len(self._session_names)
            except ValueError:
                idx = 0
        self.run_worker(self._switch_to_session(self._session_names[idx]))

    # ---- Shutdown ----

    async def action_quit(self) -> None:
        """Clean shutdown. Preserves DB session rows so conversations resume next launch."""
        if self.session_mgr:
            await self.session_mgr.shutdown()
        if self.db:
            await self.db.close()
        self.exit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Launch the HalOS TUI."""
    # Set up logging to file (Textual owns the terminal)
    log_dir = Path.home() / ".halos"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Honor log_level from config.yaml (was hardcoded DEBUG, which generated 4 GB
    # logs in ~16 h from aiosqlite query tracing). Rotate to cap disk usage.
    try:
        cfg_level = getattr(logging, load_config().log_level, logging.INFO)
    except Exception:
        cfg_level = logging.INFO
    handler = RotatingFileHandler(
        filename=str(log_dir / "tui.log"),
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logging.basicConfig(level=cfg_level, handlers=[handler])
    # aiosqlite DEBUG logs every execute+fetch — pure noise even when debugging.
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    app = HalOSTUI()
    app.run()
    if app._reload_requested:
        # /reload was called — Textual has restored the terminal.
        # Re-exec Python so code changes on disk are picked up (the in-process
        # module cache would otherwise hold the stale classes).
        os.execv(sys.executable, [sys.executable, "-m", "halos", "tui"])


if __name__ == "__main__":
    main()
