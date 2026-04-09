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
    codex_model: Annotated[str, typer.Option("--codex-model", help="Codex model")] = "gpt-4.3",
    codex_effort: Annotated[str, typer.Option("--codex-effort", help="Codex reasoning effort (low, medium, high, xhigh)")] = "xhigh",
    headless: Annotated[bool, typer.Option("--headless", help="Run without TUI (plain terminal output)")] = False,
) -> None:
    """Draft and iteratively refine a requirements document."""
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
        )
    else:
        from autoplanner.tui import AutoplannerApp
        tui = AutoplannerApp(
            task,
            max_iterations=max_iterations,
            claude_model=claude_model,
            claude_effort=claude_effort,
            codex_model=codex_model,
            codex_effort=codex_effort,
            reviewer=reviewer,
        )
        tui.run()


if __name__ == "__main__":
    app()
