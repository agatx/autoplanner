"""Pluggable output writer used by sessions and orchestrator.

Two implementations:
- TerminalWriter: plain print() for headless / pipe usage
- TuiWriter: posts to a Textual RichLog widget
"""
from __future__ import annotations

from typing import Protocol


class Writer(Protocol):
    def write(self, text: str) -> None: ...
    def write_thinking(self, text: str) -> None: ...
    def write_status(self, text: str) -> None: ...
    def thinking_start(self, label: str) -> None: ...
    def thinking_end(self) -> None: ...


class TerminalWriter:
    """Writes directly to the terminal with ANSI codes."""

    def __init__(self) -> None:
        self._needs_label = True

    def write(self, text: str) -> None:
        print(text, end="", flush=True)

    def write_thinking(self, text: str) -> None:
        print(text, end="", flush=True)

    def write_status(self, text: str) -> None:
        from rich.console import Console
        Console().print(text)

    def thinking_start(self, label: str) -> None:
        print(f"\n  [{label}] \033[2m💭 ", end="", flush=True)

    def thinking_end(self) -> None:
        print("\033[0m", flush=True)


# Singleton — set by the TUI or left as terminal default
_active_writer: Writer = TerminalWriter()


def get_writer() -> Writer:
    return _active_writer


def set_writer(w: Writer) -> None:
    global _active_writer
    _active_writer = w
