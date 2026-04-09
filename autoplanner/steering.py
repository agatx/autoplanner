from __future__ import annotations

import sys
import threading
from queue import Queue, Empty
from typing import Protocol


class SteeringSource(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def drain(self) -> str | None: ...
    def put(self, message: str) -> None: ...


class StdinSteering:
    """Reads steering input from stdin in a background thread."""

    def __init__(self) -> None:
        self._queue: Queue[str] = Queue()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def put(self, message: str) -> None:
        self._queue.put(message)

    def _reader(self) -> None:
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self._queue.put(line)
            except EOFError:
                break

    def drain(self) -> str | None:
        messages: list[str] = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except Empty:
                break
        if not messages:
            return None
        return "\n".join(messages)


class QueueSteering:
    """Steering input from an external source (e.g. TUI input widget)."""

    def __init__(self) -> None:
        self._queue: Queue[str] = Queue()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def put(self, message: str) -> None:
        self._queue.put(message)

    def drain(self) -> str | None:
        messages: list[str] = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except Empty:
                break
        if not messages:
            return None
        return "\n".join(messages)
