"""Outbound notification handler for HalOS."""

import asyncio
import logging
from datetime import datetime

import httpx
from telegram.error import NetworkError, TimedOut

from .config import Config

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: Config):
        self.config = config
        self._bot = None  # Set after main telegram bot starts
        self._agent_bots: list = []  # AgentBot instances as fallback/alternative
        self._quiet_mode = False

    def set_bot(self, bot):
        """Set the main telegram bot application for sending messages."""
        self._bot = bot

    def register_agent_bot(self, agent_bot):
        """Register an AgentBot so it can be used for proactive notifications."""
        self._agent_bots.append(agent_bot)

    def unregister_agent_bot(self, session_name: str):
        """Unregister an AgentBot by session name."""
        self._agent_bots = [ab for ab in self._agent_bots if ab.session_name != session_name]

    @property
    def chat_id(self) -> int:
        """Get the chat ID to send notifications to."""
        return (self.config.notifications.telegram_chat_id
                or (self.config.telegram.allowed_user_ids[0]
                    if self.config.telegram.allowed_user_ids else None))

    def is_quiet_hours(self) -> bool:
        """Check if we're in quiet hours."""
        if self._quiet_mode:
            return True
        now = datetime.now().strftime("%H:%M")
        start = self.config.notifications.quiet_hours_start
        end = self.config.notifications.quiet_hours_end
        if start < end:
            return start <= now < end
        else:  # Wraps midnight
            return now >= start or now < end

    def toggle_quiet(self) -> bool:
        """Toggle quiet mode. Returns new state."""
        self._quiet_mode = not self._quiet_mode
        return self._quiet_mode

    async def _send_with_retry(self, bot, chat_id: int, text: str) -> None:
        """Send a Telegram message with exponential backoff on transient errors.

        RALPLAN-DR v2.1 ADR-004: 3 attempts, 1s -> 2s -> 4s delays.
        Only retries transient network errors. Logic errors (bad chat_id,
        malformed message, permission denied) are NOT retried — they surface
        on first attempt so they can be logged and handled upstream.
        """
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                await bot.send_message(chat_id=chat_id, text=text)
                if attempt > 0:
                    logger.info(f"Telegram send succeeded on attempt {attempt + 1}")
                return
            except (httpx.RequestError, NetworkError, TimedOut) as e:
                last_exc = e
                delay = 2 ** attempt  # 1, 2, 4
                logger.warning(
                    f"Telegram send attempt {attempt + 1}/3 failed "
                    f"({type(e).__name__}: {e}); retrying in {delay}s"
                )
                if attempt < 2:
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def notify(self, message: str, urgent: bool = False):
        """Send a notification to Telegram via main bot or first available agent bot."""
        if not urgent and self.is_quiet_hours():
            logger.info(f"Suppressed notification during quiet hours: {message[:50]}...")
            return

        # Try main bot first, then agent bots
        if self._bot and self.chat_id:
            try:
                max_len = self.config.telegram.max_message_length
                if len(message) <= max_len:
                    await self._send_with_retry(self._bot.bot, self.chat_id, message)
                else:
                    chunks = []
                    remaining = message
                    while remaining:
                        if len(remaining) <= max_len:
                            chunks.append(remaining)
                            break
                        split_at = remaining.rfind("\n\n", 0, max_len)
                        if split_at == -1:
                            split_at = remaining.rfind("\n", 0, max_len)
                        if split_at == -1:
                            split_at = max_len
                        chunks.append(remaining[:split_at])
                        remaining = remaining[split_at:].lstrip()
                    for chunk in chunks:
                        await self._send_with_retry(self._bot.bot, self.chat_id, chunk)
                logger.info(f"Sent notification: {message[:50]}...")
                return
            except Exception as e:
                logger.error(f"Main bot notify failed: {e}")

        # Fallback to first available agent bot
        for agent_bot in self._agent_bots:
            try:
                if await agent_bot.push(message):
                    logger.info(f"Sent notification via agent bot [{agent_bot.session_name}]: {message[:50]}...")
                    return
            except Exception:
                continue

        logger.warning("Cannot notify: no working bot configured")
