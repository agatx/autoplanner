# AutoPlanner

Agentic document refinement loop that uses **Claude Code** for drafting/revising and **Codex CLI** for reviewing technical design documents and implementation plans.

## How it works

1. **Claude** drafts a requirements document from your task description
2. **Codex** (or Claude) reviews it — pushing back on architecture, completeness, edge cases
3. **Claude** revises based on feedback
4. Loop repeats until the reviewer approves (LGTM) or max iterations are reached
5. A narrative **walkthrough** is generated analyzing the document's evolution and key decisions

Each agent runs in a **persistent session** — conversation context carries across iterations, and token caching keeps costs down.

## Install

```bash
pipx install -e .
```

Requires `claude` and `codex` CLIs on your PATH.

## Usage

```bash
# Launch TUI — type the task interactively
autoplanner

# Launch TUI with a task
autoplanner "Design a user authentication system with OAuth2 and MFA"

# Headless mode (plain terminal output)
autoplanner --headless "Design a user auth system"
```

### TUI

The default mode opens a terminal UI with a scrolling log and a fixed input box at the bottom. Type steering instructions at any time — they're applied at the next phase transition, or immediately as a correction if typed while an agent is working.

Type `q`, `quit`, or Ctrl+C to exit. After a run completes, you can start a new task from the same session.

### Options

| Option | Default | Description |
|---|---|---|
| `TASK` | *(optional)* | Task description, or enter it in the TUI |
| `-n` / `--max-iter` | `5` | Maximum refinement iterations |
| `-r` / `--reviewer` | `auto` | Reviewer agent: `auto`, `codex`, or `claude` |
| `--claude-model` | `opus` | Claude model for writing and reviewing |
| `--claude-effort` | `high` | Claude effort level (`low`, `medium`, `high`, `max`) |
| `--codex-model` | *(from codex config)* | Codex model override |
| `--codex-effort` | *(from codex config)* | Codex reasoning effort override |
| `--headless` | off | Run without TUI (plain terminal output) |

### Examples

```bash
# Use Claude for both writing and reviewing
autoplanner -r claude "Design a caching layer"

# Limit to 3 iterations with sonnet (faster/cheaper)
autoplanner -n 3 --claude-model sonnet "API rate limiting design"

# Override codex model
autoplanner --codex-model o3 "Database migration strategy"
```

## Output

Two files are saved to the current directory:

- `<slug>-<timestamp>-requirements.md` — the final document
- `<slug>-<timestamp>-walkthrough.md` — narrative analysis of the document's evolution, key decisions, and attribution

Intermediate iterations are saved in `.autoplanner/<run-id>/` along with `history.json`.

## Steering

Type instructions while agents are working to steer the process:

- **Between phases** — applied as context to the next agent call
- **During an agent response** — picked up immediately after the response completes and applied as a correction before the next phase

Examples of steering input:
```
focus more on security requirements
skip the mobile section, that's out of scope
the auth flow should use PKCE
```

## Prompts

Agent prompts live in `autoplanner/prompts/` and can be edited to change behavior:

- `claude_draft.txt` — initial document drafting instructions
- `claude_revise.txt` — revision instructions (iteration-aware)
- `codex_review.txt` — review criteria (iteration-aware, converges toward approval)
- `walkthrough.txt` — narrative walkthrough generation

## Architecture

```
autoplanner/
  main.py            CLI entrypoint (Typer)
  tui.py             Textual TUI with log + input panes
  orchestrator.py    Main loop: draft → review → revise → check done
  output.py          Pluggable writer (terminal vs TUI)
  steering.py        Steering input (stdin or TUI queue)
  prompts.py         Cached prompt template loading
  history.py         Iteration tracking, file saving, walkthrough data
  agents/
    session.py       Persistent sessions for Claude and Codex CLIs
    claude_agent.py  Draft, revise, correct, review functions
    codex_agent.py   Review function + preflight check
  prompts/
    claude_draft.txt
    claude_revise.txt
    codex_review.txt
    walkthrough.txt
```
