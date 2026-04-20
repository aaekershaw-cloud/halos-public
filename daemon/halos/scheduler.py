"""Scheduled task management for HalOS."""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import yaml

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)

from .config import Config
from .db import Database
from .claude_api import ClaudeAPI
from .notifier import Notifier
from .tasks.health_check import HealthCheckTask
from .tasks.code_watch import CodeWatchTask
from .tasks.market_check import MarketCheckTask
from .tasks.digest import DigestTask
from .tasks.reddit_scout import RedditScoutTask
from .tasks.follow_accounts import FollowAccountsTask
from .model_selector import select_model, detect_task_type
from .task_runner import invoke_custom_task

logger = logging.getLogger(__name__)

TASK_CLASSES = {
    "health_check": HealthCheckTask,
    "code_watch": CodeWatchTask,
    "market_check": MarketCheckTask,
    "digest": DigestTask,
    "reddit_scout": RedditScoutTask,
    "follow_accounts": FollowAccountsTask,
}


class TaskScheduler:
    def __init__(self, config: Config, db: Database, claude_api: ClaudeAPI, notifier: Notifier,
                 engine=None, kimi_api=None, kimi_engine=None, skill_evolver=None):
        self.config = config
        self.db = db
        self.claude_api = claude_api
        self.kimi_api = kimi_api
        self.kimi_engine = kimi_engine
        self.engine = engine
        self.notifier = notifier
        self.skill_evolver = skill_evolver
        # (task_name, error_class) -> unix_ts of last notification.
        # RALPLAN-DR v2.1 ADR-005: suppress duplicate pages within 1h.
        # Observability (scheduled_task_runs inserts) is NOT gated on this.
        self._notify_dedup: dict[tuple[str, str], float] = {}
        # task_name -> ISO start time, recorded in _run_task for honest timestamps.
        self._run_starts: dict[str, str] = {}
        # task_name set of retries currently scheduled/running. Prevents the
        # 10-min poll loop from firing a second retry while the first is still in flight.
        self._retry_in_flight: set[str] = set()
        self.scheduler = AsyncIOScheduler(
            timezone=config.scheduler.timezone,
            job_defaults={
                "misfire_grace_time": 600,  # 10 min — prevents silent skips when event loop is busy
                "coalesce": True,           # if a job misfires multiple times, run it once
                "max_instances": 1,         # one instance of each job at a time
            },
        )

    async def load_tasks_from_yaml(self, tasks_path: str = "config/tasks.yaml"):
        """Load task definitions from YAML file and persist to DB.

        Malformed tasks.yaml is logged and skipped — daemon startup must not
        fail because of a broken scheduled task definition, otherwise agent
        Telegram bots never come online.
        """
        path = Path(tasks_path)
        if not path.exists():
            logger.info("No tasks.yaml found, skipping")
            return

        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            logger.error(
                f"Failed to parse {path}: {e}. "
                "Scheduled tasks will not load, but daemon startup will continue."
            )
            return
        except OSError as e:
            logger.error(f"Failed to read {path}: {e}. Skipping scheduled tasks.")
            return

        tasks = raw.get("tasks", {})
        for name, task_def in tasks.items():
            task_type = task_def.get("type", "custom")
            cron = task_def.get("cron", "0 * * * *")
            payload = {k: v for k, v in task_def.items() if k not in ("type", "cron")}
            await self.db.upsert_scheduled_task(name, task_type, cron, payload)
            logger.info(f"Loaded scheduled task: {name} ({task_type}, {cron})")

    async def start(self):
        """Load tasks from DB and start the scheduler."""
        tasks = await self.db.get_scheduled_tasks(enabled_only=True)

        for task in tasks:
            self._add_job(task)

        # RALPLAN-DR v2.1 ADR-003: listen for error/missed/max_instances events
        # so task outcomes are recorded to scheduled_task_runs and failures
        # bubble up as notifications (separately deduped).
        self.scheduler.add_listener(
            self._on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
        )

        self.scheduler.start()

        # Reconcile: remove any jobs not in the DB set (orphan guard).
        expected = {t["name"] for t in tasks}
        actual = {j.id for j in self.scheduler.get_jobs()}
        orphans = actual - expected
        for job_id in orphans:
            logger.warning(f"Removing orphaned scheduler job: {job_id}")
            self.scheduler.remove_job(job_id)

        logger.info(f"Scheduler started with {len(tasks)} task(s), removed {len(orphans)} orphan(s)")

        # Start polling for TUI-initiated reload requests
        asyncio.create_task(self._poll_reload_requests())
        # Auto-retry poll: re-runs custom tasks whose latest run failed/missed.
        asyncio.create_task(self._poll_missed_runs())

    def _on_job_event(self, event):
        """APScheduler listener — records every run outcome and notifies on failure.

        Runs synchronously inside APScheduler's internal dispatch; the async
        DB and notification work is scheduled onto the event loop via
        asyncio.ensure_future. Runs rows are ALWAYS written — notification is
        a separate step gated by the dedup dict (1h per (task, error_class)).
        """
        raw_job_id = event.job_id
        # Auto-retry jobs use id "<task>__retry_<ts>" so the listener can tell
        # them apart from organic cron fires. Strip the suffix for task_name.
        is_retry = "__retry_" in raw_job_id
        task_name = raw_job_id.split("__retry_")[0] if is_retry else raw_job_id
        finished_iso = datetime.now(timezone.utc).isoformat()
        started_iso = self._run_starts.pop(task_name, finished_iso)

        if event.code == EVENT_JOB_EXECUTED:
            status = "success"
            err_class: str | None = None
            err_msg: str | None = None
        elif event.code == EVENT_JOB_ERROR:
            status = "error"
            exc = getattr(event, "exception", None)
            err_class = type(exc).__name__ if exc else "UnknownError"
            err_msg = (str(exc)[:500] if exc else "") or None
        elif event.code == EVENT_JOB_MISSED:
            status = "missed"
            err_class = "Missed"
            err_msg = "Job missed its scheduled run time"
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            status = "max_instances"
            err_class = "MaxInstances"
            err_msg = "Previous instance still running when next fire triggered"
        else:
            return

        async def _record_and_notify() -> None:
            try:
                await self.db.insert_scheduled_task_run(
                    task_name=task_name,
                    started_ts=started_iso,
                    finished_ts=finished_iso,
                    status=status,
                    error_class=err_class,
                    error_msg=err_msg,
                    is_retry=is_retry,
                )
                await self.db.update_scheduled_task_status(
                    task_name=task_name,
                    status=status,
                    error=err_msg,
                    success_ts=finished_iso if status == "success" else None,
                )
            except Exception:
                logger.exception(f"Failed to record scheduled_task_run for {task_name}")
            finally:
                # Clear in-flight flag so the next poll can retry a future failure.
                self._retry_in_flight.discard(task_name)

            # Notification is separately gated — only fires on failure, deduped 1h.
            if status == "success" or not self.notifier:
                return
            dedup_key = (task_name, err_class or "")
            last = self._notify_dedup.get(dedup_key, 0.0)
            now = time.time()
            if now - last < 3600:
                logger.info(
                    f"Suppressed duplicate notification for {task_name}/{err_class} "
                    f"(last fired {int(now - last)}s ago)"
                )
                return
            self._notify_dedup[dedup_key] = now
            try:
                await self.notifier.notify(
                    f"[{task_name}] {status}: {err_class}: {err_msg or ''}",
                    urgent=True,
                )
            except Exception:
                logger.exception(f"Failed to notify for {task_name}")

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_record_and_notify())
            else:
                logger.warning(
                    f"No running event loop for {task_name} job event — skipping record"
                )
        except RuntimeError:
            logger.warning(f"No event loop available for {task_name} job event")

    def _add_job(self, task: dict):
        """Add a single scheduled job."""
        name = task["name"]
        cron = task["cron_expression"]

        try:
            trigger = CronTrigger.from_crontab(cron, timezone=self.config.scheduler.timezone)
        except Exception as e:
            logger.error(f"Invalid cron for {name}: {cron} ({e})")
            return

        self.scheduler.add_job(
            self._run_task,
            trigger=trigger,
            id=name,
            name=name,
            replace_existing=True,
            jitter=120,  # spread out tasks at the same minute by up to 2 min
            kwargs={"task_name": name, "task_type": task["task_type"], "payload": task.get("payload")},
        )
        logger.info(f"Scheduled: {name} -> {cron}")

    async def _run_task(self, task_name: str, task_type: str, payload: dict = None):
        """Execute a scheduled task.

        Wrapped in a top-level try/except so a single failing task never
        crashes the APScheduler executor or blocks subsequent jobs.
        """
        start_iso = datetime.now(timezone.utc).isoformat()
        self._run_starts[task_name] = start_iso
        logger.info(f"Running scheduled task: {task_name} ({task_type})")
        try:
            await self._run_task_inner(task_name, task_type, payload)
        except Exception:
            logger.exception(f"Scheduled task {task_name} failed")
            raise

    async def _run_task_inner(self, task_name: str, task_type: str, payload: dict = None):
        """Actual task execution logic (called by _run_task)."""
        await self.db.update_scheduled_task_run(task_name)

        if task_type == "custom":
            # Custom tasks use an LLM prompt
            prompt = (payload or {}).get("prompt", "")
            notify = (payload or {}).get("notify", False)
            session_name = (payload or {}).get("session", "")

            if prompt:
                result = await invoke_custom_task(
                    payload or {},
                    engine=self.engine,
                    kimi_engine=self.kimi_engine,
                    config=self.config,
                    claude_api=self.claude_api,
                    skill_evolver=self.skill_evolver,
                )

                # Agents can suppress notification by returning empty output or a skip marker.
                # Keeps scheduled tasks silent when there's nothing worth reporting.
                skip_markers = ("NO_MESSAGE", "[NO_NOTIFY]", "NO_ACTION", "NO_UPDATE")
                is_skip = (
                    not result
                    or any(result.upper().startswith(m) for m in skip_markers)
                    or result.startswith("API error")
                )

                if notify and self.notifier and not is_skip:
                    # Route notification through the agent's own bot if session is specified
                    if session_name and hasattr(self.notifier, '_agent_bots'):
                        agent_bot = next(
                            (b for b in self.notifier._agent_bots if b.session_name.lower() == session_name.lower()),
                            None
                        )
                        if agent_bot:
                            await agent_bot.push(f"[{task_name}]\n{result}")
                        else:
                            await self.notifier.notify(f"[{task_name}]\n{result}")
                    else:
                        await self.notifier.notify(f"[{task_name}]\n{result}")
                elif notify and is_skip:
                    logger.info(f"Task {task_name} signaled no-notify; skipping push")
                return

        task_cls = TASK_CLASSES.get(task_type)
        if not task_cls:
            logger.warning(f"Unknown task type: {task_type}")
            return

        task = task_cls(db=self.db, claude_api=self.claude_api, engine=self.engine, notifier=self.notifier, kimi_api=self.kimi_api, kimi_engine=self.kimi_engine)
        # For tasks with nested config, extract it
        config = (payload or {}).get("config", payload)
        await task.run(config)

    async def add_task(self, name: str, cron: str, description: str,
                       task_type: str = "custom", session: str = "") -> str:
        """Add a new scheduled task at runtime and persist to YAML."""
        payload = {"prompt": description, "notify": True, "description": description}
        if session:
            payload["session"] = session
        await self.db.upsert_scheduled_task(name, task_type, cron, payload)

        task_data = {
            "name": name,
            "task_type": task_type,
            "cron_expression": cron,
            "payload": payload,
        }
        self._add_job(task_data)

        # Persist to tasks.yaml so it survives restarts
        await self._save_task_to_yaml(name, task_type, cron, payload)

        return f"Scheduled '{name}' with cron '{cron}'"

    async def _save_task_to_yaml(self, name: str, task_type: str, cron: str, payload: dict,
                                  tasks_path: str = "config/tasks.yaml"):
        """Append a new task to tasks.yaml for persistence across restarts."""
        try:
            path = Path(tasks_path)
            if not path.exists():
                # Create new file with tasks: header
                yaml_content = {"tasks": {name: self._build_task_dict(task_type, cron, payload)}}
                path.write_text(yaml.dump(yaml_content, default_flow_style=False, sort_keys=False))
                return

            # Read existing content
            content = path.read_text()
            data = yaml.safe_load(content) or {}
            tasks = data.get("tasks", {})

            # Add or update task
            tasks[name] = self._build_task_dict(task_type, cron, payload)
            data["tasks"] = tasks

            # Write back
            path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
            logger.info(f"Saved task '{name}' to {tasks_path}")
        except Exception as e:
            logger.error(f"Failed to save task to YAML: {e}")

    def _build_task_dict(self, task_type: str, cron: str, payload: dict) -> dict:
        """Build a task dictionary matching tasks.yaml format."""
        task = {
            "type": task_type,
            "cron": cron,
        }

        # Map payload fields to YAML format
        if payload.get("session"):
            task["session"] = payload["session"]
        if payload.get("description"):
            task["description"] = payload["description"]
        if payload.get("prompt"):
            task["prompt"] = payload["prompt"]
        if payload.get("notify"):
            task["notify"] = payload["notify"]

        # Handle config for health_check and similar tasks
        if payload.get("config"):
            task["config"] = payload["config"]
        if payload.get("notify_on_failure"):
            task["notify_on_failure"] = payload["notify_on_failure"]

        return task

    async def pause_task(self, name: str) -> str:
        """Pause a scheduled task."""
        if await self.db.set_scheduled_task_enabled(name, False):
            try:
                self.scheduler.remove_job(name)
            except Exception:
                pass
            return f"Paused '{name}'"
        return f"Task '{name}' not found"

    async def resume_task(self, name: str) -> str:
        """Resume a paused task."""
        if await self.db.set_scheduled_task_enabled(name, True):
            tasks = await self.db.get_scheduled_tasks()
            for t in tasks:
                if t["name"] == name:
                    self._add_job(t)
                    return f"Resumed '{name}'"
        return f"Task '{name}' not found"

    async def list_tasks(self) -> str:
        """List all scheduled tasks."""
        tasks = await self.db.get_scheduled_tasks()
        if not tasks:
            return "No scheduled tasks."

        lines = []
        for t in tasks:
            status = "ON" if t["enabled"] else "OFF"
            last = t.get("last_run", "never") or "never"
            lines.append(f"[{status}] {t['name']} ({t['task_type']}) — {t['cron_expression']} — last: {last}")
        return "\n".join(lines)

    async def _poll_missed_runs(self):
        """Auto-retry failed/missed custom tasks once within 60 min of failure.

        Polls every 10 min. For each enabled custom task, reads the most recent
        scheduled_task_runs row. If it's non-success, not already a retry, and
        within the retry window, fires a one-shot APScheduler job whose id carries
        a "__retry_<ts>" suffix so _on_job_event flags the resulting run row as
        is_retry=True. That flag prevents the next poll from looping retries.
        """
        POLL_INTERVAL_S = 600        # 10 min
        RETRY_WINDOW_S = 3600        # retry within 60 min of failure, not later

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_S)
                tasks = await self.db.get_scheduled_tasks(enabled_only=True)
                now = datetime.now(timezone.utc)
                for t in tasks:
                    if t.get("task_type") != "custom":
                        continue
                    name = t["name"]
                    if name in self._retry_in_flight:
                        continue
                    latest = await self.db.get_latest_scheduled_task_run(name)
                    if not latest or latest["status"] == "success":
                        continue
                    if latest.get("is_retry"):
                        continue  # already retried once — give up, don't loop
                    ref_ts = latest.get("finished_ts") or latest.get("started_ts")
                    if not ref_ts:
                        continue
                    try:
                        ref_dt = datetime.fromisoformat(str(ref_ts).replace("Z", "+00:00"))
                        if ref_dt.tzinfo is None:
                            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    age_s = (now - ref_dt).total_seconds()
                    if age_s > RETRY_WINDOW_S:
                        continue
                    retry_id = f"{name}__retry_{int(time.time())}"
                    self._retry_in_flight.add(name)
                    try:
                        self.scheduler.add_job(
                            self._run_task,
                            trigger="date",
                            run_date=datetime.now() + timedelta(seconds=5),
                            id=retry_id,
                            name=retry_id,
                            kwargs={
                                "task_name": name,
                                "task_type": t["task_type"],
                                "payload": t.get("payload"),
                            },
                            misfire_grace_time=60,
                        )
                        logger.info(
                            f"Auto-retry scheduled for {name} "
                            f"(orig status={latest['status']}, age={int(age_s)}s)"
                        )
                    except Exception:
                        self._retry_in_flight.discard(name)
                        logger.exception(f"Failed to schedule retry for {name}")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in _poll_missed_runs")

    async def _poll_reload_requests(self):
        """Poll DB for TUI-initiated reload requests and hot-reload scheduler."""
        while True:
            try:
                await asyncio.sleep(5)  # Poll every 5 seconds
                if await self.db.check_scheduler_reload_request():
                    logger.info("TUI requested scheduler reload — reloading from YAML...")
                    await self._hot_reload()
                    await self.db.mark_scheduler_reload_processed()
                    logger.info("Scheduler reload complete")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in scheduler reload poll loop")

    async def _hot_reload(self):
        """Hot-reload scheduler from YAML without full daemon restart."""
        # Remove all existing jobs
        for job in self.scheduler.get_jobs():
            self.scheduler.remove_job(job.id)
        logger.info("Removed all existing scheduled jobs")

        # Reload tasks from YAML into DB
        await self.load_tasks_from_yaml()

        # Re-add jobs from DB
        tasks = await self.db.get_scheduled_tasks(enabled_only=True)
        for task in tasks:
            self._add_job(task)
        logger.info(f"Reloaded {len(tasks)} task(s) from YAML")

    def stop(self):
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
