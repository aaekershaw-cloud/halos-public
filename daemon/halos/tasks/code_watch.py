"""Error log monitoring task."""

import logging
from pathlib import Path
from .base import BaseTask

logger = logging.getLogger(__name__)


class CodeWatchTask(BaseTask):
    task_type = "code_watch"

    async def execute(self, payload: dict = None) -> dict:
        payload = payload or {}
        log_path = payload.get("log_path")
        pattern = payload.get("pattern", "ERROR|CRITICAL")
        tail_lines = payload.get("tail_lines", 50)

        if not log_path:
            return {"success": False, "result": "No log_path specified"}

        path = Path(log_path).expanduser()
        if not path.exists():
            return {"success": True, "result": f"Log file not found: {log_path}"}

        try:
            lines = path.read_text().splitlines()
            tail = lines[-tail_lines:] if len(lines) > tail_lines else lines

            import re
            matches = [l for l in tail if re.search(pattern, l)]

            if not matches:
                return {"success": True, "result": f"No {pattern} entries in last {tail_lines} lines"}

            summary = f"Found {len(matches)} matching lines:\n" + "\n".join(matches[:10])
            if len(matches) > 10:
                summary += f"\n... and {len(matches) - 10} more"

            if self.notifier:
                await self.notifier.notify(f"Log alert ({path.name}):\n{summary}")

            return {"success": True, "result": summary}
        except Exception as e:
            return {"success": False, "result": f"Error reading log: {str(e)}"}
