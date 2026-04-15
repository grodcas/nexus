"""
metrics.py — tiny JSONL timing harness for Plan 1.

Stdlib only. Appends one JSON object per event to
~/.nexus/metrics/handle_tool.jsonl. Events are buffered in memory and
flushed on process exit or on explicit flush() call.

API:
    from metrics import timed, log_event, mark_cold_warm, flush

    with timed("browse.ensure_browser", action="browse"):
        ensure_browser()

    cold = mark_cold_warm("handle_tool.browse")
    log_event(action="browse", total_ms=4820.3, cold=cold, ok=True)

Overhead target: <1ms per event under lock contention-free conditions.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from contextlib import contextmanager

_LOG_PATH = os.path.expanduser("~/.nexus/metrics/handle_tool.jsonl")
_BUFFER: list[dict] = []
_LOCK = threading.Lock()
_SEEN_COLD: set[str] = set()


def _ensure_dir() -> None:
    d = os.path.dirname(_LOG_PATH)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def mark_cold_warm(label: str) -> bool:
    """
    Return True the first time we see `label` in this process, False after.
    Used by handle_tool to tag a call as cold or warm in the logs.
    """
    with _LOCK:
        if label in _SEEN_COLD:
            return False
        _SEEN_COLD.add(label)
        return True


def log_event(**fields) -> None:
    """Append an event dict to the buffer. `ts` is auto-filled."""
    fields.setdefault("ts", round(time.time(), 3))
    with _LOCK:
        _BUFFER.append(fields)


@contextmanager
def timed(phase: str, **extra):
    """
    Context manager that measures wall-clock duration and emits an event
    with phase=..., duration_ms=..., ok=..., plus any extra fields.
    """
    start = time.perf_counter()
    ok = True
    err: str | None = None
    try:
        yield
    except Exception as e:
        ok = False
        err = str(e)[:200]
        raise
    finally:
        dur_ms = (time.perf_counter() - start) * 1000.0
        log_event(
            phase=phase,
            duration_ms=round(dur_ms, 2),
            ok=ok,
            error=err,
            **extra,
        )


def flush() -> None:
    """Write all buffered events to disk and clear the buffer."""
    with _LOCK:
        if not _BUFFER:
            return
        _ensure_dir()
        with open(_LOG_PATH, "a") as f:
            for event in _BUFFER:
                f.write(json.dumps(event) + "\n")
        _BUFFER.clear()


def reset_cold_warm() -> None:
    """Testing helper: wipe the cold/warm tracking set."""
    with _LOCK:
        _SEEN_COLD.clear()


atexit.register(flush)
