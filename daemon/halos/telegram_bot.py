"""Telegram bot interface for HalOS."""

import asyncio
import logging
import time
from datetime import datetime
from telegram import Update
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TimedOut,
    Forbidden,
    InvalidToken,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from .config import Config
from .router import Router, RouteDecision
from .claude_code import ClaudeCodeEngine, StreamEvent, TOOL_LABELS
from .claude_api import ClaudeAPI
from .memory import MemoryManager
from .scheduler import TaskScheduler
from .notifier import Notifier
from .db import Database

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, config: Config, router: Router, engine: ClaudeCodeEngine,
                 claude_api: ClaudeAPI, memory: MemoryManager, scheduler: TaskScheduler,
                 notifier: Notifier, db: Database, kimi_api=None, kimi_engine=None):
        self.config = config
        self.router = router
        self.engine = engine
        self.claude_api = claude_api
        self.kimi_api = kimi_api
        self.kimi_engine = kimi_engine
        self.memory = memory
        self.scheduler = scheduler
        self.notifier = notifier
        self.db = db
        self.app = None
        self._start_time = datetime.now()
        self._active_code_session: dict[int, dict] = {}  # user_id -> {name, project_dir}
        self._preferred_engine: dict[int, str] = {}      # user_id -> "claude" | "kimi"
        self._switch_context: dict[int, str] = {}        # compacted context after /switch

    def _is_authorized(self, user_id: int) -> bool:
        if not self.config.telegram.allowed_user_ids:
            return False
        return user_id in self.config.telegram.allowed_user_ids

    # --- Basic commands ---

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return
        await update.message.reply_text(
            f"Hal is online. Your user ID: {update.effective_user.id}"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        uptime = datetime.now() - self._start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)

        msg_count = await self.db.get_message_count()
        tasks = await self.scheduler.list_tasks()
        cost = await self.db.get_cost_summary("today")
        sessions = await self.db.get_active_sessions()
        running = self.engine.get_running()

        text = (
            f"Uptime: {hours}h {minutes}m\n"
            f"Messages: {msg_count}\n"
            f"API calls today: {cost['calls']}\n"
            f"Tokens today: {cost['tokens']:,}\n"
            f"Active sessions: {len(sessions)}\n"
            f"Running: {', '.join(running) if running else 'none'}\n\n"
            f"Scheduled tasks:\n{tasks}"
        )
        await update.message.reply_text(text)

    # --- Memory commands ---

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        query = " ".join(context.args) if context.args else None
        result = await self.memory.list_memory(query)
        await self._send_chunked(update.message, result)

    async def _cmd_remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await update.message.reply_text("Usage: /remember key: value\nOr: /remember category key: value")
            return

        if ":" in text:
            before_colon, value = text.split(":", 1)
            parts = before_colon.strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() in ("project", "preference", "fact", "context"):
                category, key = parts[0].lower(), parts[1].strip()
            else:
                category, key = "fact", before_colon.strip()
            value = value.strip()
        else:
            category, key, value = "fact", text[:30], text

        result = await self.memory.remember(key, value, category)
        await update.message.reply_text(result)

    async def _cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        key = " ".join(context.args) if context.args else ""
        if not key:
            await update.message.reply_text("Usage: /forget <key>")
            return
        result = await self.memory.forget(key)
        await update.message.reply_text(result)

    # --- Task commands ---

    async def _cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        result = await self.scheduler.list_tasks()
        await update.message.reply_text(result)

    async def _cmd_add_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        args = context.args or []
        if len(args) < 3:
            await update.message.reply_text(
                'Usage: /add_task <name> "<cron>" <description>\n'
                'Example: /add_task morning_check "0 8 * * *" Check project status'
            )
            return

        name = args[0]
        remaining = " ".join(args[1:])
        if remaining.startswith('"'):
            try:
                end_quote = remaining.index('"', 1)
                cron = remaining[1:end_quote]
                description = remaining[end_quote + 1:].strip()
            except ValueError:
                await update.message.reply_text("Missing closing quote on cron expression.")
                return
        else:
            cron_parts = args[1:6]
            cron = " ".join(cron_parts)
            description = " ".join(args[6:])

        result = await self.scheduler.add_task(name, cron, description)
        await update.message.reply_text(result)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        name = " ".join(context.args) if context.args else ""
        if not name:
            await update.message.reply_text("Usage: /pause <task_name>")
            return
        result = await self.scheduler.pause_task(name)
        await update.message.reply_text(result)

    async def _cmd_resume_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        name = " ".join(context.args) if context.args else ""
        if not name:
            await update.message.reply_text("Usage: /resume <task_name>")
            return
        result = await self.scheduler.resume_task(name)
        await update.message.reply_text(result)

    # --- Claude Code / Session commands ---

    async def _cmd_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/code <project> <instruction>\n"
                "/code <instruction> (uses default session)\n"
                "/code new <project> — fresh session\n"
                "/code kill <project> — terminate session\n"
                "/sessions — list sessions\n"
                "/switch <project> — change default\n"
                "/end — exit code session"
            )
            return

        first = args[0].lower()

        if first == "new" and len(args) >= 2:
            project_name, project_dir = self.engine.resolve_project(args[1])
            if not project_name:
                await update.message.reply_text(f"Unknown project: {args[1]}")
                return
            instruction = " ".join(args[2:]) if len(args) > 2 else "Review the codebase and summarize current state."
            self._active_code_session[update.effective_user.id] = {"name": project_name, "project_dir": project_dir}
            await self._run_code(update, project_name, project_dir, instruction)
            return

        if first == "kill" and len(args) >= 2:
            project_name, _ = self.engine.resolve_project(args[1])
            if project_name:
                active = self._active_code_session.get(update.effective_user.id)
                if active and active["name"] == project_name:
                    del self._active_code_session[update.effective_user.id]
                result = await self.engine.kill_session(project_name)
                await update.message.reply_text(result)
            else:
                await update.message.reply_text(f"Unknown project: {args[1]}")
            return

        project_name, project_dir = self.engine.resolve_project(first)

        if project_name and len(args) > 1:
            instruction = " ".join(args[1:])
            self._active_code_session[update.effective_user.id] = {"name": project_name, "project_dir": project_dir}
            await self._run_code(update, project_name, project_dir, instruction)
        elif project_name and len(args) == 1:
            self._active_code_session[update.effective_user.id] = {"name": project_name, "project_dir": project_dir}
            await update.message.reply_text(f"Entered {project_name} session. Messages go to Claude Code until /end")
        else:
            default = await self.db.get_default_session()
            if not default:
                await update.message.reply_text(
                    "No default session set. Use /switch <project> or /code <project> <instruction>"
                )
                return
            instruction = " ".join(args)
            self._active_code_session[update.effective_user.id] = {"name": default["name"], "project_dir": default["project_dir"]}
            await self._run_code(update, default["name"], default["project_dir"], instruction)

    async def _run_code(self, update: Update, project_name: str, project_dir: str, instruction: str):
        """Execute Claude Code with streaming progress via message editing."""
        lock = self.engine._get_lock(project_name)
        if lock.locked():
            status_msg = await update.message.reply_text(f"[{project_name}] Queued — waiting for current task...")
        else:
            status_msg = await update.message.reply_text(f"[{project_name}] Working...")

        debounce = self.config.claude_code.progress_debounce_secs
        last_progress_time = [0.0]  # list so inner function can mutate

        async def on_progress(event: StreamEvent):
            now = time.time()
            if now - last_progress_time[0] < debounce:
                return
            last_progress_time[0] = now
            label = TOOL_LABELS.get(event.tool_name or "", f"Using {event.tool_name}...")
            try:
                await status_msg.edit_text(f"[{project_name}] {label}")
            except Exception:
                pass

        try:
            model = self.config.claude_code.code_model
            result = await self.engine.invoke_streaming(
                instruction=instruction,
                project_name=project_name,
                project_dir=project_dir,
                model=model,
                on_progress=on_progress,
            )

            # Delete the status message and send final result as new message (triggers notification)
            try:
                await status_msg.delete()
            except Exception:
                pass

            await self._send_chunked(update.message, f"[{project_name}]\n\n{result.text}")
        except Exception as e:
            logger.exception(f"TelegramBot _run_code failed for {project_name}: {e}")
            try:
                await status_msg.edit_text(f"[{project_name}] Error — see reply")
            except Exception:
                pass
            await self._send_chunked(
                update.message,
                f"Sorry, something went wrong in {project_name}: {type(e).__name__}: {e}"
            )

    async def _cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        sessions = await self.db.get_active_sessions()
        if not sessions:
            await update.message.reply_text("No active sessions.\nStart one with /code <project> <instruction>")
            return

        running = self.engine.get_running()
        lines = ["Active Sessions:\n"]
        for s in sessions:
            default_tag = " (default)" if s.get("is_default") else ""
            running_tag = " [running]" if s["name"] in running else ""
            last = s.get("last_message_at", "never") or "never"
            lines.append(
                f"  {'>' if s.get('is_default') else ' '} {s['name']}{default_tag}{running_tag}\n"
                f"    {s['project_dir']}\n"
                f"    {s.get('message_count', 0)} messages | last: {last}"
            )

        lines.append("\nUse /switch <name> to change default.")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/switch <project> — change default code session\n"
                "/switch kimi — use Kimi for general chat\n"
                "/switch claude — use Claude for general chat"
            )
            return

        choice = args[0].lower()
        user_id = update.effective_user.id
        if choice in ("kimi", "claude"):
            current = self._preferred_engine.get(user_id, "claude")
            if current != choice:
                summary = await self._compact_context("telegram", current)
                if summary:
                    self._switch_context[user_id] = f"[Context from previous {current} conversation: {summary}]"
            self._preferred_engine[user_id] = choice
            await update.message.reply_text(f"Switched general chat engine to {choice}.")
            return

        project_name, project_dir = self.engine.resolve_project(args[0])
        if not project_name:
            await update.message.reply_text(f"Unknown project: {args[0]}")
            return

        session_id = self.engine.session_id_for(project_name)
        await self.db.upsert_session(project_name, session_id, project_dir)
        await self.db.set_default_session(project_name)
        await update.message.reply_text(f"Switched default session to {project_name}.")

    async def _compact_context(self, source: str, current_engine_name: str) -> str:
        recent = await self.db.get_recent_messages(source=source, limit=20)
        if len(recent) < 2:
            return ""
        lines = []
        for msg in recent:
            role = msg["role"].upper()
            content = msg["content"][:800]
            lines.append(f"{role}: {content}")
        conversation = "\n".join(lines)
        prompt = (
            "Summarize the following conversation into a tight paragraph (max 300 words). "
            "Preserve key facts, decisions, and any open tasks or questions:\n\n"
            f"{conversation}"
        )
        try:
            if current_engine_name == "kimi" and self.kimi_engine:
                return await self.kimi_engine.invoke_ephemeral(prompt, model="default")
            elif current_engine_name == "claude" and self.engine:
                return await self.engine.invoke_ephemeral(prompt, model="haiku")
            elif self.kimi_api:
                return await self.kimi_api.complete(prompt, system="Summarize concisely.")
            elif self.claude_api:
                return await self.claude_api.complete(prompt, system="Summarize concisely.")
        except Exception as e:
            logger.warning(f"Failed to compact context: {e}")
        return ""

    # --- Cost / Quiet ---

    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        today = await self.db.get_cost_summary("today")
        week = await self.db.get_cost_summary("week")
        month = await self.db.get_cost_summary("month")
        text = (
            f"Today: {today['calls']} calls, {today['tokens']:,} tokens, ${today['cost_cents']/100:.2f}\n"
            f"This week: {week['calls']} calls, {week['tokens']:,} tokens, ${week['cost_cents']/100:.2f}\n"
            f"This month: {month['calls']} calls, {month['tokens']:,} tokens, ${month['cost_cents']/100:.2f}"
        )
        await update.message.reply_text(text)

    async def _cmd_quiet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        is_quiet = self.notifier.toggle_quiet()
        status = "ON — notifications suppressed" if is_quiet else "OFF — notifications active"
        await update.message.reply_text(f"Quiet mode: {status}")

    async def _cmd_kimi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        text = " ".join(context.args) if context.args else ""
        if not text:
            await update.message.reply_text("Usage: /kimi <message>")
            return
        if self.config.telegram.typing_indicator:
            await update.message.chat.send_action("typing")
        if self.kimi_engine:
            result = await self.kimi_engine.invoke_chat(message=text, source="telegram")
            await self._send_chunked(update.message, f"[Kimi CLI]\n{result.text}")
            await self.db.add_message("telegram", "assistant", result.text)
        elif self.kimi_api:
            response = await self.kimi_api.chat(text, source="telegram")
            await self._send_chunked(update.message, f"[Kimi API]\n{response}")
            await self.db.add_message("telegram", "assistant", response)
        else:
            await update.message.reply_text("Kimi is not configured.")

    # --- Session mode commands ---

    async def _cmd_end(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        active = self._active_code_session.pop(update.effective_user.id, None)
        if active:
            await update.message.reply_text(f"Exited {active['name']} session.")
        else:
            await update.message.reply_text("No active code session.")

    # --- Default message handler ---

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        user_message = update.message.text
        if not user_message:
            return

        await self.db.add_message("telegram", "user", user_message)

        sticky = self._active_code_session.get(update.effective_user.id)
        decision = self.router.classify(user_message, sticky_session=sticky)

        # Inject compacted context if present (one-time)
        context_prefix = self._switch_context.pop(update.effective_user.id, "")
        instruction = user_message
        if context_prefix:
            instruction = f"{context_prefix}\n\n{user_message}"

        if decision.use_kimi:
            if self.config.telegram.typing_indicator:
                await update.message.chat.send_action("typing")
            if self.kimi_engine:
                result = await self.kimi_engine.invoke_streaming(
                    instruction=instruction,
                    project_name="general-kimi",
                    project_dir=str(Path("~/.halos").expanduser()),
                    model="default",
                    source="telegram",
                )
                await self._send_chunked(update.message, f"[Kimi CLI]\n{result.text}")
                await self.db.add_message("telegram", "assistant", result.text)
            elif self.kimi_api:
                response = await self.kimi_api.chat(instruction, source="telegram")
                await self._send_chunked(update.message, f"[Kimi API]\n{response}")
                await self.db.add_message("telegram", "assistant", response)
            else:
                await update.message.reply_text("Kimi is not configured.")
            return

        if decision.use_fallback:
            # Fallback to OpenRouter
            if self.config.telegram.typing_indicator:
                await update.message.chat.send_action("typing")
            if self.claude_api:
                response = await self.claude_api.chat(instruction, source="telegram",
                                                       use_heavy=(decision.model == "opus"))
            elif self.kimi_api:
                response = await self.kimi_api.chat(instruction, source="telegram")
            else:
                response = "No LLM API configured."
            await self._send_chunked(update.message, response)
            await self.db.add_message("telegram", "assistant", response)
            return

        if decision.session_type == "project" and decision.project_name:
            self._active_code_session[update.effective_user.id] = {
                "name": decision.project_name,
                "project_dir": decision.project_dir,
            }
            await self._run_code(update, decision.project_name, decision.project_dir, user_message)
            return

        # Preferred engine override
        preferred = self._preferred_engine.get(update.effective_user.id)
        if preferred == "kimi" and not decision.use_kimi:
            if self.config.telegram.typing_indicator:
                await update.message.chat.send_action("typing")
            if self.kimi_engine:
                result = await self.kimi_engine.invoke_streaming(
                    instruction=instruction,
                    project_name="general-kimi",
                    project_dir=str(Path("~/.halos").expanduser()),
                    model="default",
                    source="telegram",
                )
                await self._send_chunked(update.message, f"[Kimi CLI]\n{result.text}")
                await self.db.add_message("telegram", "assistant", result.text)
            elif self.kimi_api:
                response = await self.kimi_api.chat(instruction, source="telegram")
                await self._send_chunked(update.message, f"[Kimi API]\n{response}")
                await self.db.add_message("telegram", "assistant", response)
            else:
                await update.message.reply_text("Kimi is not configured.")
            return

        # General chat via Claude Code engine
        if self.config.telegram.typing_indicator:
            await update.message.chat.send_action("typing")

        status_msg = await update.message.reply_text("Thinking...")

        debounce = self.config.claude_code.progress_debounce_secs
        last_progress_time = [0.0]

        async def on_progress(event: StreamEvent):
            now = time.time()
            if now - last_progress_time[0] < debounce:
                return
            last_progress_time[0] = now
            label = TOOL_LABELS.get(event.tool_name or "", f"Using {event.tool_name}...")
            try:
                await status_msg.edit_text(label)
            except Exception:
                pass

        try:
            result = await self.engine.invoke_chat(
                message=instruction,
                model=decision.model,
                on_progress=on_progress,
                source="telegram",
            )
            try:
                await status_msg.delete()
            except Exception:
                pass
            await self._send_chunked(update.message, result.text)
            await self.db.add_message("telegram", "assistant", result.text)

        except Exception as e:
            logger.error(f"Engine chat failed, falling back: {e}")
            try:
                await status_msg.delete()
            except Exception:
                pass
            if self.kimi_engine:
                try:
                    result = await self.kimi_engine.invoke_streaming(
                        instruction=instruction,
                        project_name="general-kimi",
                        project_dir=str(Path("~/.halos").expanduser()),
                        model="default",
                        source="telegram",
                    )
                    await self._send_chunked(update.message, result.text)
                    await self.db.add_message("telegram", "assistant", result.text)
                    return
                except Exception:
                    pass
            if self.kimi_api:
                response = await self.kimi_api.chat(instruction, source="telegram")
            elif self.claude_api:
                response = await self.claude_api.chat(user_message, source="telegram",
                                                       use_heavy=(decision.model == "opus"))
            else:
                response = "No LLM configured."
            await self._send_chunked(update.message, response)
            await self.db.add_message("telegram", "assistant", response)

    # --- Helpers ---

    async def _send_chunked(self, message, text: str):
        """Send text in chunks, handling Telegram flood control and rate limits."""
        max_len = self.config.telegram.max_message_length
        chunks = [text] if len(text) <= max_len else self._chunk_response(text, max_len)
        
        for chunk in chunks:
            for attempt in range(1, 4):  # 3 retries
                try:
                    await message.reply_text(chunk)
                    break  # Success, move to next chunk
                except RetryAfter as e:
                    wait = min(float(getattr(e, "retry_after", 5)), 30.0)
                    logger.warning(
                        f"TelegramBot flood control: waiting {wait:.1f}s "
                        f"(attempt {attempt}/3)"
                    )
                    if attempt >= 3:
                        logger.error("TelegramBot failed to send after 3 attempts")
                        return
                    await asyncio.sleep(wait)
                except (NetworkError, TimedOut) as e:
                    logger.warning(
                        f"TelegramBot network error: {type(e).__name__} "
                        f"(attempt {attempt}/3)"
                    )
                    if attempt >= 3:
                        return
                    await asyncio.sleep(2.0 * attempt)
                except (BadRequest, Forbidden, InvalidToken) as e:
                    logger.error(
                        f"TelegramBot permanent send failure: {type(e).__name__}: {e}"
                    )
                    return
                except Exception as e:
                    logger.exception(f"TelegramBot unexpected send error: {e}")
                    return

    def _chunk_response(self, text: str, max_len: int) -> list[str]:
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            split_at = text.rfind("\n\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind(" ", 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip()
        return chunks

    async def start(self):
        self.app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("memory", self._cmd_memory))
        self.app.add_handler(CommandHandler("remember", self._cmd_remember))
        self.app.add_handler(CommandHandler("forget", self._cmd_forget))
        self.app.add_handler(CommandHandler("tasks", self._cmd_tasks))
        self.app.add_handler(CommandHandler("add_task", self._cmd_add_task))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume_task))
        self.app.add_handler(CommandHandler("code", self._cmd_code))
        self.app.add_handler(CommandHandler("sessions", self._cmd_sessions))
        self.app.add_handler(CommandHandler("switch", self._cmd_switch))
        self.app.add_handler(CommandHandler("cost", self._cmd_cost))
        self.app.add_handler(CommandHandler("quiet", self._cmd_quiet))
        self.app.add_handler(CommandHandler("kimi", self._cmd_kimi))
        self.app.add_handler(CommandHandler("end", self._cmd_end))

        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        self.notifier.set_bot(self.app)

        logger.info("Telegram bot starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
