from __future__ import annotations

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
    enable_debug: Annotated[bool, typer.Option(
        "--debug",
        help="Enable diagnostic logging to stderr",
    )] = False,
) -> None:
    """Draft and iteratively refine a requirements document."""
    if enable_debug:
        from autoplanner.debug import enable
        enable()

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
        )
        tui.run()


if __name__ == "__main__":
    app()
