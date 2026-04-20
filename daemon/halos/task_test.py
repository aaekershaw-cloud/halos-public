"""Regression harness for scheduled agent tasks.

Runs tasks with a `regression:` block in config/tasks.yaml, captures output,
and asserts against simple rules. Also exposes a one-shot runner for ad-hoc
smoke testing a single task by name.

WARNING: tasks invoke real agents, hit real APIs, and may have side effects
(email reads, DB writes, etc.). Annotate `regression:` only on tasks whose
invocation is safe to re-run frequently.

Usage:
    python -m halos task test                     # every task with regression:
    python -m halos task test <name> [<name>...]  # specific tasks
    python -m halos task run <name>               # run one task, print output

Regression schema (per task in config/tasks.yaml):
    regression:
      forbid: [substrings]    # fail if any appears in output (case-insensitive)
      require: [substrings]   # fail if any is missing
      expect: silent|message  # silent = NO_MESSAGE/empty; message = non-silent
      allow_empty: true       # empty output is OK (default false unless expect=silent)
      max_length: 4000        # characters
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .claude_code import ClaudeCodeEngine
from .config import load_config
from .db import Database
from .task_runner import invoke_custom_task

HALOS_ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = HALOS_ROOT / "config" / "tasks.yaml"
CONFIG_PATH = HALOS_ROOT / "config" / "config.yaml"

SKIP_MARKERS = ("NO_MESSAGE", "[NO_NOTIFY]", "NO_ACTION", "NO_UPDATE")


@dataclass
class TaskResult:
    name: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    output: str = ""
    seconds: float = 0.0


def _load_tasks(path: Path = TASKS_PATH) -> dict:
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("tasks") or {}


def _is_silent(output: str) -> bool:
    s = (output or "").strip()
    if not s:
        return True
    return any(s.upper().startswith(m) for m in SKIP_MARKERS)


def _assert(output: str, rules: dict) -> list[str]:
    reasons: list[str] = []
    stripped = (output or "").strip()
    lower = stripped.lower()

    expect = (rules.get("expect") or "").lower()
    silent = _is_silent(stripped)

    if expect == "silent" and not silent:
        reasons.append("expected silent output (NO_MESSAGE / empty)")
    elif expect == "message" and silent:
        reasons.append("expected a real message, got silent output")

    allow_empty = rules.get("allow_empty", expect == "silent")
    if not allow_empty and not stripped:
        reasons.append("empty output not allowed")

    for needle in rules.get("forbid") or []:
        if needle.lower() in lower:
            reasons.append(f"forbidden substring present: {needle!r}")

    for needle in rules.get("require") or []:
        if needle.lower() not in lower:
            reasons.append(f"required substring missing: {needle!r}")

    max_length = rules.get("max_length")
    if max_length and len(stripped) > max_length:
        reasons.append(f"output length {len(stripped)} > max {max_length}")

    return reasons


async def _build_engine():
    config = load_config(str(CONFIG_PATH))
    db = Database(config.db_path)
    await db.connect()
    engine = ClaudeCodeEngine(config, db, memory=None)
    return config, db, engine


async def _close_db(db):
    for attr in ("close", "disconnect"):
        fn = getattr(db, attr, None)
        if fn is None:
            continue
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                await res
            return
        except Exception:
            pass


async def _run_one(name: str, payload: dict, *, engine, config) -> TaskResult:
    t0 = time.monotonic()
    try:
        output = await invoke_custom_task(payload, engine=engine, config=config)
    except Exception as e:
        return TaskResult(
            name=name,
            passed=False,
            reasons=[f"exception: {e!r}"],
            seconds=time.monotonic() - t0,
        )
    elapsed = time.monotonic() - t0
    rules = payload.get("regression") or {}
    reasons = _assert(output, rules)
    return TaskResult(
        name=name, passed=not reasons, reasons=reasons, output=output, seconds=elapsed
    )


async def test_tasks(names: Optional[list[str]] = None) -> int:
    tasks = _load_tasks()
    if not tasks:
        print("No tasks found in config/tasks.yaml", file=sys.stderr)
        return 2

    if names:
        selected = {n: tasks[n] for n in names if n in tasks}
        missing = [n for n in names if n not in tasks]
        if missing:
            print(f"Unknown task(s): {', '.join(missing)}", file=sys.stderr)
            return 2
    else:
        selected = {n: p for n, p in tasks.items() if p.get("regression")}

    if not selected:
        print("No tasks matched (none have a `regression:` block).")
        return 0

    config, db, engine = await _build_engine()
    results: list[TaskResult] = []
    try:
        for name, payload in selected.items():
            print(f"→ {name} ... ", end="", flush=True)
            r = await _run_one(name, payload, engine=engine, config=config)
            status = "PASS" if r.passed else "FAIL"
            print(f"{status} ({r.seconds:.1f}s)")
            for reason in r.reasons:
                print(f"    ✗ {reason}")
            results.append(r)
    finally:
        await _close_db(db)

    failed = [r for r in results if not r.passed]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} passed.")
    return 1 if failed else 0


async def run_task(name: str) -> int:
    tasks = _load_tasks()
    if name not in tasks:
        print(f"Unknown task: {name}", file=sys.stderr)
        return 2

    config, db, engine = await _build_engine()
    try:
        output = await invoke_custom_task(tasks[name], engine=engine, config=config)
    finally:
        await _close_db(db)

    print(output)
    return 0


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if argv else 2)

    cmd, rest = argv[0], argv[1:]

    if cmd == "test":
        sys.exit(asyncio.run(test_tasks(rest or None)))
    elif cmd == "run":
        if not rest:
            print("usage: python -m halos task run <name>", file=sys.stderr)
            sys.exit(2)
        sys.exit(asyncio.run(run_task(rest[0])))
    else:
        print(f"Unknown subcommand: {cmd}\n", file=sys.stderr)
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
