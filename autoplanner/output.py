"""Pluggable output writer used by sessions and orchestrator.

Two implementations:
- TerminalWriter: plain print() for headless / pipe usage
- TuiWriter: posts to a Textual RichLog widget
"""
from __future__ import annotations

import sys
from typing import Protocol


class Writer(Protocol):
    def write(self, text: str) -> None: ...
    def write_thinking(self, text: str) -> None: ...
    def write_status(self, text: str) -> None: ...
    def thinking_start(self, label: str) -> None: ...
    def thinking_end(self) -> None: ...
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

    def present_decision(self, decision: dict, prior_decisions: list[dict]) -> None:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        c = Console()
        c.print()

        # Show previously locked decisions for context
        if prior_decisions:
            c.print("[dim]Previously locked:[/dim]")
            for pd in prior_decisions:
                if pd.get("resolution"):
                    c.print(f"  [dim]{pd['id']} — {pd['resolution']['locked_direction']}[/dim]")
            c.print()

        # Decision header
        conflict_note = ""
        if decision.get("conflict_with"):
            conflict_note = f" [yellow](challenges {decision['conflict_with']})[/yellow]"
        c.print(f"[bold cyan]Decision: {decision['title']}[/bold cyan]{conflict_note}")
        c.print(f"  {decision.get('summary', '')}")
        c.print()

        # Options table
        table = Table(show_header=True, header_style="bold")
        table.add_column("Key", width=4)
        table.add_column("Option")
        table.add_column("Pros", style="green")
        table.add_column("Cons", style="red")
        if decision.get("conflict_with"):
            table.add_column("Effect", style="yellow")

        for opt in decision.get("options", []):
            current = " ◀" if opt["key"] == decision.get("current_choice") else ""
            row = [
                f"[bold]{opt['key']}[/bold]",
                f"{opt['label']}{current}",
                opt.get("pros", ""),
                opt.get("cons", ""),
            ]
            if decision.get("conflict_with"):
                row.append(opt.get("effect", ""))
            table.add_row(*row)

        c.print(table)
        c.print(f"  [dim]Current choice: {decision.get('current_choice')}[/dim]")

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
            # Parse: first token is key, rest after separator is note
            parts = line.split(None, 1)
            key = parts[0].upper()
            note = ""
            if len(parts) > 1:
                # Strip leading separator if present
                rest = parts[1]
                for sep in ("\u2014", "--", "-"):
                    if rest.startswith(sep):
                        rest = rest[len(sep):].strip()
                        break
                note = rest
            if key.lower() == "skip":
                return ("skip", note)
            if key in valid_keys:
                return (key, note)
            c.print(f"[red]Invalid choice '{key}'. Valid: {', '.join(valid_keys)}[/red]")


# Singleton — set by the TUI or left as terminal default
_active_writer: Writer = TerminalWriter()


def get_writer() -> Writer:
    return _active_writer


def set_writer(w: Writer) -> None:
    global _active_writer
    _active_writer = w
