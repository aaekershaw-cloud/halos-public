"""aiohttp WebSocket-PTY bridge that hosts a browser-accessible HalOS TUI.

Run with:
    python -m halos.web                     # localhost:3000, default cmd = halos tui
    python -m halos.web --host 0.0.0.0      # expose on LAN (use with Tailscale/SSH tunnel)
    python -m halos.web --port 3100

Per WebSocket connection: a fresh PTY is allocated, the command is spawned
attached to the slave side, and bytes are proxied both ways. Resize is an
in-band escape `\\x1b[RESIZE:cols;rows]` from the wterm client, matching the
protocol in vercel-labs/wterm's `examples/local/server.ts`.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import pty
import re
import secrets
import signal
import struct
import sys
import termios
import urllib.request
from pathlib import Path

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
WASM_PATH = STATIC_DIR / "wterm.wasm"
WTERM_VERSION = "0.1.8"
WASM_CDN = f"https://cdn.jsdelivr.net/npm/@wterm/core@{WTERM_VERSION}/wasm/wterm.wasm"
RESIZE_RE = re.compile(r"\x1b\[RESIZE:(\d+);(\d+)\]")
HALOS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CMD = [sys.executable, "-m", "halos", "tui"]
TOKEN_COOKIE = "halos_token"
PUBLIC_PATHS = {"/wterm.wasm"}


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _load_or_create_token() -> str:
    """Persist a stable token in ~/.halos/web_token so it survives restarts."""
    path = Path.home() / ".halos" / "web_token"
    if path.exists():
        tok = path.read_text().strip()
        if tok:
            return tok
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(18)
    path.write_text(tok)
    os.chmod(path, 0o600)
    return tok


def _ensure_wasm() -> None:
    """Download wterm.wasm from jsdelivr once; cached locally afterward."""
    if WASM_PATH.exists():
        return
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Fetching wterm.wasm from {WASM_CDN}")
    with urllib.request.urlopen(WASM_CDN, timeout=30) as r:
        WASM_PATH.write_bytes(r.read())
    logger.info(f"Cached wterm.wasm ({WASM_PATH.stat().st_size} bytes)")


async def _index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def _terminal_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    cmd_param = request.query.get("cmd")
    cmd = cmd_param.split() if cmd_param else DEFAULT_CMD

    master_fd, slave_fd = pty.openpty()
    _set_pty_size(master_fd, 24, 80)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    # Ensure common user bin dirs are reachable so tools installed via uv/pipx
    # (e.g. kimi at ~/.local/bin/kimi) resolve without absolute paths.
    extra = [str(Path.home() / ".local" / "bin"), str(Path.home() / "bin")]
    path_parts = env.get("PATH", "").split(":")
    env["PATH"] = ":".join([p for p in extra if p not in path_parts] + path_parts)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=env,
            cwd=str(HALOS_ROOT),
        )
    except FileNotFoundError as e:
        await ws.send_str(f"\r\n\x1b[31mFailed to spawn: {e}\x1b[0m\r\n")
        os.close(master_fd)
        os.close(slave_fd)
        # 4404: spawn failed — client treats as terminal, no reconnect.
        await ws.close(code=4404, message=b"spawn failed")
        return ws

    os.close(slave_fd)
    loop = asyncio.get_running_loop()
    logger.info(f"PTY session started (pid={proc.pid}, cmd={cmd})")

    pty_done = asyncio.Event()

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            pty_done.set()
            return
        if not data:
            pty_done.set()
            return
        asyncio.ensure_future(ws.send_bytes(data))

    loop.add_reader(master_fd, _on_readable)

    async def _wait_proc() -> None:
        await proc.wait()
        pty_done.set()

    wait_task = asyncio.create_task(_wait_proc())

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                payload = msg.data
                resize = RESIZE_RE.match(payload)
                if resize:
                    cols, rows = int(resize.group(1)), int(resize.group(2))
                    _set_pty_size(master_fd, rows, cols)
                    continue
                os.write(master_fd, payload.encode("utf-8", "replace"))
            elif msg.type == WSMsgType.BINARY:
                os.write(master_fd, msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
            if pty_done.is_set():
                break
    finally:
        loop.remove_reader(master_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        wait_task.cancel()
        logger.info(f"PTY session ended (pid={proc.pid})")
        if not ws.closed:
            await ws.close()

    return ws


def _auth_middleware(token: str):
    @web.middleware
    async def mw(request: web.Request, handler):
        if request.path in PUBLIC_PATHS or request.path.startswith("/static/"):
            return await handler(request)
        provided = request.query.get("token") or request.cookies.get(TOKEN_COOKIE)
        if not provided or not secrets.compare_digest(provided, token):
            return web.Response(status=401, text="unauthorized")
        response = await handler(request)
        if request.query.get("token") and isinstance(response, web.StreamResponse):
            response.set_cookie(
                TOKEN_COOKIE, token,
                httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 30,
            )
        return response
    return mw


def make_app(token: str) -> web.Application:
    _ensure_wasm()
    app = web.Application(middlewares=[_auth_middleware(token)])
    app.router.add_get("/", _index)
    app.router.add_get("/api/terminal", _terminal_ws)
    app.router.add_static("/static", str(STATIC_DIR), show_index=False)
    app.router.add_get("/wterm.wasm", lambda _r: web.FileResponse(WASM_PATH))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HalOS browser TUI bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.environ.get("HALOS_WEB_TOKEN") or _load_or_create_token()
    access_url = f"http://{args.host}:{args.port}/?token={token}"
    logger.info(f"Serving browser TUI on http://{args.host}:{args.port}")
    logger.info(f"Access URL (token required): {access_url}")
    web.run_app(make_app(token), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
