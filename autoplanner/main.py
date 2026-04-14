from __future__ import annotations

import sys
from typing import Annotated, Optional

import typer

from autoplanner.orchestrator import Reviewer

app = typer.Typer(
    name="autoplanner",
    help="Iterative document refinement using Claude Code and Codex CLI.",
)


@app.command()
def run(
    task: Annotated[Optional[str], typer.Argument(help="Task description (or enter it in the TUI)")] = None,
    max_iterations: Annotated[int, typer.Option("--max-iter", "-n", help="Maximum refinement iterations")] = 5,
    reviewer: Annotated[Reviewer, typer.Option("--reviewer", "-r", help="Reviewer: auto, codex, or claude")] = Reviewer.AUTO,
    claude_model: Annotated[str, typer.Option("--claude-model", help="Claude model")] = "opus",
    claude_effort: Annotated[str, typer.Option("--claude-effort", help="Claude effort level (low, medium, high, max)")] = "high",
    codex_model: Annotated[str, typer.Option("--codex-model", help="Codex model (empty = use codex config)")] = "",
    codex_effort: Annotated[str, typer.Option("--codex-effort", help="Codex reasoning effort (empty = use codex config)")] = "",
    headless: Annotated[bool, typer.Option("--headless", help="Run without TUI (plain terminal output)")] = False,
    continue_run: Annotated[Optional[str], typer.Option(
        "--continue", "-c",
        help="Resume a previous run. Use 'last' for most recent, or pass a run directory name.",
    )] = None,
    skip_to_walkthrough: Annotated[Optional[str], typer.Option(
        "--skip-to-walkthrough",
        help="Skip draft/review; provide path to .autoplanner run dir or 'synthetic'",
    )] = None,
    ingest: Annotated[Optional[str], typer.Option(
        "--ingest",
        help="Load a pre-existing markdown file as the initial draft (skip drafting)",
    )] = None,
    human_review: Annotated[bool, typer.Option(
        "--human-review", "-H",
        help="Enable human-in-the-loop review of high-stakes decisions",
    )] = False,
    on_decision: Annotated[Optional[str], typer.Option(
        "--on-decision",
        help="Decision resolution policy: prompt, accept, or fail (default: auto-detect from TTY)",
    )] = None,
    on_parse_error: Annotated[Optional[str], typer.Option(
        "--on-parse-error",
        help="Parse error policy: warn or fail (default: auto-detect from TTY)",
    )] = None,
    skip_permissions: Annotated[bool, typer.Option(
        "--dangerously-skip-permissions",
        help="Pass --dangerously-skip-permissions to Claude and --full-auto to Codex",
    )] = False,
    enable_debug: Annotated[bool, typer.Option(
        "--debug",
        help="Enable diagnostic logging to stderr",
    )] = False,
) -> None:
    """Draft and iteratively refine a requirements document."""
    if enable_debug:
        from autoplanner.debug import enable
        enable()

    # TTY auto-detection for decision policies
    if on_decision is None:
        on_decision = "prompt" if sys.stdin.isatty() else "fail"
    if on_parse_error is None:
        on_parse_error = "warn" if sys.stdin.isatty() else "fail"
    if on_decision not in ("prompt", "accept", "fail"):
        raise typer.BadParameter(f"Invalid --on-decision: {on_decision}")
    if on_parse_error not in ("warn", "fail"):
        raise typer.BadParameter(f"Invalid --on-parse-error: {on_parse_error}")

    hitl_kwargs = dict(
        human_review=human_review,
        on_decision_policy=on_decision,
        on_parse_error_policy=on_parse_error,
    )

    if continue_run is not None:
        if headless:
            from autoplanner import orchestrator
            orchestrator.resume(
                continue_run,
                max_iterations=max_iterations,
                claude_model=claude_model,
                claude_effort=claude_effort,
                codex_model=codex_model,
                codex_effort=codex_effort,
                reviewer=reviewer,
                skip_permissions=skip_permissions,
                **hitl_kwargs,
            )
        else:
            from autoplanner.tui import AutoplannerApp
            tui = AutoplannerApp(
                None,
                max_iterations=max_iterations,
                claude_model=claude_model,
                claude_effort=claude_effort,
                codex_model=codex_model,
                codex_effort=codex_effort,
                reviewer=reviewer,
                continue_run=continue_run,
                skip_permissions=skip_permissions,
                **hitl_kwargs,
            )
            tui.run()
        return

    if headless:
        if not task:
            raise typer.BadParameter("Task is required in --headless mode")
        from autoplanner import orchestrator
        orchestrator.run(
            task,
            max_iterations=max_iterations,
            claude_model=claude_model,
            claude_effort=claude_effort,
            codex_model=codex_model,
            codex_effort=codex_effort,
            reviewer=reviewer,
            skip_to_walkthrough=skip_to_walkthrough,
            ingest=ingest,
            skip_permissions=skip_permissions,
            **hitl_kwargs,
        )
    else:
        if not task and skip_to_walkthrough:
            task = "walkthrough-test"
        from autoplanner.tui import AutoplannerApp
        tui = AutoplannerApp(
            task,
            max_iterations=max_iterations,
            claude_model=claude_model,
            claude_effort=claude_effort,
            codex_model=codex_model,
            codex_effort=codex_effort,
            reviewer=reviewer,
            skip_to_walkthrough=skip_to_walkthrough,
            ingest=ingest,
            skip_permissions=skip_permissions,
            **hitl_kwargs,
        )
        tui.run()


if __name__ == "__main__":
    app()
