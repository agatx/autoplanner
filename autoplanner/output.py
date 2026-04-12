"""Pluggable output writer used by sessions and orchestrator.

Two implementations:
- TerminalWriter: plain print() for headless / pipe usage
- TuiWriter: posts to a Textual RichLog widget
"""
from __future__ import annotations

import sys
from typing import Protocol


def _parse_decision_input(text: str, valid_keys: list[str]) -> tuple[str, str] | None:
    """Parse slash-command decision input.

    Returns:
        ("A", note)    — choice via /A [-- note]
        ("skip", "")   — skip via /skip
        ("custom", note) — free-form answer via /custom <text>
        ("options", "") — re-display options via /options
        None           — bare text, treat as a question for the author
    """
    if not text.startswith("/"):
        return None

    body = text[1:]  # strip leading /
    parts = body.split(None, 1)
    if not parts:
        return None
    cmd = parts[0].upper()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "SKIP":
        return ("skip", "")
    if cmd == "OPTIONS":
        return ("options", "")
    if cmd == "CUSTOM":
        return ("custom", rest)
    if cmd in valid_keys:
        # Optional note after separator
        for sep in ("\u2014", "--", "-"):
            if rest.startswith(sep):
                return (cmd, rest[len(sep):].strip())
        return (cmd, rest)

    return None


class Writer(Protocol):
    def write(self, text: str) -> None: ...
    def write_thinking(self, text: str) -> None: ...
    def write_status(self, text: str) -> None: ...
    def thinking_start(self, label: str) -> None: ...
    def thinking_end(self) -> None: ...
    def bell(self) -> None: ...
    def present_decision(self, decision: dict, prior_decisions: list[dict]) -> None: ...
    def await_decision_input(self, valid_keys: list[str], prompt_text: str) -> tuple[str, str]: ...


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

    def bell(self) -> None:
        print("\a", end="", flush=True)

    def present_decision(self, decision: dict, prior_decisions: list[dict]) -> None:
        from rich.console import Console
        c = Console()
        c.print()

        # Show previously locked decisions for context
        if prior_decisions:
            c.print("[dim]Previously locked:[/dim]")
            for pd in prior_decisions:
                if pd.get("resolution"):
                    c.print(f"[dim]{pd['id']} — {pd['resolution']['locked_direction']}[/dim]")
            c.print()

        # Decision header
        conflict_note = ""
        if decision.get("conflict_with"):
            conflict_note = f" [yellow](challenges {decision['conflict_with']})[/yellow]"
        c.print(f"[bold cyan]Decision: {decision['title']}[/bold cyan]{conflict_note}")
        c.print(decision.get("summary", ""))
        c.print()

        # Options
        for i, opt in enumerate(decision.get("options", [])):
            if i > 0:
                c.print()
            current = " [bright_blue]◀ current[/bright_blue]" if opt["key"] == decision.get("current_choice") else ""
            effect_note = f" [yellow]\\[{opt['effect']}][/yellow]" if opt.get("effect") else ""
            c.print(f"[bold]\\[{opt['key']}] {opt['label']}[/bold]{current}{effect_note}")
            if opt.get("description"):
                c.print(f"[dim]{opt['description']}[/dim]")
            c.print(f"[green]Pros:[/green] [dim]{opt.get('pros', '')}[/dim]")
            c.print(f"[red]Cons:[/red] [dim]{opt.get('cons', '')}[/dim]")
        c.print()

    def await_decision_input(self, valid_keys: list[str], prompt_text: str) -> tuple[str, str]:
        from rich.console import Console
        c = Console()
        while True:
            c.print(f"\n[bold]{prompt_text}[/bold]: ", end="")
            line = sys.stdin.readline()
            if not line:
                raise EOFError("stdin closed during decision input")
            line = line.strip()
            if not line:
                continue
            result = _parse_decision_input(line, valid_keys)
            if result is not None:
                return result
            # Not a choice — treat as a question
            return ("", line)


# Singleton — set by the TUI or left as terminal default
_active_writer: Writer = TerminalWriter()


def get_writer() -> Writer:
    return _active_writer


def set_writer(w: Writer) -> None:
    global _active_writer
    _active_writer = w
