from __future__ import annotations

from typing import Annotated

import typer

from autoplanner import orchestrator
from autoplanner.orchestrator import Reviewer

app = typer.Typer(
    name="autoplanner",
    help="Iterative document refinement using Claude Code and Codex CLI.",
)


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="The task or topic for the requirements document")],
    max_iterations: Annotated[int, typer.Option("--max-iter", "-n", help="Maximum refinement iterations")] = 5,
    reviewer: Annotated[Reviewer, typer.Option("--reviewer", "-r", help="Reviewer: auto, codex, or claude")] = Reviewer.AUTO,
    claude_model: Annotated[str, typer.Option("--claude-model", help="Claude model")] = "opus",
    claude_effort: Annotated[str, typer.Option("--claude-effort", help="Claude effort level (low, medium, high, max)")] = "high",
    codex_model: Annotated[str, typer.Option("--codex-model", help="Codex model")] = "gpt-4.3",
    codex_effort: Annotated[str, typer.Option("--codex-effort", help="Codex reasoning effort (low, medium, high, xhigh)")] = "xhigh",
) -> None:
    """Draft and iteratively refine a requirements document."""
    orchestrator.run(
        task,
        max_iterations=max_iterations,
        claude_model=claude_model,
        claude_effort=claude_effort,
        codex_model=codex_model,
        codex_effort=codex_effort,
        reviewer=reviewer,
    )


if __name__ == "__main__":
    app()
