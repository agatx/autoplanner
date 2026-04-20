from __future__ import annotations

import threading
import traceback
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
from autoplanner.output import _parse_decision_input
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

    def bell(self) -> None:
        self._app.call_from_thread(self._app.bell)

    def present_decision(self, decision: dict, prior_decisions: list[dict]) -> None:
        self.flush_pending()
        self._app.call_from_thread(self._app.render_decision, decision, prior_decisions)

    def await_decision_input(self, valid_keys: list[str], prompt_text: str) -> tuple[str, str]:
        event = threading.Event()
        self._app.call_from_thread(
            self._app.begin_decision_input, valid_keys, prompt_text, event,
        )
        event.wait()
        assert self._app._decision_result is not None
        return self._app._decision_result


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
        continue_run: str | None = None,
        human_review: bool = False,
        on_decision_policy: str = "prompt",
        on_parse_error_policy: str = "warn",
        skip_permissions: bool = False,
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
        self._continue_run = continue_run
        self._human_review = human_review
        self._on_decision_policy = on_decision_policy
        self._on_parse_error_policy = on_parse_error_policy
        self._skip_permissions = skip_permissions
        self._steering = QueueSteering()
        self._orchestrator_running = False
        self._worker = None
        self._last_run_id: str | None = None
        self._in_thinking = False
        self._streaming_buf = ""
        self._writer: TuiWriter | None = None
        self._in_decision_phase = False
        self._decision_event: threading.Event | None = None
        self._decision_result: tuple[str, str] | None = None
        self._decision_valid_keys: list[str] | None = None

    def _worker_alive(self) -> bool:
        if self._worker is None:
            return False
        state = getattr(self._worker, "state", None)
        name = getattr(state, "name", str(state))
        return name in ("PENDING", "RUNNING")

    def compose(self) -> ComposeResult:
        yield OutputLog(id="log", wrap=True, highlight=True, markup=True)
        placeholder = "Enter task description..." if not self._initial_task else "Type steering instructions (Enter to send)..."
        yield Static("Ready", id="status-bar")
        yield Input(placeholder=placeholder, id="prompt-input")

    def on_mount(self) -> None:
        debug(f"on_mount: running={self._orchestrator_running} task={self._initial_task!r} ingest={self._ingest!r} continue={self._continue_run!r}")
        self._writer = TuiWriter(self)
        set_writer(self._writer)
        heartbeat_start(self)
        self.set_focus(self.query_one("#prompt-input", Input), scroll_visible=False)
        if self._ingest and not self._initial_task and self._continue_run is None:
            log = self.query_one("#log", RichLog)
            log.write(Text(f"Ingested document: {self._ingest}", style="bold green"))
            log.write(Text(
                "Enter a task description below to start refining it "
                "(e.g. \"Improve this plan\").",
                style="dim",
            ))
        if self._continue_run is not None:
            self._start_resume(self._continue_run)
        elif self._initial_task:
            self._start_run(self._initial_task)

    async def action_quit(self) -> None:
        heartbeat_stop()
        from autoplanner.agents.session import _cleanup_all
        _cleanup_all()
        self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        worker_state = getattr(getattr(self._worker, "state", None), "name", None)
        debug(
            f"on_input_submitted: text={text!r} running={self._orchestrator_running} "
            f"decision_phase={self._in_decision_phase} worker_state={worker_state}"
        )
        if not text:
            return
        event.input.value = ""

        if text.lower() in ("q", "quit", "exit") and not self._orchestrator_running:
            await self.action_quit()
            return

        log = self.query_one("#log", RichLog)

        if self._in_decision_phase:
            self._handle_decision_input(text, log)
            return

        if self._orchestrator_running and not self._worker_alive():
            debug(f"on_input_submitted: stale _running=True, worker_state={worker_state} — resetting")
            log.write(Text(
                "[warning] Previous run state was stale — starting fresh.",
                style="yellow",
            ))
            self._orchestrator_running = False

        if not self._orchestrator_running:
            self._start_run(text)
            event.input.placeholder = "Type steering instructions (Enter to send)..."
        else:
            log.write(Text(f"> {text}", style="cyan"))
            self._steering.put(text)

    def _handle_decision_input(self, text: str, log: RichLog) -> None:
        """Validate and accept decision input, or treat as a question."""
        assert self._decision_valid_keys is not None
        assert self._decision_event is not None
        result = _parse_decision_input(text, self._decision_valid_keys)

        log.write(Text(f"> {text}", style="cyan"))

        if result is not None:
            key = result[0]
            if key == "options":
                # Re-display handled by orchestrator
                self._decision_result = result
                self._decision_event.set()
                return
            # Valid choice (including custom) — resolve and exit decision phase
            self._decision_result = result
            self._in_decision_phase = False
            self.query_one("#prompt-input", Input).placeholder = "Type steering instructions (Enter to send)..."
            self._decision_event.set()
        else:
            # Question — send to worker thread for discussion
            self._decision_result = ("", text)
            inp = self.query_one("#prompt-input", Input)
            inp.disabled = True
            self._decision_event.set()

    def render_decision(self, decision: dict, prior_decisions: list[dict]) -> None:
        """Render a decision block in the output log."""
        self._flush_streaming_buf()
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

        conflict_note = f" (challenges {decision['conflict_with']})" if decision.get("conflict_with") else ""
        log.write(Text(f"Decision: {decision['title']}{conflict_note}", style="bold cyan"))
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

    def begin_decision_input(
        self, valid_keys: list[str], prompt_text: str, event: threading.Event,
    ) -> None:
        """Set up the app to receive decision input (called on main thread)."""
        self._flush_streaming_buf()
        self._in_decision_phase = True
        self._decision_valid_keys = valid_keys
        self._decision_event = event
        self._decision_result = None
        inp = self.query_one("#prompt-input", Input)
        inp.disabled = False
        inp.placeholder = prompt_text

    def _start_run(self, task: str) -> None:
        debug(f"_start_run: task={task!r} ingest={self._ingest!r}")
        self._orchestrator_running = True
        log = self.query_one("#log", RichLog)
        log.write(Text(f"> {task}", style="bold"))
        self.query_one("#status-bar", Static).update("Running...")
        self._worker = self._run_worker(
            orchestrator.run, task,
            skip_to_walkthrough=self._skip_to_walkthrough,
            ingest=self._ingest,
        )

    def _start_resume(self, run_id: str) -> None:
        self._orchestrator_running = True
        log = self.query_one("#log", RichLog)
        log.write(Text(f"> Resuming run: {run_id}", style="bold"))
        self.query_one("#status-bar", Static).update("Resuming...")
        self.query_one("#prompt-input", Input).placeholder = "Type steering instructions (Enter to send)..."
        self._worker = self._run_worker(orchestrator.resume, run_id)

    @work(thread=True)
    def _run_worker(self, fn, first_arg, **extra_kwargs) -> None:
        debug(f"_run_worker: fn={fn.__name__} first_arg={first_arg!r} extra={list(extra_kwargs.keys())}")
        try:
            result = fn(
                first_arg,
                max_iterations=self._max_iterations,
                claude_model=self._claude_model,
                claude_effort=self._claude_effort,
                codex_model=self._codex_model,
                codex_effort=self._codex_effort,
                reviewer=self._reviewer,
                steering_source=self._steering,
                human_review=self._human_review,
                on_decision_policy=self._on_decision_policy,
                on_parse_error_policy=self._on_parse_error_policy,
                skip_permissions=self._skip_permissions,
                **extra_kwargs,
            )
            if self._writer is not None:
                self._writer.flush_pending()
            self.call_from_thread(self._handle_run_complete, result)
        except Exception as e:
            tb = traceback.format_exception(e)
            debug(f"worker: exception:\n{''.join(tb)}")
            if self._writer is not None:
                self._writer.flush_pending()
            self.call_from_thread(self._handle_run_failed, str(e), "".join(tb))

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
        self._streaming_buf += text
        # Write complete lines; keep partial trailing line in buffer
        while "\n" in self._streaming_buf:
            line, self._streaming_buf = self._streaming_buf.split("\n", 1)
            if line:
                self._safe_write(Text(line), shrink=False)
        # Flush if buffer grows large (no newline but lots of text)
        if len(self._streaming_buf) >= 2048:
            self._safe_write(Text(self._streaming_buf), shrink=False)
            self._streaming_buf = ""

    def append_thinking(self, text: str) -> None:
        self._safe_write(Text(text, style="dim"), shrink=False)

    def start_thinking(self, label: str) -> None:
        self._flush_streaming_buf()
        self._in_thinking = True
        self._safe_write(Text(f"💭 [{label}] thinking...", style="dim"))

    def end_thinking(self) -> None:
        self._in_thinking = False

    def _flush_streaming_buf(self) -> None:
        if self._streaming_buf:
            self._safe_write(Text(self._streaming_buf), shrink=False)
            self._streaming_buf = ""

    def append_status(self, text: str) -> None:
        self._flush_streaming_buf()
        self._safe_write(text)

    def _handle_run_complete(self, result_path: Path) -> None:
        self._handle_run_end(
            "Done — press Ctrl+C to exit",
            Text(f"✓ Complete: {result_path.name}", style="bold green"),
            bell=True,
        )

    def _handle_run_failed(self, error: str, tb: str = "") -> None:
        self._flush_streaming_buf()
        self._safe_write("")
        self._safe_write(Text("─" * 60, style="red"))
        self._safe_write(Text("Run failed", style="bold red"))
        self._safe_write(Text("─" * 60, style="red"))
        self._safe_write(Text(error or "(no error message)", style="red"))
        if tb:
            self._safe_write("")
            self._safe_write(Text("Traceback:", style="red"))
            for line in tb.strip().splitlines():
                self._safe_write(Text(line, style="dim red"))
        self._safe_write("")
        self._safe_write(Text(
            "Resume from the last saved iteration:",
            style="bold yellow",
        ))
        self._safe_write(Text("  autoplanner -c last", style="bold cyan"))
        self._safe_write(Text("─" * 60, style="red"))
        self._handle_run_end(
            "Error — press Ctrl+C to exit. Resume with: autoplanner -c last",
            Text(f"✗ Error: {error}", style="bold red"),
        )

    def _handle_run_end(self, status_text: str, message: Text, *, bell: bool = False) -> None:
        try:
            self._orchestrator_running = False
            self._finish_run(status_text, message, bell=bell)
        except Exception as e:
            debug(f"_handle_run_end: exception: {e}")
            self._orchestrator_running = False
