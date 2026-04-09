from __future__ import annotations

import sys
import threading
from queue import Queue, Empty


class SteeringInput:
    """Background stdin reader that collects user input without blocking the main loop.

    Type anything while agents are working — it gets picked up at the next
    transition point and fed as additional context to the next agent call.
    """

    def __init__(self) -> None:
        self._queue: Queue[str] = Queue()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _reader(self) -> None:
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self._queue.put(line)
                    print(f"  [you] {line}  (queued for next phase)", flush=True)
            except EOFError:
                break

    def drain(self) -> str | None:
        """Drain all queued messages, return combined text or None."""
        messages: list[str] = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except Empty:
                break
        if not messages:
            return None
        return "\n".join(messages)
