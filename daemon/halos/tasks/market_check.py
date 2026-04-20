"""Market/trading check task (custom prompt-based)."""

import logging
from .base import BaseTask

logger = logging.getLogger(__name__)


class MarketCheckTask(BaseTask):
    task_type = "market_check"

    async def execute(self, payload: dict = None) -> dict:
        payload = payload or {}
        prompt = payload.get("prompt", "Summarize current market conditions and any positions that need attention.")

        if not self.claude_api and not self.kimi_api:
            return {"success": False, "result": "No API configured"}

        if self.kimi_api:
            result = await self.kimi_api.complete(
                prompt=prompt,
                system="You are a concise financial assistant. Keep responses brief and actionable.",
            )
        else:
            result = await self.claude_api.complete(
                prompt=prompt,
                system="You are a concise financial assistant. Keep responses brief and actionable.",
            )

        if self.notifier and not result.startswith("API error"):
            await self.notifier.notify(f"Market check:\n{result}")

        return {"success": not result.startswith("API error"), "result": result}
