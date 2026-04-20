"""Per-session Telegram bot for a named HalOS agent."""

import asyncio
import logging
import time
from pathlib import Path
from telegram import Update
from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TelegramError,
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
from .claude_code import ClaudeCodeEngine, StreamEvent, TOOL_LABELS
from .db import Database
from .model_selector import select_model, detect_task_type

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


class AgentBot:
    """A Telegram bot dedicated to a single named HalOS agent/session."""

    def __init__(
        self,
        session_name: str,
        token: str,
        project_dir: str,
        personality: str,
        engine: ClaudeCodeEngine,
        db: Database,
        config: Config,
        claude_api=None,
        kimi_api=None,
        kimi_engine=None,
        skill_evolver=None,
    ):
        self.session_name = session_name
        self.token = token
        self.project_dir = project_dir
        self.personality = personality
        self.engine = engine
        self.db = db
        self.config = config
        self.claude_api = claude_api
        self.kimi_api = kimi_api
        self.kimi_engine = kimi_engine
        self.skill_evolver = skill_evolver
        self.app = None
        self._preferred_engine: dict[int, str] = {}  # user_id -> "claude" | "kimi"
        self._switch_context: dict[int, str] = {}    # compacted context after /switch

    def _is_authorized(self, user_id: int) -> bool:
        if not self.config.telegram.allowed_user_ids:
            return False
        return user_id in self.config.telegram.allowed_user_ids

    # --- Commands ---

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return
        await update.message.reply_text(
            f"Agent '{self.session_name}' is online.\n"
            f"Working directory: {self.project_dir}\n"
            "Send me any message to start a conversation."
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        running = self.engine.get_running()
        is_running = self.session_name in running
        cost = await self.db.get_cost_summary("today")
        sessions = await self.db.get_active_sessions()
        session_info = next(
            (s for s in sessions if s["name"] == self.session_name), None
        )
        msg_count = session_info.get("message_count", 0) if session_info else 0
        last_active = session_info.get("last_message_at", "never") if session_info else "never"

        text = (
            f"Agent: {self.session_name}\n"
            f"Status: {'running' if is_running else 'idle'}\n"
            f"Messages: {msg_count}\n"
            f"Last active: {last_active}\n"
            f"Cost today: ${cost['cost_cents'] / 100:.4f}\n"
            f"Directory: {self.project_dir}"
        )
        await update.message.reply_text(text)

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        result = await self.engine.kill_session(self.session_name)
        await update.message.reply_text(f"Session cleared. {result}\nNew conversation will start on your next message.")

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        running = self.engine.get_running()
        if self.session_name in running:
            result = await self.engine.kill_session(self.session_name)
            await update.message.reply_text(f"Cancelled. {result}")
        else:
            await update.message.reply_text(f"[{self.session_name}] Nothing is currently running.")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        await update.message.reply_text(
            f"Agent: {self.session_name}\n\n"
            "Commands:\n"
            "/start — greeting\n"
            "/status — session info and cost\n"
            "/switch kimi|claude — change engine for this agent\n"
            "/clear — reset the conversation\n"
            "/cancel — stop the current task\n"
            "/skills — list evolved skills for this agent\n"
            "/crystallize — manually crystallize last turn into a skill\n"
            "/forget_skill <name> — delete a skill by name\n"
            "/help — this message\n\n"
            "Send any message to chat with this agent.\n\n"
            "Note: Tasks run until complete (no timeout). Use /cancel if needed."
        )

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        if not self.skill_evolver:
            await update.message.reply_text("Skill evolution not enabled.")
            return
        skills = await self.skill_evolver.list_skills(self.session_name)
        if not skills:
            await update.message.reply_text(f"[{self.session_name}] No evolved skills yet.")
            return
        lines = [f"[{self.session_name}] Evolved Skills ({len(skills)}):"]
        for s in skills:
            uses = s.get("usage_count", 0)
            lines.append(f"\n• {s['name']} (used {uses}x)\n  {s['content'][:120]}...")
        await update.message.reply_text("\n".join(lines[:50]))

    async def _cmd_crystallize(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        if not self.skill_evolver:
            await update.message.reply_text("Skill evolution not enabled.")
            return
        await update.message.reply_text(
            f"[{self.session_name}] Crystallization runs automatically after multi-tool turns. "
            f"Use /skills to see what has evolved so far."
        )

    async def _cmd_forget_skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        if not self.skill_evolver:
            await update.message.reply_text("Skill evolution not enabled.")
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /forget_skill <skill_name>")
            return
        name = " ".join(args).strip()
        ok = await self.skill_evolver.delete_skill_by_name(self.session_name, name)
        if ok:
            await update.message.reply_text(f"[{self.session_name}] Skill '{name}' deleted.")
        else:
            await update.message.reply_text(f"[{self.session_name}] Skill '{name}' not found.")

    async def _cmd_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        args = context.args or []
        if not args or args[0].lower() not in ("kimi", "claude"):
            await update.message.reply_text("Usage: /switch kimi  or  /switch claude")
            return
        choice = args[0].lower()
        user_id = update.effective_user.id

        # Determine current effective engine before switch
        override = self._preferred_engine.get(user_id)
        agent_cfg = self.config.agents.get(self.session_name, None)
        provider = getattr(agent_cfg, "provider", "") if agent_cfg else ""
        current = "claude"
        if override == "kimi" or (not override and provider.lower() == "kimi"):
            current = "kimi"

        if current != choice:
            summary = await self._compact_context(f"telegram:{self.session_name}", current)
            if summary:
                self._switch_context[user_id] = f"[Context from previous {current} conversation: {summary}]"

        self._preferred_engine[user_id] = choice
        await update.message.reply_text(f"Switched {self.session_name} to {choice}.")

    async def _compact_context(self, source: str, current_engine_name: str) -> str:
        """Summarize recent conversation history for engine switching."""
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
            logger.warning(f"Failed to compact context for {self.session_name}: {e}")
        return ""

    # --- Message handler ---

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return

        user_message = update.message.text

        # Handle photo attachments (compressed images)
        if not user_message and update.message.photo:
            try:
                photo = update.message.photo[-1]  # highest resolution
                file = await context.bot.get_file(photo.file_id, read_timeout=60, connect_timeout=20)

                # Use actual extension from Telegram's file_path, fallback to .jpg
                ext = Path(file.file_path).suffix if file.file_path else ".jpg"
                temp_dir = Path.home() / ".halos" / "temp"
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp_path = temp_dir / f"photo_{int(time.time())}{ext}"
                await file.download_to_drive(temp_path, read_timeout=60, connect_timeout=20)

                caption = update.message.caption or "What do you see in this image?"
                user_message = f"Read this image file: {temp_path}\n\n{caption}"
            except Exception as e:
                logger.error(f"[{self.session_name}] Failed to download photo: {e}")
                await update.message.reply_text("Sorry, I couldn't download that image. Please try again.")
                return

        # Handle document attachments (uncompressed images, PDFs)
        if not user_message and update.message.document:
            doc = update.message.document
            mime = doc.mime_type or ""
            if mime.startswith("image/") or mime == "application/pdf":
                try:
                    file = await context.bot.get_file(doc.file_id, read_timeout=60, connect_timeout=20)
                    ext = Path(doc.file_name).suffix if doc.file_name else Path(file.file_path).suffix if file.file_path else ""
                    temp_dir = Path.home() / ".halos" / "temp"
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    temp_path = temp_dir / f"doc_{int(time.time())}{ext}"
                    await file.download_to_drive(temp_path, read_timeout=60, connect_timeout=20)

                    caption = update.message.caption or "What do you see in this file?"
                    user_message = f"Read this file: {temp_path}\n\n{caption}"
                except Exception as e:
                    logger.error(f"[{self.session_name}] Failed to download document: {e}")
                    await update.message.reply_text("Sorry, I couldn't download that file. Please try again.")
                    return

        if not user_message:
            return

        await self.db.add_message(f"telegram:{self.session_name}", "user", user_message)

        if self.config.telegram.typing_indicator:
            await update.message.chat.send_action("typing")

        user_id = update.effective_user.id
        override = self._preferred_engine.get(user_id)

        # Inject compacted context if present (one-time)
        context_prefix = self._switch_context.pop(user_id, "")
        instruction = user_message
        if context_prefix:
            instruction = f"{context_prefix}\n\n{user_message}"

        agent_cfg = self.config.agents.get(self.session_name, None)
        provider = getattr(agent_cfg, "provider", "") if agent_cfg else ""
        agent_model = getattr(agent_cfg, "model", "") if agent_cfg else ""

        # Route to Kimi CLI if explicitly configured or overridden
        use_kimi_cli = provider.lower() == "kimi"
        use_kimi_api = (
            provider.lower() == "kimi_api"
            or agent_model.startswith("moonshot")
            or (not provider and self.kimi_api and not self.engine)
        )

        if override == "kimi":
            use_kimi_cli = True
            use_kimi_api = False
        elif override == "claude":
            use_kimi_cli = False
            use_kimi_api = False

        if use_kimi_cli and self.kimi_engine:
            lock = self.kimi_engine._get_lock(f"telegram:kimi:{self.session_name}")
            if lock.locked():
                status_msg = await update.message.reply_text(
                    f"[{self.session_name}] Queued — waiting for current task..."
                )
            else:
                status_msg = await update.message.reply_text(f"[{self.session_name}] Thinking...")

            debounce = self.config.claude_code.progress_debounce_secs
            last_progress_time = [0.0]

            async def on_progress(event):
                now = time.time()
                if now - last_progress_time[0] < debounce:
                    return
                last_progress_time[0] = now
                label = TOOL_LABELS.get(event.tool_name or "", f"Using {event.tool_name}...")
                try:
                    await status_msg.edit_text(f"[{self.session_name}] {label}")
                except Exception:
                    pass

            # CRITICAL: Acquire lock to prevent concurrent tool use in the same session
            async with lock:
                try:
                    # Update status to "Thinking" once we have the lock
                    try:
                        await status_msg.edit_text(f"[{self.session_name}] Thinking...")
                    except Exception:
                        pass

                    # Auto-select model based on task characteristics
                    task_type = detect_task_type(instruction)
                    model = select_model(
                        prompt=instruction,
                        task_type_hint=task_type,
                        force_model=agent_model if agent_model else None,
                    )
                    result = await self.kimi_engine.invoke_streaming(
                        instruction=instruction,
                        project_name=f"telegram:kimi:{self.session_name}",
                        project_dir=self.project_dir,
                        model=model,
                        on_progress=on_progress,
                        personality_override=self.personality,
                        source=f"telegram:{self.session_name}",
                    )
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    await self.db.add_message(f"telegram:{self.session_name}", "assistant", result.text)
                    await self._send_chunked(update.message, result.text)

                    # Fire-and-forget skill crystallization for multi-tool turns
                    if (
                        self.skill_evolver
                        and result.tool_calls
                        and len(result.tool_calls) >= 2
                        and not result.error
                    ):
                        asyncio.create_task(
                            self.skill_evolver.crystallize_turn(
                                agent=self.session_name,
                                source=f"telegram:{self.session_name}",
                                instruction=instruction,
                                result_text=result.text,
                                tool_calls=result.tool_calls,
                                model="haiku",
                            )
                        )
                except Exception as e:
                    logger.exception(f"AgentBot [{self.session_name}] kimi handle_message failed: {e}")
                    try:
                        await status_msg.edit_text(f"[{self.session_name}] Error — see reply")
                    except Exception:
                        pass
                    fallback = ""
                    if self.kimi_api:
                        try:
                            fallback = await self.kimi_api.chat(instruction, source=f"telegram:{self.session_name}", personality_override=self.personality)
                        except Exception:
                            pass
                    if fallback:
                        await self._send_chunked(update.message, fallback)
                        await self.db.add_message(f"telegram:{self.session_name}", "assistant", fallback)
                    else:
                        await self._send_chunked(
                            update.message,
                            f"Sorry, something went wrong: {type(e).__name__}: {e}"
                        )
            return

        if use_kimi_api and self.kimi_api:
            if self.config.telegram.typing_indicator:
                await update.message.chat.send_action("typing")
            response = await self.kimi_api.chat(instruction, source=f"telegram:{self.session_name}", personality_override=self.personality)
            await self.db.add_message(f"telegram:{self.session_name}", "assistant", response)
            await self._send_chunked(update.message, f"[Kimi]\n{response}")
            return

        # Check if lock exists and is locked, without creating it as side effect
        project_key = f"telegram:claude:{self.session_name.lower()}"
        lock = self.engine._session_locks.get(project_key)
        if lock and lock.locked():
            status_msg = await update.message.reply_text(
                f"[{self.session_name}] Queued — waiting for current task..."
            )
        else:
            status_msg = await update.message.reply_text(f"[{self.session_name}] Thinking...")

        debounce = self.config.claude_code.progress_debounce_secs
        last_progress_time = [0.0]

        async def on_progress(event: StreamEvent):
            now = time.time()
            if now - last_progress_time[0] < debounce:
                return
            last_progress_time[0] = now
            label = TOOL_LABELS.get(event.tool_name or "", f"Using {event.tool_name}...")
            try:
                await status_msg.edit_text(f"[{self.session_name}] {label}")
            except Exception:
                pass

        status_cleared = False
        # Lock is acquired inside invoke_streaming(), not here
        try:
            model = agent_model or self.config.claude_code.default_model
            logger.info(f"AgentBot [{self.session_name}] invoking Claude Code: model={model}, project_dir={self.project_dir}, personality_len={len(self.personality)}")
            # No timeout for human-initiated messages - let them run until complete
            result = await self.engine.invoke_streaming(
                instruction=instruction,
                project_name=f"telegram:claude:{self.session_name.lower()}",
                project_dir=self.project_dir,
                model=model,
                on_progress=on_progress,
                personality_override=self.personality,
                source=f"telegram:{self.session_name}",
                timeout=0,  # 0 = no timeout
            )
            logger.info(f"AgentBot [{self.session_name}] Claude Code returned: error={result.error}, text_len={len(result.text)}")

            try:
                await status_msg.delete()
                status_cleared = True
            except Exception:
                pass

            reply_text = result.text or ""
            if not reply_text.strip():
                reply_text = (
                    f"[{self.session_name}] Empty response from Claude "
                    f"(error={getattr(result, 'error', False)}). Check daemon log."
                )
                logger.warning(
                    f"AgentBot [{self.session_name}] empty result.text, "
                    f"error={getattr(result, 'error', False)}"
                )

            await self.db.add_message(f"telegram:{self.session_name}", "assistant", reply_text)
            await self._send_chunked(update.message, reply_text)

            # Fire-and-forget skill crystallization for multi-tool turns
            if (
                self.skill_evolver
                and result.tool_calls
                and len(result.tool_calls) >= 2
                and not result.error
            ):
                asyncio.create_task(
                    self.skill_evolver.crystallize_turn(
                        agent=self.session_name,
                        source=f"telegram:{self.session_name}",
                        instruction=instruction,
                        result_text=reply_text,
                        tool_calls=result.tool_calls,
                        model="haiku",
                    )
                )
        except Exception as e:
            logger.exception(f"AgentBot [{self.session_name}] handle_message failed: {e}")
            fallback = ""
            if self.kimi_api:
                try:
                    fallback = await self.kimi_api.chat(instruction, source=f"telegram:{self.session_name}")
                except Exception:
                    pass
            if not fallback and self.claude_api:
                try:
                    fallback = await self.claude_api.chat(instruction, source=f"telegram:{self.session_name}", personality_override=self.personality)
                except Exception:
                    pass
            try:
                if fallback:
                    await self._send_chunked(update.message, fallback)
                    await self.db.add_message(f"telegram:{self.session_name}", "assistant", fallback)
                else:
                    await self._send_chunked(
                        update.message,
                        f"[{self.session_name}] Sorry, something went wrong: {type(e).__name__}: {e}"
                    )
            except Exception as send_err:
                logger.exception(
                    f"AgentBot [{self.session_name}] failed to send error reply: {send_err}"
                )
        finally:
            if not status_cleared:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

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
                        f"AgentBot [{self.session_name}] flood control: waiting {wait:.1f}s "
                        f"(attempt {attempt}/3)"
                    )
                    if attempt >= 3:
                        logger.error(f"AgentBot [{self.session_name}] failed to send after 3 attempts")
                        return  # Give up on this chunk
                    await asyncio.sleep(wait)
                except (NetworkError, TimedOut) as e:
                    logger.warning(
                        f"AgentBot [{self.session_name}] network error: {type(e).__name__} "
                        f"(attempt {attempt}/3)"
                    )
                    if attempt >= 3:
                        return
                    await asyncio.sleep(2.0 * attempt)  # Exponential backoff
                except (BadRequest, Forbidden, InvalidToken) as e:
                    # Permanent errors - don't retry
                    logger.error(
                        f"AgentBot [{self.session_name}] permanent send failure: "
                        f"{type(e).__name__}: {e}"
                    )
                    return
                except Exception as e:
                    logger.exception(
                        f"AgentBot [{self.session_name}] unexpected send error: {e}"
                    )
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

    # --- Proactive messaging ---

    async def push(self, text: str) -> bool:
        """Send a proactive message to the configured chat ID.

        Retries transient Telegram errors (NetworkError, TimedOut, RetryAfter).
        Does not retry permanent errors (BadRequest, Forbidden, InvalidToken).
        """
        chat_id = self.config.notifications.telegram_chat_id
        if not self.app or not chat_id:
            logger.warning(
                f"AgentBot [{self.session_name}] push skipped: "
                f"app={'ready' if self.app else 'missing'}, chat_id={chat_id or 'missing'}"
            )
            return False

        max_len = self.config.telegram.max_message_length
        chunks = [text] if len(text) <= max_len else self._chunk_response(text, max_len)

        for chunk in chunks:
            if not await self._send_one(chat_id, chunk):
                return False
        return True

    async def _send_one(self, chat_id: int, text: str) -> bool:
        """Send a single chunk with retry on transient Telegram errors."""
        max_attempts = 3
        backoffs = [1.0, 2.0]  # seconds between attempts 1→2 and 2→3

        for attempt in range(1, max_attempts + 1):
            try:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
                return True
            except RetryAfter as e:
                # Telegram flood-control; server says how long to wait
                wait = min(float(getattr(e, "retry_after", 5)), 30.0)
                logger.warning(
                    f"AgentBot [{self.session_name}] push hit flood control "
                    f"(RetryAfter={wait:.1f}s, attempt {attempt}/{max_attempts})"
                )
                if attempt >= max_attempts:
                    logger.error(
                        f"AgentBot [{self.session_name}] push failed after "
                        f"{max_attempts} attempts: RetryAfter"
                    )
                    return False
                await asyncio.sleep(wait)
            except (NetworkError, TimedOut) as e:
                # Transient network errors — retry with backoff
                if attempt >= max_attempts:
                    logger.error(
                        f"AgentBot [{self.session_name}] push failed after "
                        f"{max_attempts} attempts: {type(e).__name__}: {e}"
                    )
                    return False
                wait = backoffs[attempt - 1]
                logger.warning(
                    f"AgentBot [{self.session_name}] push transient error "
                    f"{type(e).__name__} (attempt {attempt}/{max_attempts}), "
                    f"retrying in {wait:.1f}s: {e}"
                )
                await asyncio.sleep(wait)
            except (BadRequest, Forbidden, InvalidToken) as e:
                # Permanent errors — do not retry
                logger.error(
                    f"AgentBot [{self.session_name}] push permanent failure "
                    f"{type(e).__name__}: {e}"
                )
                return False
            except TelegramError as e:
                # Catch-all for other telegram errors — log type + message, no retry
                logger.error(
                    f"AgentBot [{self.session_name}] push failed "
                    f"{type(e).__name__}: {e}"
                )
                return False
            except Exception as e:
                # Non-telegram exceptions (asyncio, TypeError, etc.) — no retry
                logger.error(
                    f"AgentBot [{self.session_name}] push unexpected error "
                    f"{type(e).__name__}: {e}"
                )
                return False
        return False

    # --- Lifecycle ---

    async def start(self):
        self.app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        self.app.add_handler(CommandHandler("switch", self._cmd_switch))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("skills", self._cmd_skills))
        self.app.add_handler(CommandHandler("crystallize", self._cmd_crystallize))
        self.app.add_handler(CommandHandler("forget_skill", self._cmd_forget_skill))
        self.app.add_handler(
            MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, self._handle_message)
        )

        logger.info(f"AgentBot [{self.session_name}] starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info(f"AgentBot [{self.session_name}] online.")

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info(f"AgentBot [{self.session_name}] stopped.")
