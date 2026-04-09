from __future__ import annotations

import json
import select
import subprocess
import sys
import time
from enum import Enum, auto

# Kill process if no output for this long (seconds)
IDLE_TIMEOUT = 120
# Kill process if total runtime exceeds this (seconds)
MAX_TIMEOUT = 600


class StreamMode(Enum):
    CLAUDE = auto()
    CODEX = auto()


class AgentTimeout(RuntimeError):
    pass


def stream_command(
    cmd: list[str],
    *,
    label: str,
    mode: StreamMode,
    idle_timeout: int = IDLE_TIMEOUT,
    max_timeout: int = MAX_TIMEOUT,
) -> str:
    """Run a subprocess with JSON streaming, displaying thinking and output in real time."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None

    try:
        if mode == StreamMode.CLAUDE:
            result = _stream_claude(proc, label, idle_timeout, max_timeout)
        else:
            result = _stream_codex(proc, label, idle_timeout, max_timeout)
    except AgentTimeout:
        proc.kill()
        proc.wait()
        raise

    stderr = proc.stderr.read() if proc.stderr else ""
    proc.wait()

    if proc.returncode != 0:
        if stderr:
            print(f"  [{label}] stderr: {stderr}", file=sys.stderr)
        raise RuntimeError(f"{label} exited with code {proc.returncode}")

    return result


def _read_lines(proc: subprocess.Popen, idle_timeout: int, max_timeout: int):
    """Yield lines from proc.stdout with idle and total timeout enforcement."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    start = time.monotonic()
    buf = ""

    while True:
        elapsed = time.monotonic() - start
        if elapsed > max_timeout:
            raise AgentTimeout(f"Process exceeded max timeout ({max_timeout}s)")

        ready, _, _ = select.select([fd], [], [], min(idle_timeout, 1.0))

        if not ready:
            if time.monotonic() - start > max_timeout:
                raise AgentTimeout(f"Process exceeded max timeout ({max_timeout}s)")
            # Check if process has exited
            if proc.poll() is not None:
                # Drain remaining
                rest = proc.stdout.read()
                if rest:
                    buf += rest
                    for line in buf.splitlines(keepends=True):
                        yield line.rstrip("\n")
                break
            # Check idle
            continue

        chunk = proc.stdout.read(4096)
        if not chunk:
            if buf:
                yield buf
            break

        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line


def _stream_claude(proc: subprocess.Popen, label: str, idle_timeout: int, max_timeout: int) -> str:
    """Parse Claude stream-json events, showing thinking and text deltas live."""
    current_block: str | None = None
    text_parts: list[str] = []

    for line in _read_lines(proc, idle_timeout, max_timeout):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        evt_type = obj.get("type", "")

        if evt_type == "stream_event":
            event = obj.get("event", {})
            _handle_claude_stream_event(event, label, current_block_ref := [current_block], text_parts)
            current_block = current_block_ref[0]

        elif evt_type == "assistant":
            msg = obj.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    text_parts.append(text)
                    _print_text(text, label)

    return "".join(text_parts).strip()


def _handle_claude_stream_event(
    event: dict,
    label: str,
    current_block_ref: list[str | None],
    text_parts: list[str],
) -> None:
    et = event.get("type", "")

    if et == "content_block_start":
        block_type = event.get("content_block", {}).get("type", "")
        current_block_ref[0] = block_type
        if block_type == "thinking":
            print(f"\n  [{label}] \033[2m💭 ", end="", flush=True)

    elif et == "content_block_delta":
        delta = event.get("delta", {})
        dt = delta.get("type", "")
        if dt == "thinking_delta":
            thinking = delta.get("thinking", "")
            print(thinking, end="", flush=True)
        elif dt == "text_delta":
            text = delta.get("text", "")
            text_parts.append(text)
            if current_block_ref[0] != "text":
                print("\033[0m", flush=True)
                current_block_ref[0] = "text"
            _print_text_inline(text, label)

    elif et == "content_block_stop":
        if current_block_ref[0] == "thinking":
            print("\033[0m", flush=True)
        current_block_ref[0] = None


def _stream_codex(proc: subprocess.Popen, label: str, idle_timeout: int, max_timeout: int) -> str:
    """Parse Codex JSON events, showing items as they complete."""
    text_parts: list[str] = []

    for line in _read_lines(proc, idle_timeout, max_timeout):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        evt_type = obj.get("type", "")

        if evt_type == "item.completed":
            item = obj.get("item", {})
            item_type = item.get("type", "")
            text = item.get("text", "")

            if item_type == "agent_message" and text:
                text_parts.append(text)
                _print_text(text, label)
            elif item_type == "tool_call":
                tool = item.get("name", item.get("call_id", "tool"))
                print(f"  [{label}] \033[33m🔧 {tool}\033[0m", flush=True)
            elif item_type == "tool_output":
                output = text[:200]
                if output:
                    print(f"  [{label}] \033[2m   → {output}\033[0m", flush=True)

        elif evt_type == "turn.started":
            print(f"  [{label}] thinking...", flush=True)

    return "\n".join(text_parts).strip()


# --- Helpers ---

_needs_label = True


def _print_text(text: str, label: str) -> None:
    for line in text.splitlines(keepends=True):
        print(f"  [{label}] {line}", end="", flush=True)
    if not text.endswith("\n"):
        print(flush=True)


def _print_text_inline(text: str, label: str) -> None:
    """Print streaming text, adding label prefix at the start of each new line."""
    global _needs_label
    for ch in text:
        if _needs_label:
            print(f"  [{label}] ", end="", flush=True)
            _needs_label = False
        print(ch, end="", flush=True)
        if ch == "\n":
            _needs_label = True
