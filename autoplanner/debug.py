"""Diagnostic logging for debugging TUI hangs.

In TUI mode, writing to fd 2 corrupts Textual's terminal rendering.
So all debug output goes to a log file (autoplanner-debug.log in cwd).
In headless mode, it goes to stderr as usual.

Enable with --debug flag or AUTOPLANNER_DEBUG=1 env var.
"""
from __future__ import annotations

import os
import threading
import time

_enabled = bool(os.environ.get("AUTOPLANNER_DEBUG"))
_t0 = time.monotonic()
_log_fd: int | None = None  # file descriptor for the log file
_lock = threading.Lock()

LOG_FILE = "autoplanner-debug.log"


def enable() -> None:
    global _enabled
    _enabled = True


def _get_fd() -> int:
    """Lazily open the log file on first write."""
    global _log_fd
    if _log_fd is None:
        with _lock:
            if _log_fd is None:
                _log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    return _log_fd


def debug(msg: str) -> None:
    if not _enabled:
        return
    elapsed = time.monotonic() - _t0
    thread = threading.current_thread().name
    line = f"[AP-DEBUG {elapsed:8.3f} {thread}] {msg}\n"
    try:
        os.write(_get_fd(), line.encode())
    except OSError:
        pass


# --- Event-loop heartbeat ---
# A daemon thread posts a callback via call_soon_threadsafe every 2s.
# If the callback doesn't fire within the timeout, the event loop is blocked.

_heartbeat_stop = threading.Event()


def _heartbeat_loop(app) -> None:
    """Run in a daemon thread.  Probes whether the asyncio event loop is alive."""
    while not _heartbeat_stop.is_set():
        _heartbeat_stop.wait(2.0)
        if _heartbeat_stop.is_set():
            break
        fired = threading.Event()

        def _ping():
            fired.set()

        try:
            loop = app._loop  # noqa: SLF001
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_ping)
            else:
                debug("HEARTBEAT: event loop not running")
                continue
        except Exception as e:
            debug(f"HEARTBEAT: call_soon_threadsafe failed: {e}")
            continue

        if not fired.wait(timeout=2.0):
            debug("HEARTBEAT: EVENT LOOP BLOCKED")
        else:
            debug("HEARTBEAT: event loop alive")


def heartbeat_start(app) -> None:
    if not _enabled:
        return
    _heartbeat_stop.clear()
    t = threading.Thread(target=_heartbeat_loop, args=(app,), daemon=True)
    t.start()


def heartbeat_stop() -> None:
    _heartbeat_stop.set()
