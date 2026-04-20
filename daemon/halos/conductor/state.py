"""Conductor state file — tracks the running Chromium's PID and CDP endpoint."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

STATE_PATH = Path.home() / ".halos" / "conductor.json"


@dataclass
class ConductorState:
    pid: int
    cdp_url: str
    tabs_url: str
    user_data_dir: str
    started_at: str

    @classmethod
    def new(cls, pid: int, cdp_url: str, tabs_url: str, user_data_dir: str) -> "ConductorState":
        return cls(
            pid=pid,
            cdp_url=cdp_url,
            tabs_url=tabs_url,
            user_data_dir=user_data_dir,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )


def save(state: ConductorState) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(asdict(state), indent=2))


def load() -> Optional[ConductorState]:
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        return ConductorState(**data)
    except (json.JSONDecodeError, TypeError):
        return None


def clear() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
