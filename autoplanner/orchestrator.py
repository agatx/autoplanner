from __future__ import annotations

from enum import Enum
from pathlib import Path

from autoplanner.agents import claude_agent, codex_agent
from autoplanner.agents.session import ClaudeSession, CodexSession
from autoplanner.history import History, IterationRecord, make_run_id, make_output_name
from autoplanner.output import get_writer
from autoplanner.steering import SteeringSource, StdinSteering

AUTOPLANNER_DIR = ".autoplanner"


class Reviewer(str, Enum):
    AUTO = "auto"
    CODEX = "codex"
    CLAUDE = "claude"


def is_done(review_text: str, iteration: int, max_iterations: int) -> bool:
    w = get_writer()
    if iteration >= max_iterations:
        w.write_status(f"[yellow]Reached max iterations ({max_iterations}), stopping.[/yellow]")
        return True
    if review_text.strip().upper().startswith("LGTM"):
        w.write_status("[green]Reviewer approved the document.[/green]")
        return True
    return False


def _resolve_reviewer(requested: Reviewer) -> Reviewer:
    w = get_writer()
    if requested == Reviewer.CLAUDE:
        return Reviewer.CLAUDE

    if requested == Reviewer.CODEX:
        w.write_status("  Checking Codex availability...")
        if codex_agent.preflight():
            return Reviewer.CODEX
        w.write_status("[red]Codex is unavailable and was explicitly requested. Aborting.[/red]")
        raise RuntimeError("Codex unavailable")

    w.write_status("  Checking Codex availability...")
    if codex_agent.preflight():
        w.write_status("  [green]Codex available[/green]")
        return Reviewer.CODEX
    else:
        w.write_status("  [yellow]Codex unavailable — falling back to Claude for reviews[/yellow]")
        return Reviewer.CLAUDE


def _do_review(
    active_reviewer: Reviewer,
    codex_session: CodexSession,
    claude_review_session: ClaudeSession,
    document: str,
    task: str,
    iteration: int,
    *,
    max_iterations: int = 5,
    steering: str | None = None,
) -> tuple[str, str]:
    w = get_writer()
    if active_reviewer == Reviewer.CODEX:
        try:
            w.write_status("  Reviewing with Codex...")
            text = codex_agent.review(
                codex_session, document, task, iteration,
                max_iterations=max_iterations, steering=steering,
            )
            return text, "codex"
        except RuntimeError as e:
            w.write_status(f"  [yellow]Codex failed ({e}), falling back to Claude...[/yellow]")

    w.write_status("  Reviewing with Claude...")
    text = claude_agent.review(
        claude_review_session, document, task, iteration,
        max_iterations=max_iterations, steering=steering,
    )
    return text, "claude"


def run(
    task: str,
    *,
    max_iterations: int = 5,
    claude_model: str = "opus",
    claude_effort: str = "high",
    codex_model: str = "gpt-4.3",
    codex_effort: str = "xhigh",
    reviewer: Reviewer = Reviewer.AUTO,
    steering_source: SteeringSource | None = None,
) -> Path:
    cwd = Path.cwd()
    run_id = make_run_id(task)
    work_dir = cwd / AUTOPLANNER_DIR / run_id

    history = History(task=task, run_id=run_id, work_dir=work_dir)

    writer_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude")
    claude_review_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude-review")
    codex_session = CodexSession(model=codex_model, effort=codex_effort, label="codex")
    walkthrough_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude-walkthrough")

    if steering_source is None:
        steering_source = StdinSteering()

    try:
        return _run_loop(
            task, history,
            writer_session=writer_session,
            claude_review_session=claude_review_session,
            codex_session=codex_session,
            walkthrough_session=walkthrough_session,
            cwd=cwd,
            max_iterations=max_iterations,
            reviewer=reviewer,
            steering=steering_source,
        )
    finally:
        history.release()
        writer_session.close()
        claude_review_session.close()
        codex_session.close()
        walkthrough_session.close()


def _run_loop(
    task: str,
    history: History,
    *,
    writer_session: ClaudeSession,
    claude_review_session: ClaudeSession,
    codex_session: CodexSession,
    walkthrough_session: ClaudeSession,
    cwd: Path,
    max_iterations: int,
    reviewer: Reviewer,
    steering: SteeringSource,
) -> Path:
    w = get_writer()
    steering.start()

    w.write_status(f"[dim]Run: {history.run_id}[/dim]")
    w.write_status(f"[dim]Work dir: {history.work_dir}[/dim]")
    w.write_status(f"[dim]Writer: {writer_session.model} (effort: {writer_session.effort})[/dim]")
    w.write_status(f"[dim]Codex:  {codex_session.model} (effort: {codex_session.effort})[/dim]")

    active_reviewer = _resolve_reviewer(reviewer)
    w.write_status(f"[dim]Reviewer: {active_reviewer.value}[/dim]")

    document = ""
    review_text = ""

    for iteration in range(1, max_iterations + 1):
        user_steering = steering.drain()
        if user_steering:
            w.write_status(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        if iteration == 1:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Drafting with Claude...")
            document = claude_agent.draft(writer_session, task, steering=user_steering)
            phase = "draft"
        else:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Revising with Claude...")
            document = claude_agent.revise(
                writer_session, document, review_text,
                iteration=iteration, max_iterations=max_iterations,
                steering=user_steering,
            )
            phase = "revision"

        path = history.add(IterationRecord(
            iteration=iteration,
            phase=phase,
            author="claude",
            content=document,
        ))
        w.write_status(f"  Saved {path}")

        user_steering = steering.drain()
        if user_steering:
            w.write_status(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        review_text, review_author = _do_review(
            active_reviewer, codex_session, claude_review_session,
            document, task, iteration,
            max_iterations=max_iterations,
            steering=user_steering,
        )

        history.add(IterationRecord(
            iteration=iteration,
            phase="review",
            author=review_author,
            content=review_text,
        ))
        w.write_status(f"  Review received from {review_author} ({len(review_text)} chars)")

        if is_done(review_text, iteration, max_iterations):
            break

    steering.stop()

    history.save_json()

    w.write_status("\n[bold]Generating walkthrough...[/bold]")
    walkthrough = _generate_walkthrough(task, history, walkthrough_session)
    (history.work_dir / "walkthrough.md").write_text(walkthrough, encoding="utf-8")

    final_name = make_output_name(task, "requirements")
    final_path = cwd / final_name
    final_path.write_text(document, encoding="utf-8")
    w.write_status(f"[green]Final document: {final_path.name}[/green]")

    walkthrough_name = make_output_name(task, "walkthrough")
    walkthrough_path = cwd / walkthrough_name
    walkthrough_path.write_text(walkthrough, encoding="utf-8")
    w.write_status(f"[green]Walkthrough:    {walkthrough_path.name}[/green]")

    return final_path


def _generate_walkthrough(task: str, history: History, session: ClaudeSession) -> str:
    from autoplanner.prompts import load
    prompt = load("walkthrough.txt").format(
        task=task,
        iteration_history=history.build_iteration_history(),
    )
    return session.send(prompt)
