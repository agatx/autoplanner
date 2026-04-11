# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

AutoPlanner is an agentic document refinement loop. It spawns **Claude Code** (writer) and **Codex CLI** (reviewer) as persistent subprocess sessions, iterating draft-review-revise cycles until the reviewer approves (LGTM) or max iterations are reached. A final walkthrough summarizes the document's evolution.

## Commands

```bash
# Install (editable, into pipx)
pipx install -e .

# Run with TUI (default)
autoplanner "Design a caching layer"

# Run headless
autoplanner --headless "Design a caching layer"

# Refine existing doc
autoplanner --ingest existing.md "Improve the design"

# Resume most recent run
autoplanner -c last

# Resume a specific run (substring match)
autoplanner -c caching-layer

# Regenerate walkthrough from prior run
autoplanner --skip-to-walkthrough .autoplanner/<run-id> "Task"
```

Python 3.11+ required. No linter config, no CI. Dependencies: typer, rich, textual. Dev dependency: pytest.

## Tests

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests
python3 -m pytest tests/ -v
```

Tests cover pure logic only — no subprocess mocking, no TUI rendering. The suite runs in under a second.

| Module | What it covers |
|---|---|
| `test_extract_markdown.py` | `_extract_markdown`: fenced blocks, heading fallback, preamble stripping, edge cases |
| `test_history.py` | `_slugify`, `find_run_dir`, `last_document_and_review`, `build_iteration_history`, JSON round-trip |
| `test_is_done.py` | `is_done`: LGTM detection (case/whitespace/prefix), max iterations |
| `test_steering.py` | `_drain_queue`, `QueueSteering` put/drain lifecycle |
| `test_session_helpers.py` | `_is_transient` (transient vs permanent errors), `steering_block` formatting |

## Architecture

### Orchestrator loop (`orchestrator.py`)

The core loop in `_run_loop` drives: draft (iteration 1) -> review -> revise (iteration 2+) -> review -> ... -> walkthrough. The `is_done` function terminates on LGTM prefix or max iterations. Steering input is drained at three points per iteration (pre-phase, mid-draft/revise, mid-review) and triggers corrections via `claude_agent.correct()`.

### Session model (`agents/session.py`)

`ClaudeSession` is a **long-lived subprocess** running `claude -p --input-format stream-json --output-format stream-json`. Messages are sent as JSON lines on stdin; responses are parsed from stream events on stdout with non-blocking I/O (`select` + `os.read`). Session IDs are tracked to maintain conversation context across turns. The process is restarted transparently if it dies.

`CodexSession` spawns a **new `codex exec` process per turn** but passes thread IDs to resume sessions. It reads JSON events line-by-line from stdout.

Both session types retry transient errors (overloaded/529/503) up to 3 times with exponential backoff (15s, 30s, 60s). All spawned process groups are tracked in `_active_pgroups` and killed via `atexit`.

### Agent modules (`agents/claude_agent.py`, `agents/codex_agent.py`)

Thin wrappers that load prompt templates, format them with context, and call `session.send()`. `claude_agent` has four operations: `draft`, `revise`, `correct`, `review`. `codex_agent` has `review` and `preflight`. Both agents use the same `review.txt` prompt template.

### Output routing (`output.py`)

A `Writer` protocol with two implementations: `TerminalWriter` (headless) and `TuiWriter` (Textual app). The active writer is a module-level singleton set at startup. All output (streaming text, thinking indicators, status messages) flows through `get_writer()`.

### TUI (`tui.py`)

Textual app with three widgets: `OutputLog` (scrolling), status bar, and `Input`. The orchestrator runs in a `@work(thread=True)` worker thread. `TuiWriter` buffers streaming chunks and flushes them to the main thread via `call_from_thread`.

### Steering (`steering.py`)

`SteeringSource` protocol with `StdinSteering` (headless, background thread on stdin) and `QueueSteering` (TUI, fed from Input widget). The orchestrator calls `drain()` to collect accumulated messages.

### Prompts (`prompts.py`, `prompts/`)

Templates are plain `.txt` files loaded once via `lru_cache`. They use Python `str.format()` placeholders: `{task}`, `{document}`, `{review}`, `{iteration}`, `{max_iterations}`, `{remaining}`. Steering is appended via `steering_block()`.

### History (`history.py`)

Each run gets a work directory under `.autoplanner/<slug>-<timestamp>/`. Iteration artifacts are saved as `NN_phase.md`. A file lock (`fcntl.flock`) prevents concurrent runs in the same directory. `history.json` stores the machine-readable log. `History.from_directory()` reconstructs from a prior run for walkthrough regeneration or resume. `find_run_dir()` locates run directories by exact name, substring match, or most-recent.

## Key Design Decisions

- **Persistent sessions over one-shot calls**: Claude runs as a single long-lived subprocess per role (writer, reviewer, walkthrough) to preserve conversation context and benefit from token caching.
- **Codex is one-process-per-turn**: Unlike Claude, Codex uses `codex exec` with thread ID resumption rather than a persistent stdin/stdout stream.
- **Reviewer fallback**: `auto` mode tries Codex first (with a preflight health check), falls back to Claude. Mid-run Codex failures also fall back transparently.
- **Non-blocking stdout parsing**: ClaudeSession uses `select()` + `os.O_NONBLOCK` to read stream-json events without blocking the thread.
- **Debug logging goes to a file**: In TUI mode, writing to stderr corrupts Textual's rendering, so `debug.py` writes to `autoplanner-debug.log` via raw `os.write()`. Enable with `--debug` or `AUTOPLANNER_DEBUG=1`.
- **Resume uses fresh sessions**: `-c` restores document/review state from `history.json` but creates new subprocess sessions. Conversation context does not carry over — the revise prompt provides enough context for Claude to continue meaningfully.

## Workflow Reminders

- **Update README.md** when adding new features, CLI options, or changing any public-facing behavior. The README has a usage section, options table, and examples section that must stay in sync with `main.py`.
