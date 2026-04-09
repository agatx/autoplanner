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
    claude_model: Annotated[str, typer.Option("--model", "-m", help="Claude model to use")] = "sonnet",
    reviewer: Annotated[Reviewer, typer.Option("--reviewer", "-r", help="Reviewer: auto, codex, or claude")] = Reviewer.AUTO,
) -> None:
    """Draft and iteratively refine a requirements document."""
    orchestrator.run(
        task,
        max_iterations=max_iterations,
        claude_model=claude_model,
        reviewer=reviewer,
    )


if __name__ == "__main__":
    app()
