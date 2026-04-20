"""LLM API wrapper for HalOS (OpenRouter)."""

import time
from openai import AsyncOpenAI
from pathlib import Path

from .config import Config
from .memory import MemoryManager
from .db import Database


class ClaudeAPI:
    def __init__(self, config: Config, memory: MemoryManager, db: Database):
        self.config = config
        self.memory = memory
        self.db = db
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=config.anthropic.api_key,
        )
        self._personality: str = ""

    def load_personality(self, personality_path: str):
        """Load the personality prompt from disk."""
        path = Path(personality_path)
        if path.exists():
            self._personality = path.read_text()

    async def chat(self, user_message: str, source: str = "telegram",
                   use_heavy: bool = False, personality_override: str = None) -> str:
        """Send a message to the LLM and return the response."""

        # NOTE: Database saving is handled by the caller (agent_bot.py)
        # Don't save here to avoid duplicates

        memory_context = await self.memory.get_relevant_context(user_message)
        system_prompt = personality_override if personality_override else self._personality
        if memory_context:
            system_prompt += f"\n\n## Current Memory\n\n{memory_context}"

        history = await self.db.get_recent_messages(
            self.config.memory.max_conversation_history
        )

        messages = [{"role": "system", "content": system_prompt}] + history

        model = self.config.anthropic.model_heavy if use_heavy else self.config.anthropic.model

        start = time.time()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                max_tokens=self.config.anthropic.max_tokens,
                messages=messages,
            )

            assistant_message = response.choices[0].message.content
            duration_ms = int((time.time() - start) * 1000)

            tokens = 0
            cost = 0.0
            if response.usage:
                tokens = (response.usage.prompt_tokens or 0) + (response.usage.completion_tokens or 0)

            await self.db.log_task(
                task_type=f"chat_{source}",
                duration_ms=duration_ms,
                tokens_used=tokens,
                cost_cents=cost,
                success=True,
                summary=user_message[:100],
            )

            # NOTE: Database saving is handled by the caller (agent_bot.py)
            # Don't save here to avoid duplicates
            return assistant_message

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            await self.db.log_task(
                task_type=f"chat_{source}",
                duration_ms=duration_ms,
                success=False,
                summary=str(e)[:200],
            )
            return f"API error: {str(e)}"

    async def complete(self, prompt: str, system: str = None,
                       use_heavy: bool = False) -> str:
        """One-shot completion without conversation history. Used by tasks."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        model = self.config.anthropic.model_heavy if use_heavy else self.config.anthropic.model

        start = time.time()
        try:
            response = await self.client.chat.completions.create(
                model=model,
                max_tokens=self.config.anthropic.max_tokens,
                messages=messages,
            )
            duration_ms = int((time.time() - start) * 1000)
            tokens = 0
            if response.usage:
                tokens = (response.usage.prompt_tokens or 0) + (response.usage.completion_tokens or 0)

            await self.db.log_task(
                task_type="completion",
                duration_ms=duration_ms,
                tokens_used=tokens,
                success=True,
                summary=prompt[:100],
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"API error: {str(e)}"
