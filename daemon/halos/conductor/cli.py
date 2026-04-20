"""CLI entrypoint for Foreman's browser conductor.

Usage (typical):
    python -m halos.conductor start --tabs "Builder:claude,Reviewer:kimi"
    python -m halos.conductor read Builder --lines 40
    python -m halos.conductor type Builder "write a function that..." --submit
    python -m halos.conductor wait-idle Builder --timeout 180
    python -m halos.conductor stop
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from . import driver, state

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
DEFAULT_WEB_ORIGIN = "http://127.0.0.1:3000"
TOKEN_FILE = Path.home() / ".halos" / "web_token"
PROFILE_DIR = Path.home() / ".halos" / "chromium-profile"


def _load_web_token() -> str:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"Web token not found at {TOKEN_FILE}. Start the web daemon first.")
    tok = TOKEN_FILE.read_text().strip()
    if not tok:
        raise SystemExit(f"Web token file {TOKEN_FILE} is empty.")
    return tok


def _build_tabs_url(tabs: str, web_origin: str) -> str:
    token = _load_web_token()
    return f"{web_origin}/?token={quote(token)}&tabs={quote(tabs, safe=':,')}"


def _chromium_executable() -> str:
    with sync_playwright() as p:
        return p.chromium.executable_path


def cmd_start(args: argparse.Namespace) -> int:
    existing = state.load()
    if existing and state.pid_alive(existing.pid):
        print(f"Already running: pid={existing.pid} tabs={existing.tabs_url}")
        return 0
    if existing:
        state.clear()

    tabs_url = _build_tabs_url(args.tabs, args.web_origin)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    chromium = _chromium_executable()
    cmd = [
        chromium,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        tabs_url,
    ]
    if args.headless:
        cmd.insert(1, "--headless=new")

    # Detach: start_new_session so the child survives when our CLI exits.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait briefly for CDP to come up.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.3)
    else:
        proc.terminate()
        raise SystemExit(f"Chromium failed to expose CDP on port {CDP_PORT}")

    st = state.ConductorState.new(
        pid=proc.pid,
        cdp_url=CDP_URL,
        tabs_url=tabs_url,
        user_data_dir=str(PROFILE_DIR),
    )
    state.save(st)
    print(f"Started: pid={proc.pid} cdp={CDP_URL}")
    print(f"Tabs URL: {tabs_url}")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    st = state.load()
    if not st:
        print("Not running (no state file).")
        return 0
    if state.pid_alive(st.pid):
        try:
            os.kill(st.pid, signal.SIGTERM)
            for _ in range(20):
                if not state.pid_alive(st.pid):
                    break
                time.sleep(0.2)
            if state.pid_alive(st.pid):
                os.kill(st.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    state.clear()
    print(f"Stopped pid={st.pid}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    st = state.load()
    if not st:
        print("not running")
        return 1
    alive = state.pid_alive(st.pid)
    print(json.dumps({
        "pid": st.pid,
        "alive": alive,
        "cdp_url": st.cdp_url,
        "tabs_url": st.tabs_url,
        "started_at": st.started_at,
    }, indent=2))
    return 0 if alive else 1


def _require_running() -> state.ConductorState:
    st = state.load()
    if not st or not state.pid_alive(st.pid):
        raise SystemExit("Conductor not running. Run `python -m halos.conductor start ...` first.")
    return st


def cmd_read(args: argparse.Namespace) -> int:
    st = _require_running()
    with driver.connect(st.cdp_url, st.tabs_url) as page:
        text = driver.read_terminal(page, args.tab, lines=args.lines)
    print(text)
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    st = _require_running()
    with driver.connect(st.cdp_url, st.tabs_url) as page:
        driver.type_into(page, args.tab, args.text, submit=args.submit)
    print(f"typed {len(args.text)} chars into {args.tab}{' + Enter' if args.submit else ''}")
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    st = _require_running()
    with driver.connect(st.cdp_url, st.tabs_url) as page:
        driver.press_keys(page, args.tab, args.keys)
    print(f"pressed {args.keys} on {args.tab}")
    return 0


def cmd_wait_idle(args: argparse.Namespace) -> int:
    st = _require_running()
    with driver.connect(st.cdp_url, st.tabs_url) as page:
        ok = driver.wait_idle(
            page, args.tab,
            timeout_s=args.timeout,
            stable_ms=args.stable_ms,
        )
    print("idle" if ok else "timeout")
    return 0 if ok else 2


def cmd_snapshot(args: argparse.Namespace) -> int:
    st = _require_running()
    with driver.connect(st.cdp_url, st.tabs_url) as page:
        snap = driver.full_snapshot(page)
    out = json.dumps(snap, indent=2)
    if args.out:
        Path(args.out).write_text(out)
        print(f"wrote {len(out)} bytes to {args.out}")
    else:
        print(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="halos.conductor", description=__doc__.strip().splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="launch Chromium with the two-tab HalOS TUI")
    p_start.add_argument("--tabs", required=True,
                         help='tab spec, e.g. "Builder:claude,Reviewer:kimi"')
    p_start.add_argument("--web-origin", default=DEFAULT_WEB_ORIGIN)
    p_start.add_argument("--headless", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="kill the running Chromium")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="show whether the conductor is running")
    p_status.set_defaults(func=cmd_status)

    p_read = sub.add_parser("read", help="read the last N lines from a tab")
    p_read.add_argument("tab")
    p_read.add_argument("--lines", type=int, default=40)
    p_read.set_defaults(func=cmd_read)

    p_type = sub.add_parser("type", help="type text into a tab")
    p_type.add_argument("tab")
    p_type.add_argument("text")
    p_type.add_argument("--submit", action="store_true", help="press Enter after typing")
    p_type.set_defaults(func=cmd_type)

    p_keys = sub.add_parser("keys", help="press keys into a tab (Playwright key syntax, e.g. 'Control+c')")
    p_keys.add_argument("tab")
    p_keys.add_argument("keys")
    p_keys.set_defaults(func=cmd_keys)

    p_wait = sub.add_parser("wait-idle", help="block until a tab's output stabilizes")
    p_wait.add_argument("tab")
    p_wait.add_argument("--timeout", type=float, default=120.0)
    p_wait.add_argument("--stable-ms", type=int, default=2000)
    p_wait.set_defaults(func=cmd_wait_idle)

    p_snap = sub.add_parser("snapshot", help="dump the full a11y tree")
    p_snap.add_argument("--out", default=None)
    p_snap.set_defaults(func=cmd_snapshot)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
