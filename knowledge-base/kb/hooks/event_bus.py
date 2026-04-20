"""KB event bus - synchronous pub/sub for lifecycle hooks."""

import json
import logging
import signal
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List

from kb.db import get_connection

logger = logging.getLogger(__name__)

# Supported event types
EVENT_TYPES = ("ingest", "session_start", "session_end", "query", "write")

_HANDLER_TIMEOUT_SECONDS = 5


class _TimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _TimeoutError("Handler timed out")


class KBEventBus:
    """Synchronous event bus with timeout guards and failure isolation."""

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """Register a handler for an event type.

        Args:
            event_type: One of EVENT_TYPES (or any custom string).
            handler: Callable invoked with **kwargs on emit.
        """
        self._handlers[event_type].append(handler)
        logger.debug(f"Subscribed {handler.__name__} to event '{event_type}'")

    def emit(self, event_type: str, **kwargs) -> List[dict]:
        """Dispatch event to all subscribers, log to hook_events table.

        Each handler is called with the provided kwargs.  Failed handlers are
        caught and logged - they never crash the emitter.

        Args:
            event_type: Event name to dispatch.
            **kwargs: Arbitrary payload forwarded to handlers.

        Returns:
            List of result dicts, one per handler:
            {"handler": name, "status": "success"|"error"|"timeout", "error": str|None}
        """
        results: List[dict] = []
        handlers = self._handlers.get(event_type, [])

        if not handlers:
            logger.debug(f"No handlers registered for event '{event_type}'")

        for handler in handlers:
            handler_name = getattr(handler, "__name__", repr(handler))
            status = "success"
            error_msg = None

            try:
                # Use SIGALRM for timeout on Unix; falls back gracefully on Windows
                if hasattr(signal, "SIGALRM"):
                    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                    signal.alarm(_HANDLER_TIMEOUT_SECONDS)
                try:
                    handler(**kwargs)
                finally:
                    if hasattr(signal, "SIGALRM"):
                        signal.alarm(0)
                        signal.signal(signal.SIGALRM, old_handler)

            except _TimeoutError:
                status = "timeout"
                error_msg = f"Handler '{handler_name}' timed out after {_HANDLER_TIMEOUT_SECONDS}s"
                logger.warning(error_msg)
            except Exception as exc:
                status = "error"
                error_msg = str(exc)
                logger.warning(
                    f"Handler '{handler_name}' raised on event '{event_type}': {exc}",
                    exc_info=True,
                )

            results.append({"handler": handler_name, "status": status, "error": error_msg})

        # Log to hook_events table (best-effort)
        try:
            entity_id = kwargs.get("article_id") or kwargs.get("session_id")
            entity_type = _infer_entity_type(event_type)
            overall_status = "success" if all(r["status"] == "success" for r in results) else "partial"
            if not results:
                overall_status = "no_handlers"

            data_payload = {k: v for k, v in kwargs.items() if _is_serialisable(v)}

            conn = get_connection()
            conn.execute(
                """
                INSERT INTO hook_events (event_type, entity_id, entity_type, status, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    entity_id,
                    entity_type,
                    overall_status,
                    json.dumps(data_payload),
                    datetime.utcnow().isoformat(),
                ),
            )
        except Exception as log_exc:
            logger.debug(f"Failed to log hook_event for '{event_type}': {log_exc}")

        return results


def _infer_entity_type(event_type: str) -> str:
    mapping = {
        "ingest": "article",
        "session_start": "session",
        "session_end": "session",
        "query": "query",
        "write": "article",
    }
    return mapping.get(event_type, "unknown")


def _is_serialisable(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


# Module-level singleton
_bus: KBEventBus = None


def get_bus() -> KBEventBus:
    """Return the module-level KBEventBus singleton."""
    global _bus
    if _bus is None:
        _bus = KBEventBus()
        _register_default_handlers(_bus)
    return _bus


def _register_default_handlers(bus: KBEventBus) -> None:
    """Wire up the default hook implementations."""
    try:
        from kb.hooks.ingest_hook import on_article_ingested
        bus.subscribe("ingest", on_article_ingested)
    except Exception as exc:
        logger.debug(f"Could not register ingest hook: {exc}")

    try:
        from kb.hooks.session_hooks import on_session_start, on_session_end
        bus.subscribe("session_start", on_session_start)
        bus.subscribe("session_end", on_session_end)
    except Exception as exc:
        logger.debug(f"Could not register session hooks: {exc}")
