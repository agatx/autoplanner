# AutoPlanner

Agentic document refinement loop that uses **Claude Code** for drafting/revising and **Codex CLI** for reviewing technical design documents and implementation plans.

## How it works

1. **Claude** drafts a requirements document from your task description
2. **Codex** (or Claude) reviews it — pushing back on architecture, completeness, edge cases
3. **Claude** revises based on feedback
4. Loop repeats until the reviewer approves (LGTM) or max iterations are reached
5. A narrative **walkthrough** is generated analyzing the document's evolution and key decisions

Each agent runs in a **persistent session** — conversation context carries across iterations, and token caching keeps costs down.

### Iteration-aware prompts

Review and revision prompts adapt to the current iteration:

- **Early iterations (1-2):** The reviewer focuses on high-level design and architectural soundness, pushing back hard on questionable decisions.
- **Middle iterations:** Focus shifts to completeness, consistency, and feasibility.
- **Final iterations:** The reviewer converges toward approval, flagging only critical issues. The writer makes targeted fixes rather than restructuring.

## Install

```bash
pipx install -e .
```

Requires `claude` CLI on your PATH. `codex` CLI is optional — if unavailable, Claude handles reviews too.

## Usage

```bash
# Launch TUI — type the task interactively
autoplanner

# Launch TUI with a task
autoplanner "Design a user authentication system with OAuth2 and MFA"

# Headless mode (plain terminal output)
autoplanner --headless "Design a user auth system"

# Skip drafting — refine an existing document
autoplanner --ingest existing-spec.md "Improve the auth system design"

# Resume the most recent run
autoplanner -c last

# Resume a specific run (exact or substring match on directory name)
autoplanner -c design-auth

# Regenerate walkthrough from a previous run
autoplanner --skip-to-walkthrough .autoplanner/design-auth-20260410-143022 "Auth system"
```

### TUI

The default mode opens a terminal UI with a scrolling log and a fixed input box at the bottom. Agent thinking and output stream in real time. Type steering instructions at any time — they're applied at the next phase transition, or immediately as a correction if typed while an agent is working.

Type `q`, `quit`, or Ctrl+C to exit. After a run completes, you can start a new task from the same session.

### Options

| Option | Default | Description |
|---|---|---|
| `TASK` | *(optional)* | Task description, or enter it in the TUI |
| `-n` / `--max-iter` | `5` | Maximum refinement iterations |
| `-r` / `--reviewer` | `auto` | Reviewer agent: `auto`, `codex`, or `claude` |
| `--claude-model` | `opus` | Claude model for writing and reviewing |
| `--claude-effort` | `high` | Claude effort level (`low`, `medium`, `high`, `max`) |
| `--codex-model` | *(from `~/.codex/config.toml`)* | Codex model override |
| `--codex-effort` | *(from `~/.codex/config.toml`)* | Codex reasoning effort override |
| `--headless` | off | Run without TUI (plain terminal output) |
| `-c` / `--continue` | — | Resume a previous run. `last` for most recent, or a run directory name (substring match supported) |
| `--ingest` | — | Path to a markdown file to use as the initial draft (skips drafting) |
| `--skip-to-walkthrough` | — | Path to a `.autoplanner` run directory; skips draft/review and regenerates the walkthrough only |
| `-H` / `--human-review` | off | Enable human-in-the-loop review of high-stakes architectural decisions |
| `--on-decision` | auto | Decision resolution policy: `prompt` (interactive), `accept` (auto-accept), `fail` (abort). Default: `prompt` if TTY, `fail` otherwise |
| `--on-parse-error` | auto | Parse error policy: `warn` (continue) or `fail` (abort). Default: `warn` if TTY, `fail` otherwise |
| `--dangerously-skip-permissions` | off | Pass `--dangerously-skip-permissions` to Claude and `--full-auto` to Codex, allowing agents to access files outside the current directory without prompting |
| `--debug` | off | Enable diagnostic logging to `autoplanner-debug.log` |

### Examples

```bash
# Use Claude for both writing and reviewing
autoplanner -r claude "Design a caching layer"

# Limit to 3 iterations with sonnet (faster/cheaper)
autoplanner -n 3 --claude-model sonnet "API rate limiting design"

# Override codex model
autoplanner --codex-model o3 "Database migration strategy"

# Start from an existing draft and refine it
autoplanner --ingest draft-v1.md "Database migration strategy"

# Resume the last run with more iterations
autoplanner -c last -n 10

# Resume a run by substring match
autoplanner -c db-migration

# Regenerate walkthrough for an earlier run
autoplanner --skip-to-walkthrough .autoplanner/db-migration-20260409-120000 "Database migration"

# Human-in-the-loop: pause on high-stakes decisions
autoplanner -H "Design a caching layer"

# Auto-accept decisions in CI (no human prompt)
autoplanner --headless -H --on-decision accept "Design a caching layer"

# Skip permission prompts (agents can read files outside cwd)
autoplanner --dangerously-skip-permissions "Design a caching layer"
```

## Output

Two files are saved to the current directory:

- `<slug>-<timestamp>-requirements.md` — the final document
- `<slug>-<timestamp>-walkthrough.md` — narrative analysis of the document's evolution, key decisions, and attribution

Intermediate iterations are saved in `.autoplanner/<run-id>/`:

```
.autoplanner/<run-id>/
  01_draft.md       # Initial draft
  01_review.md      # First review
  02_revision.md    # Revised document
  02_review.md      # Second review
  ...
  walkthrough.md    # Evolution narrative
  history.json      # Machine-readable iteration log
```

A file lock prevents concurrent runs from writing to the same work directory.

### Session stats

At the end of a run, a summary is printed: iteration count, wall time (including walkthrough), time attributed to each contributor (claude / codex / human), decision counts by state, and final document size.

## Steering

Type instructions while agents are working to steer the process:

- **Between phases** — applied as context to the next agent call
- **During drafting/revision** — picked up after the response completes and applied as an immediate correction
- **During review** — triggers a correction to the document, then a re-review

Examples of steering input:
```
focus more on security requirements
skip the mobile section, that's out of scope
the auth flow should use PKCE
```

## Reviewer selection

The `--reviewer` flag controls which agent reviews the document:

- **`auto`** (default) — tries Codex first (via a preflight health check), falls back to Claude if Codex is unavailable or rate-limited.
- **`codex`** — requires Codex; aborts if unavailable.
- **`claude`** — uses Claude for reviews (same model/effort as the writer, but a separate session to avoid context contamination).

If Codex fails mid-run (e.g. rate limit hit during a review), the orchestrator falls back to Claude for that review automatically.

## Resuming runs

Use `-c` / `--continue` to pick up where a previous run left off — useful after hitting max iterations:

```bash
autoplanner -c last          # most recent run
autoplanner -c caching       # substring match on run directory name
autoplanner -c last -n 10    # resume with a higher iteration cap
```

Resume loads `history.json` from the run's work directory, determines the last completed phase, and continues the draft-review loop from there. New sessions are created (conversation context doesn't carry over), but the document and review state are restored.

## Human-in-the-loop review

With `-H` / `--human-review`, the reviewer is instructed to identify high-stakes architectural decisions — technology choices, data model designs, security boundaries — and emit them as structured decision records. When decisions are detected, the loop pauses and presents each decision to the human one at a time with options, trade-offs, and the document's current choice.

The human picks an option (e.g., `B`) or types `skip` to accept the current choice. An optional note can follow the key (e.g., `B — but keep a TTL fallback for cold starts`). Anything else is treated as a question — the document author (Claude) will explain its reasoning and trade-offs before you commit. You can ask as many follow-up questions before picking an option. The choice is recorded as a **locked decision** — a binding constraint injected into all subsequent writer and reviewer prompts.

If the reviewer later identifies a conflict with a locked decision, it can raise a conflict proposal. The human resolves conflicts the same way, choosing to supersede the original or keep it.

Decision-driven extra iterations (to incorporate locked choices) do not count against `--max-iter`. They are capped separately at 3 passes. If the budget is exhausted, the run terminates with exit code 2 and pending decisions are preserved for resume with `-c`.

### Headless policies

| `--on-decision` | Behavior |
|---|---|
| `prompt` (default with TTY) | Print decisions to stdout, read from stdin |
| `accept` | Auto-accept the document's current choice |
| `fail` | Abort with non-zero exit code |

## Resilience

- **Transient errors** (overloaded, 529, 503, rate limits) are retried up to 3 times with exponential backoff (15s, 30s, 60s).
- **Process crashes** — if a Claude session process dies, it is restarted transparently on the next send.
- **Cleanup** — all spawned subprocesses are tracked and killed on exit via `atexit`, even on unexpected termination.

## Prompts

Agent prompts live in `autoplanner/prompts/` and can be edited to change behavior:

- `draft.txt` — initial document drafting instructions
- `revise.txt` — revision instructions (iteration-aware)
- `review.txt` — review criteria (iteration-aware, converges toward approval)
- `walkthrough.txt` — narrative walkthrough generation

## Tests

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests
python3 -m pytest tests/ -v
```

The test suite covers pure logic — markdown extraction, slug generation, run directory lookup, loop termination, steering queue draining, transient error detection, and history serialization. No subprocess mocking or TUI rendering. Runs in under a second.

## Architecture

```
autoplanner/
  main.py            CLI entrypoint (Typer)
  tui.py             Textual TUI with streaming log + input
  orchestrator.py    Main loop: draft → review → revise → done?
  output.py          Pluggable writer protocol (terminal vs TUI)
  steering.py        Live steering input (stdin or TUI queue)
  prompts.py         Cached prompt template loader
  history.py         Iteration records, decision state, file persistence, JSON export
  decisions.py       Decision trailer extraction and validation
  debug.py           Opt-in diagnostic logging and event-loop heartbeat
  agents/
    session.py       Persistent subprocess sessions (Claude + Codex CLIs)
    claude_agent.py  Draft, revise, correct, review via Claude
    codex_agent.py   Review + preflight check via Codex
  prompts/
    draft.txt
    revise.txt
    review.txt
    walkthrough.txt
    decisions_instruction.txt
```
