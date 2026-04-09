from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from autoplanner import orchestrator

app = typer.Typer(
    name="autoplanner",
    help="Iterative document refinement using Claude Code and Codex CLI.",
)


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="The task or topic for the requirements document")],
    max_iterations: Annotated[int, typer.Option("--max-iter", "-n", help="Maximum refinement iterations")] = 5,
    output_dir: Annotated[Path, typer.Option("--output", "-o", help="Output directory for artifacts")] = Path("output"),
    claude_model: Annotated[str, typer.Option("--model", "-m", help="Claude model to use")] = "sonnet",
) -> None:
    """Draft and iteratively refine a requirements document."""
    orchestrator.run(
        task,
        max_iterations=max_iterations,
        output_dir=output_dir,
        claude_model=claude_model,
    )


if __name__ == "__main__":
    app()
