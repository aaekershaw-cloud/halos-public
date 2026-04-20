"""Daily digest generator task."""

import logging
from datetime import datetime
from .base import BaseTask

logger = logging.getLogger(__name__)


class DigestTask(BaseTask):
    task_type = "digest"

    async def execute(self, payload: dict = None) -> dict:
        payload = payload or {}

        if not self.claude_api and not self.kimi_api:
            return {"success": False, "result": "No API configured"}

        # Gather context
        now = datetime.now()
        day_name = now.strftime("%A")
        date_str = now.strftime("%B %d, %Y")

        # Get recent task results
        recent_logs = []
        cursor = await self.db.db.execute(
            """SELECT task_type, success, summary, created_at
               FROM task_log WHERE DATE(created_at) = DATE('now', 'localtime')
               ORDER BY created_at DESC LIMIT 20"""
        )
        rows = await cursor.fetchall()
        for row in rows:
            recent_logs.append(f"- [{row[0]}] {'OK' if row[1] else 'FAIL'}: {row[2] or 'no summary'}")

        # Get memory for project context
        memory = getattr(self.claude_api, "memory", None) or getattr(self.kimi_api, "memory", None)
        memory_context = memory.get_flat_memory() if memory else ""

        prompt = f"""Generate a brief morning briefing for {day_name}, {date_str}.

Recent task activity:
{chr(10).join(recent_logs) if recent_logs else '- No tasks ran today yet'}

Context about active projects:
{memory_context[:2000] if memory_context else 'No memory loaded'}

Keep it concise — 5-8 bullet points max. Focus on what needs attention today.
If it's Monday, include a brief week-ahead outlook. If Friday, note anything to wrap up."""

        if self.kimi_api:
            result = await self.kimi_api.complete(
                prompt=prompt,
                system="You are Hal, the user's AI assistant. Be direct and actionable. No fluff.",
            )
        else:
            result = await self.claude_api.complete(
                prompt=prompt,
                system="You are Hal, the user's AI assistant. Be direct and actionable. No fluff.",
            )

        if self.notifier and not result.startswith("API error"):
            await self.notifier.notify(f"Good morning. Here's your {day_name} briefing:\n\n{result}")

        return {"success": not result.startswith("API error"), "result": result}
