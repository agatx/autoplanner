from __future__ import annotations

from enum import Enum
from pathlib import Path

from rich.console import Console

from autoplanner.agents import claude_agent, codex_agent
from autoplanner.agents.run import AgentTimeout
from autoplanner.history import History, IterationRecord, make_run_id, make_output_name
from autoplanner.steering import SteeringInput

console = Console()

AUTOPLANNER_DIR = ".autoplanner"


class Reviewer(str, Enum):
    AUTO = "auto"
    CODEX = "codex"
    CLAUDE = "claude"


def is_done(review_text: str, iteration: int, max_iterations: int) -> bool:
    if iteration >= max_iterations:
        console.print(f"[yellow]Reached max iterations ({max_iterations}), stopping.[/yellow]")
        return True
    if review_text.strip().upper().startswith("LGTM"):
        console.print("[green]Reviewer approved the document.[/green]")
        return True
    return False


def _resolve_reviewer(requested: Reviewer) -> Reviewer:
    """Determine which reviewer to use, running preflight checks as needed."""
    if requested == Reviewer.CLAUDE:
        return Reviewer.CLAUDE

    if requested == Reviewer.CODEX:
        console.print("  Checking Codex availability...")
        if codex_agent.preflight():
            return Reviewer.CODEX
        console.print("[red]Codex is unavailable and was explicitly requested. Aborting.[/red]")
        raise RuntimeError("Codex unavailable")

    # Auto mode: try codex, fall back to claude
    console.print("  Checking Codex availability...")
    if codex_agent.preflight():
        console.print("  [green]Codex available[/green]")
        return Reviewer.CODEX
    else:
        console.print("  [yellow]Codex unavailable — falling back to Claude for reviews[/yellow]")
        return Reviewer.CLAUDE


def _do_review(
    reviewer: Reviewer,
    document: str,
    task: str,
    iteration: int,
    *,
    max_iterations: int = 5,
    steering: str | None = None,
    claude_model: str = "sonnet",
) -> tuple[str, str]:
    """Run review, with fallback on failure. Returns (review_text, author)."""
    if reviewer == Reviewer.CODEX:
        try:
            console.print(f"  Reviewing with Codex...")
            text = codex_agent.review(
                document, task, iteration,
                max_iterations=max_iterations, steering=steering,
            )
            return text, "codex"
        except (RuntimeError, AgentTimeout) as e:
            console.print(f"  [yellow]Codex failed ({e}), falling back to Claude...[/yellow]")
            reviewer = Reviewer.CLAUDE

    console.print(f"  Reviewing with Claude...")
    text = claude_agent.review(
        document, task, iteration,
        max_iterations=max_iterations, steering=steering, model=claude_model,
    )
    return text, "claude"


def run(
    task: str,
    *,
    max_iterations: int = 5,
    claude_model: str = "sonnet",
    reviewer: Reviewer = Reviewer.AUTO,
) -> Path:
    cwd = Path.cwd()
    run_id = make_run_id(task)
    work_dir = cwd / AUTOPLANNER_DIR / run_id

    history = History(task=task, run_id=run_id, work_dir=work_dir)

    try:
        return _run_loop(
            task, history,
            cwd=cwd,
            max_iterations=max_iterations,
            claude_model=claude_model,
            reviewer=reviewer,
        )
    finally:
        history.release()


def _run_loop(
    task: str,
    history: History,
    *,
    cwd: Path,
    max_iterations: int,
    claude_model: str,
    reviewer: Reviewer,
) -> Path:
    steering = SteeringInput()
    steering.start()

    console.print(f"[dim]Run: {history.run_id}[/dim]")
    console.print(f"[dim]Work dir: {history.work_dir}[/dim]")

    # Resolve reviewer up front
    active_reviewer = _resolve_reviewer(reviewer)
    console.print(f"[dim]Reviewer: {active_reviewer.value}[/dim]")

    console.print(
        "[dim]Tip: type anything while agents work to steer the next phase.[/dim]"
    )

    document = ""
    review_text = ""

    for iteration in range(1, max_iterations + 1):
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
                document, review_text,
                iteration=iteration, max_iterations=max_iterations,
                steering=user_steering, model=claude_model,
            )
            phase = "revision"

        path = history.add(IterationRecord(
            iteration=iteration,
            phase=phase,
            author="claude",
            content=document,
        ))
        console.print(f"  Saved {path}")

        user_steering = steering.drain()
        if user_steering:
            console.print(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        # --- Review ---
        review_text, review_author = _do_review(
            active_reviewer, document, task, iteration,
            max_iterations=max_iterations,
            steering=user_steering, claude_model=claude_model,
        )

        history.add(IterationRecord(
            iteration=iteration,
            phase="review",
            author=review_author,
            content=review_text,
        ))
        console.print(f"  Review received from {review_author} ({len(review_text)} chars)")

        if is_done(review_text, iteration, max_iterations):
            break

    steering.stop()

    # --- Save iteration data ---
    history.save_json()

    # --- Generate narrative walkthrough via Claude ---
    console.print("\n[bold]Generating walkthrough...[/bold]")
    walkthrough = _generate_walkthrough(task, history, claude_model=claude_model)

    # Save walkthrough to work dir too
    (history.work_dir / "walkthrough.md").write_text(walkthrough, encoding="utf-8")

    # --- Save final documents to cwd ---
    final_name = make_output_name(task, "requirements")
    final_path = cwd / final_name
    final_path.write_text(document, encoding="utf-8")
    console.print(f"[green]Final document: {final_path.name}[/green]")

    walkthrough_name = make_output_name(task, "walkthrough")
    walkthrough_path = cwd / walkthrough_name
    walkthrough_path.write_text(walkthrough, encoding="utf-8")
    console.print(f"[green]Walkthrough:    {walkthrough_path.name}[/green]")

    return final_path


def _generate_walkthrough(task: str, history: History, *, claude_model: str) -> str:
    from pathlib import Path as _Path
    prompts_dir = _Path(__file__).parent / "prompts"
    template = (prompts_dir / "walkthrough.txt").read_text(encoding="utf-8")
    prompt = template.format(
        task=task,
        iteration_history=history.build_iteration_history(),
    )
    from autoplanner.agents.run import stream_command, StreamMode
    return stream_command(
        [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", claude_model,
            "--no-session-persistence",
            prompt,
        ],
        label="claude-walkthrough",
        mode=StreamMode.CLAUDE,
    )
