"""Playwright-over-CDP driver — connects to a running Chromium and drives the HalOS web TUI tabs.

Terminal reads use `innerText` on the `[aria-label="<Label> terminal"]` div, since
Playwright Python doesn't expose `page.accessibility` (that surface is Node-only).
The wterm widget renders each visible row as real DOM text, so innerText gives us
what we need; we split on newlines and take the last N.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from playwright.sync_api import Page, sync_playwright


@contextmanager
def connect(cdp_url: str, tabs_url: str) -> Iterator[Page]:
    """Connect over CDP and yield the Page pointing at the HalOS TUI.

    Picks the existing page whose URL starts with the recorded tabs_url; if none,
    falls back to the first page in the first context.
    """
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            page = _find_page(browser, tabs_url)
            if page is None:
                raise RuntimeError(
                    f"No page matching {tabs_url!r} in the running browser. "
                    f"Run `halos.conductor start` first."
                )
            yield page
        finally:
            browser.close()


def _find_page(browser, tabs_url: str) -> Optional[Page]:
    host_path = tabs_url.split("?")[0]
    for ctx in browser.contexts:
        for page in ctx.pages:
            if page.url.startswith(host_path):
                return page
    # fallback: first page we can find
    for ctx in browser.contexts:
        for page in ctx.pages:
            return page
    return None


def _terminal_locator(page: Page, tab_label: str):
    return page.get_by_role("textbox", name=f"{tab_label} terminal")


def _tab_button_locator(page: Page, tab_label: str):
    return page.get_by_role("tab", name=tab_label)


def focus_tab(page: Page, tab_label: str) -> None:
    """Click the tab button to activate it; the app also focuses the term on activation."""
    btn = _tab_button_locator(page, tab_label)
    btn.click(timeout=3000)
    # Click inside the terminal area too, ensuring the WTerm surface has focus.
    term = _terminal_locator(page, tab_label)
    try:
        term.click(timeout=1500)
    except Exception:
        pass


def read_terminal(page: Page, tab_label: str, lines: int = 40) -> str:
    """Return the last N visible lines of the named terminal via DOM innerText."""
    focus_tab(page, tab_label)
    selector = f'[aria-label="{tab_label} terminal"]'
    text = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            return el.innerText || el.textContent || "";
        }""",
        selector,
    )
    if text is None:
        return ""
    rows = [row.rstrip() for row in text.split("\n")]
    # Strip trailing empty rows that come from wterm's pre-allocated buffer.
    while rows and not rows[-1]:
        rows.pop()
    tail = rows[-lines:] if lines > 0 else rows
    return "\n".join(tail)


def type_into(page: Page, tab_label: str, text: str, submit: bool = False) -> None:
    focus_tab(page, tab_label)
    if text:
        page.keyboard.type(text, delay=10)
    if submit:
        page.keyboard.press("Enter")


def press_keys(page: Page, tab_label: str, keys: str) -> None:
    focus_tab(page, tab_label)
    page.keyboard.press(keys)


def wait_idle(
    page: Page,
    tab_label: str,
    timeout_s: float = 120.0,
    stable_ms: int = 2000,
    poll_ms: int = 500,
) -> bool:
    """Poll the terminal text until it stops changing for stable_ms. Returns True if idle, False if timed out."""
    start = time.monotonic()
    last_hash = None
    last_change = time.monotonic()
    stable_s = stable_ms / 1000.0

    while time.monotonic() - start < timeout_s:
        text = read_terminal(page, tab_label, lines=80)
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        now = time.monotonic()
        if digest != last_hash:
            last_hash = digest
            last_change = now
        elif now - last_change >= stable_s:
            return True
        time.sleep(poll_ms / 1000.0)

    return False


def full_snapshot(page: Page) -> dict:
    """Return a minimal snapshot: URL, tab labels, per-tab text. Uses DOM, not a11y."""
    return page.evaluate(
        """() => {
            const tabs = Array.from(document.querySelectorAll('[role="tab"]')).map(t => ({
                label: t.textContent.trim(),
                active: t.classList.contains('active'),
            }));
            const terms = {};
            document.querySelectorAll('[aria-label$=" terminal"]').forEach(el => {
                const label = el.getAttribute('aria-label').replace(/ terminal$/, '');
                terms[label] = (el.innerText || el.textContent || '').split('\\n').slice(-80).join('\\n');
            });
            return { url: location.href, tabs, terms };
        }"""
    )
