from __future__ import annotations

import fcntl
import json
import os
import select
import sys
import threading
import time
from dataclasses import dataclass, field
import subprocess


MAX_RETRIES = 3
RETRY_BACKOFF = [15, 30, 60]

_print_lock = threading.Lock()


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
                print(f"  [{label}] Transient error, retrying in {wait}s "
                      f"(attempt {attempt + 2}/{MAX_RETRIES + 1})...", flush=True)
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
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _session_id: str | None = field(default=None, repr=False)
    _fd: int | None = field(default=None, repr=False)
    _buf: str = field(default="", repr=False)
    _needs_label: bool = field(default=True, repr=False)

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None:
            self.close()
        self._proc = subprocess.Popen(
            [
                "claude", "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--model", self.model,
                "--effort", self.effort,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
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

    def _read_response(self, timeout: int) -> str:
        assert self._fd is not None
        text_parts: list[str] = []
        current_block: str | None = None
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            ready, _, _ = select.select([self._fd], [], [], 0.1)
            if not ready:
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError(f"{self.label} process exited unexpectedly")
                continue

            try:
                data = os.read(self._fd, 65536).decode("utf-8", errors="replace")
            except BlockingIOError:
                continue

            self._buf += data

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
                            with _print_lock:
                                print(f"\n  [{self.label}] \033[2m💭 ", end="", flush=True)

                    elif et == "content_block_delta":
                        delta = event.get("delta", {})
                        dt = delta.get("type", "")
                        if dt == "thinking_delta":
                            with _print_lock:
                                print(delta.get("thinking", ""), end="", flush=True)
                        elif dt == "text_delta":
                            text = delta.get("text", "")
                            text_parts.append(text)
                            if current_block != "text":
                                with _print_lock:
                                    print("\033[0m", flush=True)
                                current_block = "text"
                            self._print_text_inline(text)

                    elif et == "content_block_stop":
                        if current_block == "thinking":
                            with _print_lock:
                                print("\033[0m", flush=True)
                        current_block = None

                elif evt_type == "result":
                    if obj.get("is_error"):
                        raise RuntimeError(
                            f"{self.label} error: {obj.get('result', 'unknown error')}"
                        )
                    return "".join(text_parts).strip() or obj.get("result", "").strip()

        raise RuntimeError(f"{self.label} timed out after {timeout}s")

    def _print_text_inline(self, text: str) -> None:
        with _print_lock:
            for ch in text:
                if self._needs_label:
                    print(f"  [{self.label}] ", end="", flush=True)
                    self._needs_label = False
                print(ch, end="", flush=True)
                if ch == "\n":
                    self._needs_label = True

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.kill()
            self._proc.wait()
            self._proc = None
            self._session_id = None
            self._fd = None
            self._buf = ""


@dataclass
class CodexSession:
    model: str = "gpt-4.3"
    effort: str = "xhigh"
    label: str = "codex"
    _session_id: str | None = field(default=None, repr=False)

    def send(self, content: str, *, timeout: int = 600) -> str:
        return _send_with_retry(
            lambda timeout: self._send_once(content, timeout=timeout),
            self.label,
            timeout=timeout,
        )

    def _send_once(self, content: str, *, timeout: int) -> str:
        if self._session_id:
            cmd = [
                "codex", "exec", "resume", "--json",
                "-c", f"model={self.model}",
                "-c", f"model_reasoning_effort={self.effort}",
                self._session_id, content,
            ]
        else:
            cmd = [
                "codex", "exec", "--json",
                "-m", self.model,
                "-c", f"model_reasoning_effort={self.effort}",
                content,
            ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        text_parts: list[str] = []
        fd = proc.stdout.fileno()
        start = time.monotonic()

        try:
            while time.monotonic() - start < timeout:
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
                        with _print_lock:
                            for ln in text.splitlines(keepends=True):
                                print(f"  [{self.label}] {ln}", end="", flush=True)
                            if not text.endswith("\n"):
                                print(flush=True)
                    elif item_type == "tool_call":
                        tool = item.get("name", item.get("call_id", "tool"))
                        with _print_lock:
                            print(f"  [{self.label}] \033[33m🔧 {tool}\033[0m", flush=True)

                elif evt_type == "turn.started":
                    with _print_lock:
                        print(f"  [{self.label}] thinking...", flush=True)
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            proc.wait()

        if proc.returncode != 0:
            if stderr:
                print(f"  [{self.label}] stderr: {stderr}", file=sys.stderr)
            raise RuntimeError(f"{self.label} exited with code {proc.returncode}: {stderr[:200]}")

        return "\n".join(text_parts).strip()

    def close(self) -> None:
        self._session_id = None
