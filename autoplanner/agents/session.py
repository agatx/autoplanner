from __future__ import annotations

import atexit
import fcntl
import functools
import json
import os
import select
import signal
import time
from dataclasses import dataclass, field
import subprocess

from autoplanner.output import get_writer


# Track spawned process groups so atexit can kill stragglers
_active_pgroups: set[int] = set()


def _kill_pgroup(pid: int) -> None:
    """Send SIGKILL to a process group, ignoring errors if already gone."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _cleanup_all() -> None:
    for pid in list(_active_pgroups):
        _kill_pgroup(pid)


atexit.register(_cleanup_all)


MAX_RETRIES = 3
RETRY_BACKOFF = [15, 30, 60]


def _is_transient(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in [
        "overloaded", "529", "503", "too many requests",
        "capacity", "temporarily unavailable",
    ])


def _send_with_retry(
    send_fn,
    label: str,
    *,
    on_retry=None,
    timeout: int = 600,
) -> str:
    for attempt in range(MAX_RETRIES + 1):
        try:
            return send_fn(timeout=timeout)
        except RuntimeError as e:
            if attempt < MAX_RETRIES and _is_transient(str(e)):
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                w = get_writer()
                w.write_status(
                    f"  [{label}] Transient error, retrying in {wait}s "
                    f"(attempt {attempt + 2}/{MAX_RETRIES + 1})..."
                )
                time.sleep(wait)
                if on_retry:
                    on_retry()
                continue
            raise
    raise RuntimeError(f"{label} failed after {MAX_RETRIES + 1} attempts")


@dataclass
class ClaudeSession:
    model: str = "opus"
    effort: str = "high"
    label: str = "claude"
    skip_permissions: bool = False
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _session_id: str | None = field(default=None, repr=False)
    _fd: int | None = field(default=None, repr=False)
    _buf: str = field(default="", repr=False)

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None:
            self.close()
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self.model,
            "--effort", self.effort,
        ]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        _active_pgroups.add(self._proc.pid)
        assert self._proc.stdout is not None
        self._fd = self._proc.stdout.fileno()
        flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._buf = ""
        self._session_id = None

    def send(self, content: str, *, timeout: int = 600) -> str:
        return _send_with_retry(
            lambda timeout: self._send_once(content, timeout=timeout),
            self.label,
            on_retry=self.close,
            timeout=timeout,
        )

    def _send_once(self, content: str, *, timeout: int) -> str:
        self._ensure_started()
        assert self._proc is not None and self._proc.stdin is not None

        msg: dict = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }
        if self._session_id:
            msg["session_id"] = self._session_id

        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

        return self._read_response(timeout)

    def _try_flush_buffer(self, text_parts: list[str]) -> str | None:
        """If the buffer holds a complete JSON result without trailing newline, return it."""
        remaining = self._buf.strip()
        if not remaining:
            return None
        try:
            obj = json.loads(remaining)
        except json.JSONDecodeError:
            return None
        self._buf = ""
        if obj.get("type") == "result":
            if obj.get("is_error"):
                raise RuntimeError(
                    f"{self.label} error: {obj.get('result', 'unknown error')}"
                )
            return "".join(text_parts).strip() or obj.get("result", "").strip()
        return None

    def _read_response(self, timeout: int) -> str:
        assert self._fd is not None
        w = get_writer()
        text_parts: list[str] = []
        current_block: str | None = None
        last_activity = time.monotonic()
        buf_changed = False

        while time.monotonic() - last_activity < timeout:
            ready, _, _ = select.select([self._fd], [], [], 0.1)
            if not ready:
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError(f"{self.label} process exited unexpectedly")
                if buf_changed:
                    result = self._try_flush_buffer(text_parts)
                    if result is not None:
                        return result
                    buf_changed = False
                continue

            try:
                data = os.read(self._fd, 65536).decode("utf-8", errors="replace")
            except BlockingIOError:
                continue

            if not data:
                result = self._try_flush_buffer(text_parts)
                if result is not None:
                    return result
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError(f"{self.label} process exited unexpectedly")
                continue

            last_activity = time.monotonic()
            self._buf += data
            buf_changed = True

            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                evt_type = obj.get("type", "")

                if evt_type == "system":
                    if not self._session_id:
                        self._session_id = obj.get("session_id")

                elif evt_type == "stream_event":
                    event = obj.get("event", {})
                    et = event.get("type", "")

                    if et == "content_block_start":
                        block_type = event.get("content_block", {}).get("type", "")
                        current_block = block_type
                        if block_type == "thinking":
                            w.thinking_start(self.label)

                    elif et == "content_block_delta":
                        delta = event.get("delta", {})
                        dt = delta.get("type", "")
                        if dt == "thinking_delta":
                            w.write_thinking(delta.get("thinking", ""))
                        elif dt == "text_delta":
                            text = delta.get("text", "")
                            text_parts.append(text)
                            if current_block != "text":
                                w.thinking_end()
                                current_block = "text"
                            w.write(text)

                    elif et == "content_block_stop":
                        if current_block == "thinking":
                            w.thinking_end()
                        current_block = None

                elif evt_type == "result":
                    if obj.get("is_error"):
                        raise RuntimeError(
                            f"{self.label} error: {obj.get('result', 'unknown error')}"
                        )
                    return "".join(text_parts).strip() or obj.get("result", "").strip()

        raise RuntimeError(f"{self.label} idle for {timeout}s (no output received)")

    def close(self) -> None:
        from autoplanner.debug import debug
        if self._proc is not None:
            pid = self._proc.pid
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            _kill_pgroup(pid)
            try:
                self._proc.wait(timeout=5)
            except Exception as e:
                debug(f"{self.label}: wait({pid}) failed: {e}")
            _active_pgroups.discard(pid)
            self._proc = None
            self._session_id = None
            self._fd = None
            self._buf = ""


@functools.lru_cache(maxsize=1)
def _read_codex_config() -> tuple[str, str]:
    """Read model and effort from ~/.codex/config.toml if available."""
    try:
        import tomllib
        from pathlib import Path
        cfg_path = Path.home() / ".codex" / "config.toml"
        if cfg_path.exists():
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            return cfg.get("model", ""), cfg.get("model_reasoning_effort", "")
    except Exception:
        pass
    return "", ""


@dataclass
class CodexSession:
    model: str = ""
    effort: str = ""
    label: str = "codex"
    full_auto: bool = False
    _session_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.model or not self.effort:
            cfg_model, cfg_effort = _read_codex_config()
            if not self.model:
                self.model = cfg_model
            if not self.effort:
                self.effort = cfg_effort

    def send(self, content: str, *, timeout: int = 600) -> str:
        return _send_with_retry(
            lambda timeout: self._send_once(content, timeout=timeout),
            self.label,
            timeout=timeout,
        )

    def _send_once(self, content: str, *, timeout: int) -> str:
        w = get_writer()

        if self._session_id:
            cmd = ["codex", "exec", "resume", "--json"]
        else:
            cmd = ["codex", "exec", "--json"]

        if self.model:
            cmd += ["-c", f"model={self.model}"]
        if self.effort:
            cmd += ["-c", f"model_reasoning_effort={self.effort}"]

        if self.full_auto:
            cmd.append("--full-auto")

        if self._session_id:
            cmd += [self._session_id, "-"]
        else:
            cmd += ["-"]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        _active_pgroups.add(proc.pid)
        assert proc.stdin is not None
        proc.stdin.write(content)
        proc.stdin.close()
        assert proc.stdout is not None

        text_parts: list[str] = []
        fd = proc.stdout.fileno()
        last_activity = time.monotonic()
        timed_out = False

        try:
            while True:
                if time.monotonic() - last_activity >= timeout:
                    timed_out = True
                    break
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                line = proc.stdout.readline().strip()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                last_activity = time.monotonic()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                evt_type = obj.get("type", "")

                if evt_type == "thread.started":
                    tid = obj.get("thread_id")
                    if tid:
                        self._session_id = tid

                elif evt_type == "item.completed":
                    item = obj.get("item", {})
                    item_type = item.get("type", "")
                    text = item.get("text", "")
                    if item_type == "agent_message" and text:
                        text_parts.append(text)
                        w.write(text)
                    elif item_type == "tool_call":
                        tool = item.get("name", item.get("call_id", "tool"))
                        w.write_status(f"  [{self.label}] 🔧 {tool}")

                elif evt_type == "turn.started":
                    w.write_status(f"  [{self.label}] thinking...")
        finally:
            if timed_out and proc.poll() is None:
                _kill_pgroup(proc.pid)
            stderr = proc.stderr.read() if proc.stderr else ""
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_pgroup(proc.pid)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            _active_pgroups.discard(proc.pid)

        if timed_out:
            raise RuntimeError(f"{self.label} idle for {timeout}s (no output received)")

        if proc.returncode != 0:
            if stderr:
                w.write_status(f"  [{self.label}] stderr: {stderr[:200]}")
            raise RuntimeError(f"{self.label} exited with code {proc.returncode}: {stderr[:200]}")

        return "\n".join(text_parts).strip()

    def close(self) -> None:
        self._session_id = None
