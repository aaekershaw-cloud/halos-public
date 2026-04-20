"""URL health check task."""

import aiohttp
import logging
from .base import BaseTask

logger = logging.getLogger(__name__)


class HealthCheckTask(BaseTask):
    task_type = "health_check"

    async def execute(self, payload: dict = None) -> dict:
        payload = payload or {}
        url = payload.get("url")
        expected_status = payload.get("expect_status", 200)
        timeout_secs = payload.get("timeout", 15)

        if not url:
            return {"success": False, "result": "No URL specified"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_secs)) as resp:
                    if resp.status == expected_status:
                        return {"success": True, "result": f"{url} -> {resp.status} OK"}
                    else:
                        msg = f"{url} -> {resp.status} (expected {expected_status})"
                        if self.notifier:
                            await self.notifier.notify(f"Health check failed: {msg}", urgent=True)
                        return {"success": False, "result": msg}
        except aiohttp.ClientError as e:
            msg = f"{url} -> Connection error: {str(e)}"
            if self.notifier:
                await self.notifier.notify(f"Health check failed: {msg}", urgent=True)
            return {"success": False, "result": msg}
        except Exception as e:
            msg = f"{url} -> Error: {str(e)}"
            if self.notifier:
                await self.notifier.notify(f"Health check failed: {msg}", urgent=True)
            return {"success": False, "result": msg}
