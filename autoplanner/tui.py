from __future__ import annotations

import threading
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import RichLog, Input, Static

from autoplanner import orchestrator
from autoplanner.orchestrator import Reviewer
from autoplanner.output import Writer, set_writer
from autoplanner.steering import QueueSteering


class TuiWriter(Writer):
    """Routes all output to the TUI's RichLog widget."""

    def __init__(self, app: AutoplannerApp) -> None:
        self._app = app

    def write(self, text: str) -> None:
        self._app.call_from_thread(self._app.append_text, text)

    def write_thinking(self, text: str) -> None:
        self._app.call_from_thread(self._app.append_thinking, text)

    def write_status(self, text: str) -> None:
        self._app.call_from_thread(self._app.append_status, text)

    def thinking_start(self, label: str) -> None:
        self._app.call_from_thread(self._app.start_thinking, label)

    def thinking_end(self) -> None:
        self._app.call_from_thread(self._app.end_thinking)


class AutoplannerApp(App):
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

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def __init__(
        self,
        task: str | None = None,
        *,
        max_iterations: int = 5,
        claude_model: str = "opus",
        claude_effort: str = "high",
        codex_model: str = "gpt-4.3",
        codex_effort: str = "xhigh",
        reviewer: Reviewer = Reviewer.AUTO,
    ) -> None:
        super().__init__()
        self._initial_task = task
        self._max_iterations = max_iterations
        self._claude_model = claude_model
        self._claude_effort = claude_effort
        self._codex_model = codex_model
        self._codex_effort = codex_effort
        self._reviewer = reviewer
        self._steering = QueueSteering()
        self._running = False
        self._in_thinking = False

    def compose(self) -> ComposeResult:
        yield RichLog(id="log", wrap=True, highlight=True, markup=True)
        placeholder = "Enter task description..." if not self._initial_task else "Type steering instructions (Enter to send)..."
        yield Static("Ready", id="status-bar")
        yield Input(placeholder=placeholder, id="prompt-input")

    def on_mount(self) -> None:
        set_writer(TuiWriter(self))
        self.query_one("#prompt-input", Input).focus()
        if self._initial_task:
            self._start_run(self._initial_task)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        log = self.query_one("#log", RichLog)

        if not self._running:
            log.write(Text(f"> {text}", style="bold"))
            self._start_run(text)
            event.input.placeholder = "Type steering instructions (Enter to send)..."
        else:
            log.write(Text(f"[you] {text}", style="bold magenta"))
            self._steering.put(text)

    def _start_run(self, task: str) -> None:
        self._running = True
        self._update_status("Running...")
        self._run_orchestrator(task)

    @work(thread=True)
    def _run_orchestrator(self, task: str) -> None:
        try:
            result = orchestrator.run(
                task,
                max_iterations=self._max_iterations,
                claude_model=self._claude_model,
                claude_effort=self._claude_effort,
                codex_model=self._codex_model,
                codex_effort=self._codex_effort,
                reviewer=self._reviewer,
                steering_source=self._steering,
            )
            self.call_from_thread(self._on_complete, result)
        except Exception as e:
            self.call_from_thread(self._on_error, e)

    def _on_complete(self, result_path: Path) -> None:
        self._running = False
        self._update_status(f"Done! {result_path.name}")
        log = self.query_one("#log", RichLog)
        log.write("")
        log.write(Text(f"Complete. Press Ctrl+C to exit.", style="bold green"))
        inp = self.query_one("#prompt-input", Input)
        inp.placeholder = "Done. Press Ctrl+C to exit."

    def _on_error(self, error: Exception) -> None:
        self._running = False
        self._update_status("Error")
        log = self.query_one("#log", RichLog)
        log.write(Text(f"Error: {error}", style="bold red"))

    def _update_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    # --- Writer callbacks (called via call_from_thread) ---

    def append_text(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        if self._in_thinking:
            self._in_thinking = False
            log.write("")  # newline after thinking
        log.write(text, shrink=False)

    def append_thinking(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(Text(text, style="dim"), shrink=False)

    def start_thinking(self, label: str) -> None:
        self._in_thinking = True
        log = self.query_one("#log", RichLog)
        log.write(Text(f"💭 [{label}] thinking...", style="dim"))

    def end_thinking(self) -> None:
        self._in_thinking = False

    def append_status(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(text)
