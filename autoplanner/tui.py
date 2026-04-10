from __future__ import annotations

import threading
from queue import Empty
from pathlib import Path

from rich.control import Control
from rich.text import Text
from textual import work
from textual._compositor import CompositorUpdate
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import RichLog, Input, Static

from autoplanner import orchestrator
from autoplanner.debug import debug, heartbeat_start, heartbeat_stop
from autoplanner.orchestrator import Reviewer
from autoplanner.output import Writer, set_writer
from autoplanner.steering import QueueSteering

class OutputLog(RichLog, can_focus=False):
    """Streaming output should never steal keyboard focus from the prompt."""


class TuiWriter(Writer):
    """Routes all output to the TUI via non-blocking messages."""

    def __init__(self, app: AutoplannerApp) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._text_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._text_len = 0
        self._thinking_len = 0

    def _flush_chunks(self, *, text: str = "", thinking: str = "") -> None:
        if thinking:
            self._app.call_from_thread(self._app.append_thinking, thinking)
        if text:
            self._app.call_from_thread(self._app.append_text, text)

    def _take_pending(self) -> tuple[str, str]:
        with self._lock:
            thinking = "".join(self._thinking_buf)
            text = "".join(self._text_buf)
            self._thinking_buf.clear()
            self._text_buf.clear()
            self._text_len = 0
            self._thinking_len = 0
        return text, thinking

    def flush_pending(self) -> None:
        text, thinking = self._take_pending()
        self._flush_chunks(text=text, thinking=thinking)

    def _buffer_write(self, text: str, buf: list[str], length_attr: str, flush_key: str) -> None:
        chunk = ""
        with self._lock:
            buf.append(text)
            new_len = getattr(self, length_attr) + len(text)
            setattr(self, length_attr, new_len)
            if "\n" in text or new_len >= 512:
                chunk = "".join(buf)
                buf.clear()
                setattr(self, length_attr, 0)
        if chunk:
            self._flush_chunks(**{flush_key: chunk})

    def write(self, text: str) -> None:
        self._buffer_write(text, self._text_buf, "_text_len", "text")

    def write_thinking(self, text: str) -> None:
        self._buffer_write(text, self._thinking_buf, "_thinking_len", "thinking")

    def write_status(self, text: str) -> None:
        self.flush_pending()
        self._app.call_from_thread(self._app.append_status, text)

    def thinking_start(self, label: str) -> None:
        self.flush_pending()
        self._app.call_from_thread(self._app.start_thinking, label)

    def thinking_end(self) -> None:
        self.flush_pending()
        self._app.call_from_thread(self._app.end_thinking)


class AutoplannerApp(App):
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
        codex_model: str = "",
        codex_effort: str = "",
        reviewer: Reviewer = Reviewer.AUTO,
        skip_to_walkthrough: str | None = None,
        ingest: str | None = None,
    ) -> None:
        super().__init__()
        self._initial_task = task
        self._max_iterations = max_iterations
        self._claude_model = claude_model
        self._claude_effort = claude_effort
        self._codex_model = codex_model
        self._codex_effort = codex_effort
        self._reviewer = reviewer
        self._skip_to_walkthrough = skip_to_walkthrough
        self._ingest = ingest
        self._steering = QueueSteering()
        self._running = False
        self._in_thinking = False
        self._writer: TuiWriter | None = None

    def compose(self) -> ComposeResult:
        yield OutputLog(id="log", wrap=True, highlight=True, markup=True)
        placeholder = "Enter task description..." if not self._initial_task else "Type steering instructions (Enter to send)..."
        yield Static("Ready", id="status-bar")
        yield Input(placeholder=placeholder, id="prompt-input")

    def on_mount(self) -> None:
        self._writer = TuiWriter(self)
        set_writer(self._writer)
        heartbeat_start(self)
        self.set_focus(self.query_one("#prompt-input", Input), scroll_visible=False)
        if self._initial_task:
            self._start_run(self._initial_task)

    def action_quit(self) -> None:
        heartbeat_stop()
        from autoplanner.agents.session import _cleanup_all
        _cleanup_all()
        self.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        if text.lower() in ("q", "quit", "exit") and not self._running:
            self.action_quit()
            return

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
        self.query_one("#status-bar", Static).update("Running...")
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
                skip_to_walkthrough=self._skip_to_walkthrough,
                ingest=self._ingest,
            )
            if self._writer is not None:
                self._writer.flush_pending()
            self.call_from_thread(self._handle_run_complete, result)
        except Exception as e:
            debug(f"worker: exception: {e}")
            if self._writer is not None:
                self._writer.flush_pending()
            self.call_from_thread(self._handle_run_failed, str(e))

    def _safe_write(self, renderable, **kwargs) -> None:
        """Write to the log, catching any Rich rendering errors."""
        log = self.query_one("#log", RichLog)
        try:
            log.write(renderable, **kwargs)
        except Exception:
            plain = str(renderable) if not isinstance(renderable, str) else renderable
            try:
                log.write(Text(plain), **kwargs)
            except Exception as e:
                debug(f"_safe_write: rendering failed: {e}")

    def _refresh_ready_state(self) -> None:
        """Force a post-run repaint and restore focus to the prompt.

        TODO: This reaches deep into Textual's private compositor API to work
        around post-run rendering glitches. It will likely break on Textual
        upgrades — revisit when Textual exposes a public full-repaint method.
        """
        log = self.query_one("#log", RichLog)
        status = self.query_one("#status-bar", Static)
        inp = self.query_one("#prompt-input", Input)
        self.set_focus(inp, scroll_visible=False)
        dirty_widgets = {log, status, inp}
        try:
            writer_thread = getattr(getattr(self, "_driver", None), "_writer_thread", None)
            if writer_thread is not None:
                queue = getattr(writer_thread, "_queue", None)
                if queue is not None:
                    # The completion repaint is a full-screen snapshot of the
                    # current compositor state, so any queued partial writes are
                    # stale by definition and can be dropped before we flush it.
                    while True:
                        try:
                            queue.get_nowait()
                        except Empty:
                            break
            self.screen._dirty_widgets.update(dirty_widgets)  # noqa: SLF001
            self.screen._compositor.update_widgets(dirty_widgets)  # noqa: SLF001
            update = self.screen._compositor.render_update(  # noqa: SLF001
                full=True,
                screen_stack=self._background_screens,  # noqa: SLF001
            )
            file = getattr(writer_thread, "_file", None) if writer_thread is not None else None
            if file is not None and isinstance(update, CompositorUpdate):
                cursor_position = self.screen.outer_size.clamp_offset(self.cursor_position)
                terminal_sequence = update.render_segments(self.console)
                terminal_sequence += Control.move_to(*cursor_position).segment.text
                self._begin_update()  # noqa: SLF001
                try:
                    file.write(terminal_sequence)
                finally:
                    self._end_update()  # noqa: SLF001
                file.flush()
            else:
                self._display(self.screen, update)  # noqa: SLF001
        except Exception as e:
            debug(f"_refresh_ready_state: compositor refresh failed: {e}")

    def _finish_run(self, status_text: str, message: Text, *, bell: bool = False) -> None:
        self.query_one("#status-bar", Static).update(status_text)
        self._safe_write("")
        self._safe_write(message)
        self.query_one("#prompt-input", Input).placeholder = status_text
        self._refresh_ready_state()
        if bell:
            self.bell()

    def append_text(self, text: str) -> None:
        self._in_thinking = False
        self._safe_write(Text(text), shrink=False)

    def append_thinking(self, text: str) -> None:
        self._safe_write(Text(text, style="dim"), shrink=False)

    def start_thinking(self, label: str) -> None:
        self._in_thinking = True
        self._safe_write(Text(f"💭 [{label}] thinking...", style="dim"))

    def end_thinking(self) -> None:
        self._in_thinking = False

    def append_status(self, text: str) -> None:
        self._safe_write(text)

    def _handle_run_complete(self, result_path: Path) -> None:
        self._handle_run_end(
            "Done — press Ctrl+C to exit",
            Text(f"✓ Complete: {result_path.name}", style="bold green"),
            bell=True,
        )

    def _handle_run_failed(self, error: str) -> None:
        self._handle_run_end(
            "Error — press Ctrl+C to exit",
            Text(f"✗ Error: {error}", style="bold red"),
        )

    def _handle_run_end(self, status_text: str, message: Text, *, bell: bool = False) -> None:
        try:
            self._running = False
            self._finish_run(status_text, message, bell=bell)
        except Exception as e:
            debug(f"_handle_run_end: exception: {e}")
            self._running = False
