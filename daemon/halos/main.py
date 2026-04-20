"""HalOS daemon entry point."""

import asyncio
import logging
import os
import signal
from pathlib import Path

from .config import load_config
from .db import Database
from .memory import MemoryManager
from .claude_api import ClaudeAPI
from .kimi_api import KimiAPI
from .claude_code import ClaudeCodeEngine
from .kimi_code import KimiCodeEngine
from .router import Router
from .scheduler import TaskScheduler
from .notifier import Notifier
from .telegram_bot import TelegramBot
from .agent_bot import AgentBot
from .skill_evolution import SkillEvolver

logger = logging.getLogger(__name__)


async def _poll_agent_reload(config, engine, db, notifier, agent_bots_list: list, claude_api, kimi_api, kimi_engine):
    """Poll for agent reload marker file and hot-reload agents without daemon restart.

    Checks ~/.halos/reload_agents every 5 seconds. When marker exists:
    - Reloads config from disk
    - Diffs current vs new agent list
    - Starts new AgentBots, stops removed ones
    - Deletes marker after processing

    Pattern mirrors scheduler.py:452-481 (proven polling-based reload).
    """
    reload_marker = Path.home() / ".halos" / "reload_agents"
    logger.info("Agent reload polling started (checking every 5s)")

    try:
        while True:
            await asyncio.sleep(5)

            if not reload_marker.exists():
                continue

            try:
                logger.info("Agent reload marker detected — reloading config")

                # Reload config from disk
                new_config = load_config()

                # Build set of current and new agent names
                current_agents = {ab.session_name for ab in agent_bots_list}
                new_agents = set(new_config.agents.keys())

                # Diff: determine which agents to start/stop
                to_start = new_agents - current_agents
                to_stop = current_agents - new_agents
                unchanged = current_agents & new_agents

                logger.info(
                    f"Agent reload diff: {len(to_start)} to start, "
                    f"{len(to_stop)} to stop, {len(unchanged)} unchanged"
                )

                # Stop removed agents
                for agent_name in to_stop:
                    agent_bot = next((ab for ab in agent_bots_list if ab.session_name == agent_name), None)
                    if agent_bot:
                        logger.info(f"Stopping AgentBot [{agent_name}]")
                        await agent_bot.stop()
                        agent_bots_list.remove(agent_bot)
                        notifier.unregister_agent_bot(agent_name)

                # Start new agents
                for agent_name in to_start:
                    agent_config = new_config.agents[agent_name]
                    token = agent_config.bot_token
                    if not token:
                        logger.warning(f"AgentBot [{agent_name}]: no bot_token, skipping")
                        continue

                    project_dir = agent_config.project_dir or engine.project_map.get(agent_name, "")
                    project_dir = str(Path(project_dir).expanduser()) if project_dir else str(Path.home())

                    # Load soul.md (identity layer)
                    personality = ""
                    for fname in ("soul.md", "personality.md"):
                        p = Path(project_dir) / fname
                        if p.exists():
                            personality = p.read_text().strip()
                            logger.info(f"AgentBot [{agent_name}]: loaded {fname}")
                            break

                    agent_bot = AgentBot(
                        session_name=agent_name,
                        token=token,
                        project_dir=project_dir,
                        personality=personality,
                        engine=engine,
                        db=db,
                        config=new_config,
                        claude_api=claude_api,
                        kimi_api=kimi_api,
                        kimi_engine=kimi_engine,
                        skill_evolver=skill_evolver,
                    )

                    logger.info(f"Starting AgentBot [{agent_name}]")
                    await agent_bot.start()
                    agent_bots_list.append(agent_bot)
                    notifier.register_agent_bot(agent_bot)

                logger.info(
                    f"Agent reload complete: {len(agent_bots_list)} total agent bot(s) now running"
                )

                # Delete marker after successful reload
                reload_marker.unlink()

            except Exception as e:
                logger.exception(f"Agent reload failed: {e}")
                # Delete marker even on failure to prevent retry loop
                try:
                    reload_marker.unlink()
                except Exception:
                    pass

    except asyncio.CancelledError:
        logger.info("Agent reload polling stopped")
        raise


async def main():
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Write PID file so TUI /restart can find and kill us
    pid_path = Path.home() / ".halos" / "daemon.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    # Singleton guard: refuse to start if another daemon is already running.
    # When HALOS_SUPERVISED=1, launchd is the single source of truth for
    # uniqueness; PID file is informational only. Skip the PID check so a
    # launchd-triggered restart doesn't thrash against a stale PID file that
    # the previous (killed) process never got to clean up.
    # See RALPLAN-DR v2.1 ADR-007.
    supervised = os.environ.get("HALOS_SUPERVISED") == "1"
    if not supervised and pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)  # check if alive (signal 0 = no-op)
            # Still alive — refuse to start a second instance
            logger.error(
                f"Another daemon is already running (PID {old_pid}). "
                f"Kill it first or delete {pid_path}"
            )
            return
        except (ProcessLookupError, ValueError):
            pass  # PID file stale or invalid — safe to proceed
        except PermissionError:
            # Process exists but we can't signal it — assume still running
            logger.error(
                f"Another daemon is running (PID {old_pid}) but inaccessible. "
                f"Kill it first or delete {pid_path}"
            )
            return

    pid_path.write_text(str(os.getpid()))
    logger.info(f"Starting {config.name} (PID {os.getpid()})...")

    if not config.telegram.allowed_user_ids:
        raise RuntimeError("No allowed_user_ids configured in config.yaml")

    # Database
    db = Database(config.db_path)
    await db.connect()
    logger.info(f"Database connected: {config.db_path}")

    # Memory
    memory = MemoryManager(config.memory_path, db)
    await memory.load()
    dest = Path(config.memory_path)
    if not dest.exists():
        src = Path("config/memory.md")
        if src.exists():
            dest.write_text(src.read_text())
    logger.info("Memory loaded")

    # Claude Code engine (primary)
    engine = ClaudeCodeEngine(config, db, memory, skill_evolver=skill_evolver)
    personality_path = Path("config/personality.md")
    if personality_path.exists():
        engine.load_personality(str(personality_path))
        logger.info("Personality loaded")
    logger.info(f"Claude Code engine: {len(engine.project_map)} projects configured")

    # Remote agent connectivity check (non-blocking, advisory only)
    remote_host = config.claude_code.remote_host
    if remote_host:
        for agent_name, agent_cfg in config.agents.items():
            if not agent_cfg.remote or not agent_cfg.remote_project_dir:
                continue
            remote_path = agent_cfg.remote_project_dir
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-T",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=5",
                    "-o", "BatchMode=yes",
                    remote_host,
                    f"test -f {remote_path}/CLAUDE.md",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=10.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning(
                        f"Remote SSH check timed out for [{agent_name}] at {remote_host}:{remote_path} — skipping"
                    )
                    continue
                if proc.returncode == 0:
                    logger.info(f"Remote config verified at {remote_host}:{remote_path} for [{agent_name}]")
                else:
                    logger.warning(
                        f"Remote CLAUDE.md not found at {remote_host}:{remote_path}/CLAUDE.md "
                        f"for [{agent_name}] — run scripts/sync-remote.sh"
                    )
            except Exception as exc:
                logger.warning(
                    f"Remote SSH check failed for [{agent_name}] at {remote_host}: {exc} — continuing"
                )

    # Kimi CLI engine
    kimi_engine = KimiCodeEngine(config, db, memory, skill_evolver=skill_evolver)
    if personality_path.exists():
        kimi_engine.load_personality(str(personality_path))
        logger.info("Kimi CLI engine loaded")
    logger.info(f"Kimi CLI engine: {len(kimi_engine.project_map)} projects configured")

    # Claude API (fallback only — requires OPENROUTER_API_KEY)
    claude_api = None
    if config.anthropic.api_key:
        claude_api = ClaudeAPI(config, memory, db)
        if personality_path.exists():
            claude_api.load_personality(str(personality_path))
        logger.info("Claude API (OpenRouter fallback) ready")
    else:
        logger.info("No OPENROUTER_API_KEY — OpenRouter fallback disabled")

    # Kimi API (native Moonshot)
    kimi_api = None
    if config.kimi.api_key:
        kimi_api = KimiAPI(config, memory, db)
        if personality_path.exists():
            kimi_api.load_personality(str(personality_path))
        logger.info("Kimi API ready")
    else:
        logger.info("No KIMI_API_KEY — Kimi fallback disabled")

    # Skill evolution (self-evolving agent layer)
    skill_evolver = SkillEvolver(db, claude_api=claude_api, kimi_api=kimi_api)
    await skill_evolver.ensure_schema()
    logger.info("Skill evolution layer ready")

    # Router
    router = Router(config, engine)

    # Notifier
    notifier = Notifier(config)

    # Scheduler
    scheduler = TaskScheduler(config, db, claude_api, notifier, engine=engine, kimi_api=kimi_api, kimi_engine=kimi_engine, skill_evolver=skill_evolver)
    await scheduler.load_tasks_from_yaml()
    await scheduler.start()
    logger.info("Scheduler started")

    # Telegram bot (main daemon bot — optional)
    bot = None
    if config.telegram.bot_token:
        try:
            bot = TelegramBot(config, router, engine, claude_api, memory, scheduler, notifier, db,
                              kimi_api=kimi_api, kimi_engine=kimi_engine)
            await bot.start()
            logger.info(f"{config.name} main bot is online.")
        except Exception as e:
            logger.warning(f"Main bot failed to start (token may be invalid): {e}")
            bot = None
    else:
        logger.info("No TELEGRAM_BOT_TOKEN — main bot disabled. Using agent bots only.")

    logger.info(f"{config.name} is online.")

    # Per-agent bots
    agent_bots: list[AgentBot] = []
    for agent_name, agent_config in config.agents.items():
        token = agent_config.bot_token
        if not token:
            logger.warning(f"AgentBot [{agent_name}]: no bot_token configured, skipping")
            continue

        project_dir = agent_config.project_dir or engine.project_map.get(agent_name, "")
        project_dir = str(Path(project_dir).expanduser()) if project_dir else str(Path.home())

        # Load soul.md (identity layer) — fall back to personality.md if not present
        personality = ""
        for fname in ("soul.md", "personality.md"):
            p = Path(project_dir) / fname
            if p.exists():
                personality = p.read_text().strip()
                logger.info(f"AgentBot [{agent_name}]: loaded {fname}")
                break
        if not personality:
            logger.info(f"AgentBot [{agent_name}]: no soul.md or personality.md in {project_dir}")

        agent_bot = AgentBot(
            session_name=agent_name,
            token=token,
            project_dir=project_dir,
            personality=personality,
            engine=engine,
            db=db,
            config=config,
            claude_api=claude_api,
            kimi_api=kimi_api,
            kimi_engine=kimi_engine,
            skill_evolver=skill_evolver,
        )
        agent_bots.append(agent_bot)

    if agent_bots:
        await asyncio.gather(*[ab.start() for ab in agent_bots])
        for ab in agent_bots:
            notifier.register_agent_bot(ab)
        logger.info(f"Started {len(agent_bots)} agent bot(s): {', '.join(ab.session_name for ab in agent_bots)}")

    # RALPLAN-DR v2.1 ADR-004: urgent startup ping bypasses quiet hours so a
    # dead-and-recovered daemon is noticed even at 3am. Uses the retry wrapper
    # via Notifier.notify.
    try:
        task_count = len(await db.get_scheduled_tasks(enabled_only=True))
        await notifier.notify(
            f"{config.name} daemon online (PID {os.getpid()}) — "
            f"{len(agent_bots)} agent bot(s), {task_count} scheduled task(s)",
            urgent=True,
        )
    except Exception:
        logger.exception("Startup ping failed")

    # Keep running
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Start agent reload polling task
    reload_task = asyncio.create_task(
        _poll_agent_reload(config, engine, db, notifier, agent_bots, claude_api, kimi_api, kimi_engine)
    )

    await stop_event.wait()

    # Cancel reload polling on shutdown
    reload_task.cancel()
    try:
        await reload_task
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down...")
    scheduler.stop()
    if bot:
        await bot.stop()
    for ab in agent_bots:
        await ab.stop()
    await db.close()
    # Remove PID file on clean exit
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info(f"{config.name} is offline.")


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
