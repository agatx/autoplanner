from __future__ import annotations

from pathlib import Path

from rich.console import Console

from autoplanner.agents import claude_agent, codex_agent
from autoplanner.history import History, IterationRecord
from autoplanner.steering import SteeringInput

console = Console()


def is_done(review_text: str, iteration: int, max_iterations: int) -> bool:
    if iteration >= max_iterations:
        console.print(f"[yellow]Reached max iterations ({max_iterations}), stopping.[/yellow]")
        return True
    if review_text.strip().upper().startswith("LGTM"):
        console.print("[green]Reviewer approved the document.[/green]")
        return True
    return False


def run(
    task: str,
    *,
    max_iterations: int = 5,
    output_dir: Path = Path("output"),
    claude_model: str = "sonnet",
) -> Path:
    history = History(task=task, output_dir=output_dir)
    steering = SteeringInput()
    steering.start()

    console.print(
        "[dim]Tip: type anything while agents work to steer the next phase.[/dim]"
    )

    document = ""
    review_text = ""

    for iteration in range(1, max_iterations + 1):
        # Collect any steering input typed so far
        user_steering = steering.drain()
        if user_steering:
            console.print(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        # --- Draft or Revise (Claude) ---
        if iteration == 1:
            console.print(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Drafting with Claude...")
            document = claude_agent.draft(task, steering=user_steering, model=claude_model)
            phase = "draft"
        else:
            console.print(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Revising with Claude...")
            document = claude_agent.revise(
                document, review_text, steering=user_steering, model=claude_model,
            )
            phase = "revision"

        path = history.add(IterationRecord(
            iteration=iteration,
            phase=phase,
            author="claude",
            content=document,
        ))
        console.print(f"  Saved {path}")

        # Check for steering before review too
        user_steering = steering.drain()
        if user_steering:
            console.print(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        # --- Review (Codex) ---
        console.print(f"  Reviewing with Codex...")
        review_text = codex_agent.review(document, task, iteration, steering=user_steering)

        history.add(IterationRecord(
            iteration=iteration,
            phase="review",
            author="codex",
            content=review_text,
        ))
        console.print(f"  Review received ({len(review_text)} chars)")

        if is_done(review_text, iteration, max_iterations):
            break

    steering.stop()

    # --- Generate walkthrough ---
    console.print("\n[bold]Generating walkthrough...[/bold]")
    walkthrough_path = history.generate_walkthrough()
    console.print(f"[green]Done! Walkthrough: {walkthrough_path}[/green]")

    final_path = output_dir / "final.md"
    final_path.write_text(document, encoding="utf-8")
    console.print(f"[green]Final document: {final_path}[/green]")

    return final_path
