#!/usr/bin/env python3
"""Interactive demo of the decisions UI.

Usage:
    python demo_decisions.py              # Terminal mode (plain stdin/stdout)
    python demo_decisions.py --tui        # TUI mode (Textual app)

Presents a sequence of mock decisions through the real Writer implementation
so you can test rendering, input validation, and the full flow without
running any LLM calls.
"""
from __future__ import annotations

import json
import sys
import threading
import time

MOCK_DECISIONS = [
    {
        "id": "d1",
        "title": "Cache invalidation strategy",
        "summary": "The document proposes TTL-based expiration but event-driven "
                   "invalidation would reduce staleness at the cost of coupling "
                   "to the event bus.",
        "options": [
            {"key": "A", "label": "TTL-based expiration",
             "description": "Each cache entry gets a fixed time-to-live (e.g. 60s). "
                            "After expiry the next read triggers a cache miss and refetch "
                            "from the source of truth. No coordination with writers needed.",
             "pros": "Simple, decoupled, easy to reason about",
             "cons": "Stale reads up to TTL window"},
            {"key": "B", "label": "Event-driven invalidation",
             "description": "Writers publish invalidation events to an event bus (SNS/SQS). "
                            "Cache nodes subscribe and evict entries on receipt. Requires the "
                            "event bus to guarantee at-least-once delivery for correctness.",
             "pros": "Near-realtime freshness",
             "cons": "Coupling to event bus, more failure modes"},
            {"key": "C", "label": "Hybrid (event + TTL fallback)",
             "description": "Primary invalidation via events, with a short TTL (e.g. 30s) as "
                            "a safety net for missed events. Cache nodes subscribe to events "
                            "but entries also self-expire. Handles event bus outages gracefully.",
             "pros": "Best of both worlds",
             "cons": "Higher implementation complexity"},
        ],
        "current_choice": "A",
    },
    {
        "id": "d2",
        "title": "Session storage backend",
        "summary": "Redis is the obvious choice for session storage, but "
                   "DynamoDB would eliminate a managed dependency at the cost "
                   "of higher tail latency.",
        "options": [
            {"key": "A", "label": "Redis (ElastiCache)",
             "description": "Managed Redis cluster via ElastiCache. Sessions stored as "
                            "hash keys with native TTL for expiry. Supports pub/sub which "
                            "could be reused for cache invalidation. Requires sizing, "
                            "failover config, and monitoring of memory usage.",
             "pros": "Sub-ms latency, native TTL, pub/sub for invalidation",
             "cons": "Another managed service to operate"},
            {"key": "B", "label": "DynamoDB",
             "description": "Sessions stored in a DynamoDB table with a TTL attribute for "
                            "automatic cleanup. Fully serverless — no cluster to manage. "
                            "On-demand capacity mode means zero scaling decisions. However "
                            "p99 read latency is 5-10ms vs sub-ms for Redis.",
             "pros": "Serverless, no cluster management",
             "cons": "Higher p99 latency, no pub/sub"},
        ],
        "current_choice": "A",
    },
    {
        "id": "d1-v2",
        "title": "Cache invalidation strategy (conflict)",
        "summary": "The chosen event-driven approach requires the event bus to "
                   "guarantee ordering, but R7 now specifies at-most-once delivery. "
                   "This is incompatible.",
        "conflict_with": "d1",
        "options": [
            {"key": "A", "label": "Revert to TTL-based expiration",
             "description": "Abandon event-driven invalidation entirely and fall back to "
                            "TTL-only. This is compatible with at-most-once delivery since "
                            "no events are consumed. Staleness window returns.",
             "effect": "supersede",
             "pros": "Works with at-most-once delivery",
             "cons": "Stale reads return"},
            {"key": "B", "label": "Require at-least-once delivery on event bus",
             "description": "Change R7 to mandate at-least-once delivery semantics on the "
                            "event bus. This preserves event-driven invalidation but expands "
                            "the scope of the event bus requirement and may affect other "
                            "consumers that assumed at-most-once.",
             "effect": "supersede",
             "pros": "Preserves cache invalidation choice",
             "cons": "Changes R7 scope, larger blast radius"},
            {"key": "C", "label": "Keep current choice, accept staleness risk",
             "description": "Keep event-driven invalidation with at-most-once delivery. "
                            "Accept that some invalidation events may be lost, causing brief "
                            "staleness windows. Document this as a known trade-off.",
             "effect": "keep_original",
             "pros": "No changes needed",
             "cons": "Known data consistency gap during redelivery"},
        ],
        "current_choice": "A",
    },
]


from autoplanner.orchestrator import _build_resolution, _build_custom_resolution


def run_terminal_demo():
    """Exercise TerminalWriter.present_decision + await_decision_input."""
    from autoplanner.output import TerminalWriter
    from rich.console import Console

    w = TerminalWriter()
    c = Console()
    prior_decisions = []

    c.print("\n[bold]===  Decision UI Demo (Terminal Mode)  ===[/bold]")
    c.print("[dim]This walks through 3 mock decisions. Use /A, /B, /skip, /custom,\n/options, or type a question (e.g. 'Why option A?').[/dim]\n")

    w.bell()
    for i, decision in enumerate(MOCK_DECISIONS):
        c.print(f"\n[bold yellow]--- Decision {i+1}/{len(MOCK_DECISIONS)} ---[/bold yellow]")
        w.present_decision(decision, prior_decisions)

        valid_keys = [opt["key"] for opt in decision["options"]]
        keys_str = " ".join(f"/{k}" for k in valid_keys)
        prompt_text = (
            f"{keys_str}  /skip  /custom  /options  — or ask a question "
            f"[{decision['title']}]"
        )
        while True:
            chosen_key, note = w.await_decision_input(valid_keys + ["skip"], prompt_text)
            if chosen_key == "options":
                w.present_decision(decision, prior_decisions)
                continue
            if chosen_key == "custom":
                if not note:
                    c.print("[dim]  Usage: /custom <your answer>[/dim]")
                    continue
                break
            if chosen_key != "":
                break
            c.print("[dim]  Chat not available in demo mode. Use /A, /B, etc.[/dim]")

        if chosen_key == "custom":
            resolution = _build_custom_resolution(decision, note)
        else:
            if chosen_key == "skip":
                chosen_key = decision["current_choice"]
            resolution = _build_resolution(chosen_key, note, decision)
        c.print(f"\n  [green]Locked:[/green] {resolution['locked_direction']}")

        prior_decisions.append({
            "id": decision["id"],
            "state": "active",
            "title": decision["title"],
            "resolution": resolution,
        })

    c.print("\n[bold green]All decisions resolved![/bold green]")
    c.print("\n[dim]Resolutions:[/dim]")
    for pd in prior_decisions:
        c.print(f"  {pd['id']}: {pd['resolution']['locked_direction']}")


def run_tui_demo():
    """Exercise TuiWriter + AutoplannerApp decision UI in a real Textual app."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import RichLog, Input, Static
    from rich.text import Text

    from autoplanner.tui import OutputLog, TuiWriter
    from autoplanner.output import set_writer

    class DecisionDemoApp(App):
        """Minimal app that provides the callbacks TuiWriter expects."""
        AUTO_FOCUS = "#prompt-input"

        CSS = """
        #log {
            height: 1fr;
            border-bottom: solid $accent;
            scrollbar-size: 1 1;
        }
        #status-bar {
            height: 1;
            background: $surface;
            color: $text-muted;
            padding: 0 1;
        }
        #prompt-input {
            dock: bottom;
        }
        """

        BINDINGS = [Binding("ctrl+c", "quit", "Quit")]

        def __init__(self):
            super().__init__()
            self._writer: TuiWriter | None = None
            self._in_decision_phase = False
            self._decision_event: threading.Event | None = None
            self._decision_result: tuple[str, str] | None = None
            self._decision_valid_keys: list[str] | None = None

        def compose(self) -> ComposeResult:
            yield OutputLog(id="log", wrap=True, highlight=True, markup=True)
            yield Static("Decision UI Demo", id="status-bar")
            yield Input(placeholder="Waiting for decisions...", id="prompt-input")

        def on_mount(self) -> None:
            self._writer = TuiWriter(self)
            set_writer(self._writer)
            self._run_demo()

        # --- TuiWriter callback methods (same as AutoplannerApp) ---

        def _safe_write(self, renderable, **kwargs) -> None:
            log = self.query_one("#log", RichLog)
            try:
                log.write(renderable, **kwargs)
            except Exception:
                plain = str(renderable) if not isinstance(renderable, str) else renderable
                try:
                    log.write(Text(plain), **kwargs)
                except Exception:
                    pass

        def append_text(self, text: str) -> None:
            self._safe_write(Text(text), shrink=False)

        def append_thinking(self, text: str) -> None:
            self._safe_write(Text(text, style="dim"), shrink=False)

        def start_thinking(self, label: str) -> None:
            pass

        def end_thinking(self) -> None:
            pass

        def append_status(self, text: str) -> None:
            self._safe_write(text)

        # --- Input handling ---

        def on_input_submitted(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            if not text:
                return
            event.input.value = ""

            if text.lower() in ("q", "quit", "exit"):
                self.exit()
                return

            if self._in_decision_phase:
                self._handle_decision_input(text)
                return

            log = self.query_one("#log", RichLog)
            log.write(Text(f"[you] {text}", style="bold magenta"))

        def _handle_decision_input(self, text: str) -> None:
            from autoplanner.output import _parse_decision_input
            log = self.query_one("#log", RichLog)
            result = _parse_decision_input(text, self._decision_valid_keys)

            log.write(Text(f"> {text}", style="cyan"))

            if result is not None:
                key = result[0]
                if key == "options":
                    self._decision_result = result
                    self._decision_event.set()
                    return
                self._decision_result = result
                self._in_decision_phase = False
                self.query_one("#prompt-input", Input).placeholder = "Waiting for next decision..."
                self._decision_event.set()
            else:
                log.write(Text(
                    "Chat not available in demo mode. Use /A, /B, etc.",
                    style="dim",
                ))

        # --- Decision rendering (same as AutoplannerApp) ---

        def render_decision(self, decision, prior_decisions):
            log = self.query_one("#log", RichLog)
            log.write(Text(""))
            if prior_decisions:
                log.write(Text("Previously locked:", style="dim"))
                for pd in prior_decisions:
                    if pd.get("resolution"):
                        log.write(Text(
                            f"{pd['id']} — {pd['resolution']['locked_direction']}",
                            style="dim",
                        ))
                log.write(Text(""))

            conflict_note = (
                f" (challenges {decision['conflict_with']})"
                if decision.get("conflict_with") else ""
            )
            log.write(Text(
                f"Decision: {decision['title']}{conflict_note}",
                style="bold cyan",
            ))
            log.write(Text(decision.get("summary", "")))
            log.write(Text(""))

            for i, opt in enumerate(decision.get("options", [])):
                if i > 0:
                    log.write(Text(""))
                current_mark = Text(" \u25c0 current", style="bright_blue") if opt["key"] == decision.get("current_choice") else Text("")
                effect_note = Text(f" [{opt['effect']}]", style="yellow") if opt.get("effect") else Text("")
                header = Text(f"[{opt['key']}] {opt['label']}", style="bold")
                header.append_text(current_mark)
                header.append_text(effect_note)
                log.write(header)
                if opt.get("description"):
                    log.write(Text(opt["description"], style="dim"))
                pros_line = Text()
                pros_line.append("Pros: ", style="green")
                pros_line.append(opt.get("pros", ""), style="dim")
                log.write(pros_line)
                cons_line = Text()
                cons_line.append("Cons: ", style="red")
                cons_line.append(opt.get("cons", ""), style="dim")
                log.write(cons_line)
            log.write(Text(""))

        def begin_decision_input(self, valid_keys, prompt_text, event):
            self._in_decision_phase = True
            self._decision_valid_keys = valid_keys
            self._decision_event = event
            self._decision_result = None
            self.query_one("#prompt-input", Input).placeholder = prompt_text

        # --- Demo worker ---

        @work(thread=True)
        def _run_demo(self) -> None:
            w = self._writer
            prior_decisions = []

            # Small delay so the TUI renders before we start
            time.sleep(0.3)

            w.write_status("[bold]Decision UI Demo[/bold]")
            w.write_status(
                "[dim]This walks through 3 mock decisions. Try valid keys, "
                "'skip', invalid input, and notes.[/dim]"
            )

            w.bell()
            for i, decision in enumerate(MOCK_DECISIONS):
                w.write_status(
                    f"\n[bold yellow]--- Decision {i+1}/{len(MOCK_DECISIONS)} ---[/bold yellow]"
                )

                w.present_decision(decision, prior_decisions)

                valid_keys = [opt["key"] for opt in decision["options"]]
                keys_str = " ".join(f"/{k}" for k in valid_keys)
                prompt_text = (
                    f"{keys_str}  /skip  /custom  /options  — or ask a question "
                    f"[{decision['title']}]"
                )
                while True:
                    chosen_key, note = w.await_decision_input(
                        valid_keys + ["skip"], prompt_text,
                    )
                    if chosen_key == "options":
                        w.present_decision(decision, prior_decisions)
                        continue
                    if chosen_key == "custom":
                        if not note:
                            w.write_status("[dim]  Usage: /custom <your answer>[/dim]")
                            continue
                        break
                    if chosen_key != "":
                        break
                    w.write_status(
                        "[dim]  Chat not available in demo mode. "
                        "Use /A, /B, etc.[/dim]"
                    )

                if chosen_key == "custom":
                    resolution = _build_custom_resolution(decision, note)
                else:
                    if chosen_key == "skip":
                        chosen_key = decision["current_choice"]
                    resolution = _build_resolution(chosen_key, note, decision)
                w.write_status(
                    f"  [green]Decision locked: {decision['id']} — "
                    f"{resolution['locked_direction']}[/green]"
                )

                prior_decisions.append({
                    "id": decision["id"],
                    "state": "active",
                    "title": decision["title"],
                    "resolution": resolution,
                })

            w.write_status("\n[bold green]All decisions resolved![/bold green]")
            w.write_status("\n[dim]Final resolutions:[/dim]")
            for pd in prior_decisions:
                w.write_status(
                    f"  {pd['id']}: {pd['resolution']['locked_direction']}"
                )
            w.write_status("\n[dim]Press Ctrl+C or type 'q' to exit.[/dim]")

    DecisionDemoApp().run()


if __name__ == "__main__":
    if "--tui" in sys.argv:
        run_tui_demo()
    else:
        run_terminal_demo()
