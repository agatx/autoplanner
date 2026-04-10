from __future__ import annotations

import threading
from enum import Enum
from pathlib import Path

from autoplanner.agents import claude_agent, codex_agent
from autoplanner.agents.session import ClaudeSession, CodexSession
from autoplanner.history import History, IterationRecord, make_run_id, make_output_name, find_run_dir
from autoplanner.output import get_writer
from autoplanner.prompts import load
from autoplanner.steering import SteeringSource, StdinSteering

AUTOPLANNER_DIR = ".autoplanner"


def _close_sessions(sessions: list) -> None:
    for s in sessions:
        try:
            s.close()
        except Exception:
            pass


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
    codex_model: str = "",
    codex_effort: str = "",
    reviewer: Reviewer = Reviewer.AUTO,
    steering_source: SteeringSource | None = None,
    skip_to_walkthrough: str | None = None,
    ingest: str | None = None,
) -> Path:
    cwd = Path.cwd()

    # --- Fast path: walkthrough only ---
    if skip_to_walkthrough is not None:
        return _run_walkthrough_only(
            task, cwd,
            skip_to_walkthrough=skip_to_walkthrough,
            claude_model=claude_model,
            claude_effort=claude_effort,
        )

    run_id = make_run_id(task)
    work_dir = cwd / AUTOPLANNER_DIR / run_id

    history = History(task=task, run_id=run_id, work_dir=work_dir)

    writer_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude")
    claude_review_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude-review")
    codex_session = CodexSession(model=codex_model, effort=codex_effort, label="codex")
    walkthrough_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude-walkthrough")

    if steering_source is None:
        steering_source = StdinSteering()

    initial_document: str | None = None
    if ingest is not None:
        initial_document = Path(ingest).read_text(encoding="utf-8")

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
            initial_document=initial_document,
        )
    finally:
        history.release()
        sessions = [writer_session, claude_review_session,
                    codex_session, walkthrough_session]
        threading.Thread(
            target=_close_sessions, args=(sessions,), daemon=True,
        ).start()


def resume(
    run_id: str | None = None,
    *,
    max_iterations: int = 5,
    claude_model: str = "opus",
    claude_effort: str = "high",
    codex_model: str = "",
    codex_effort: str = "",
    reviewer: Reviewer = Reviewer.AUTO,
    steering_source: SteeringSource | None = None,
) -> Path:
    """Resume a previous run from where it left off."""
    cwd = Path.cwd()
    base_dir = cwd / AUTOPLANNER_DIR
    w = get_writer()

    work_dir = find_run_dir(base_dir, run_id)
    history = History.from_directory(work_dir, lock=True)
    w.write_status(f"[dim]Resuming run: {history.run_id}[/dim]")

    # Determine where we left off
    last_document = ""
    last_review = ""
    last_iteration = 0
    for rec in history.records:
        if rec.phase in ("draft", "revision"):
            last_document = rec.content
            last_iteration = rec.iteration
        elif rec.phase == "review":
            last_review = rec.content

    if not last_document:
        raise RuntimeError(f"No draft or revision found in {work_dir.name}")

    # If the last record is a draft/revision (no review for that iteration),
    # resume from that iteration and skip straight to review.
    # Otherwise start the next iteration with a revise.
    last_record = history.records[-1]
    if last_record.phase == "review":
        start_iteration = last_record.iteration + 1
        skip_write = False
    else:
        start_iteration = last_record.iteration
        skip_write = True

    w.write_status(
        f"[dim]Loaded {len(history.records)} records, "
        f"resuming from iteration {start_iteration}[/dim]"
    )

    writer_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude")
    claude_review_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude-review")
    codex_session = CodexSession(model=codex_model, effort=codex_effort, label="codex")
    walkthrough_session = ClaudeSession(model=claude_model, effort=claude_effort, label="claude-walkthrough")

    if steering_source is None:
        steering_source = StdinSteering()

    try:
        return _run_loop(
            history.task, history,
            writer_session=writer_session,
            claude_review_session=claude_review_session,
            codex_session=codex_session,
            walkthrough_session=walkthrough_session,
            cwd=cwd,
            max_iterations=max_iterations,
            reviewer=reviewer,
            steering=steering_source,
            initial_document=last_document,
            initial_review=last_review,
            start_iteration=start_iteration,
            resume_skip_write=skip_write,
        )
    finally:
        history.release()
        sessions = [writer_session, claude_review_session,
                    codex_session, walkthrough_session]
        threading.Thread(
            target=_close_sessions, args=(sessions,), daemon=True,
        ).start()


def _run_walkthrough_only(
    task: str,
    cwd: Path,
    *,
    skip_to_walkthrough: str,
    claude_model: str,
    claude_effort: str,
) -> Path:
    """Fast path: skip draft/review, run only walkthrough generation."""
    w = get_writer()

    src = Path(skip_to_walkthrough)
    history = History.from_directory(src)
    w.write_status(f"[dim]Loaded history from {src}[/dim]")

    document = ""
    for rec in reversed(history.records):
        if rec.phase in ("draft", "revision"):
            document = rec.content
            break

    walkthrough_session = ClaudeSession(
        model=claude_model, effort=claude_effort, label="claude-walkthrough",
    )

    try:
        w.write_status("\n[bold]Generating walkthrough...[/bold]")
        walkthrough = _generate_walkthrough(task, history, walkthrough_session)
        return _write_outputs(task, cwd, history.work_dir, document, walkthrough)
    finally:
        threading.Thread(
            target=_close_sessions, args=([walkthrough_session],), daemon=True,
        ).start()


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
    initial_document: str | None = None,
    initial_review: str = "",
    start_iteration: int = 1,
    resume_skip_write: bool = False,
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
    review_text = initial_review

    for iteration in range(start_iteration, max_iterations + 1):
        # --- Pre-phase steering ---
        user_steering = steering.drain()
        if user_steering:
            w.write_status(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        # --- Draft or Revise ---
        if resume_skip_write and iteration == start_iteration:
            # Resuming a run that had a draft but no review — skip to review
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Resuming from prior draft...")
            document = initial_document
            phase = "revision"
        elif iteration == 1 and initial_document is not None:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Using ingested document...")
            document = initial_document
            phase = "draft"
        elif iteration == start_iteration and initial_document is not None and start_iteration > 1:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Revising with Claude (resumed)...")
            document = claude_agent.revise(
                writer_session, initial_document, review_text,
                iteration=iteration, max_iterations=max_iterations,
                steering=user_steering,
            )
            phase = "revision"
        elif iteration == 1:
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

        # --- Immediate correction if steering arrived during draft/revise ---
        mid_steering = steering.drain()
        if mid_steering:
            w.write_status(f"  [bold magenta]Mid-phase steering — applying correction:[/bold magenta] {mid_steering}")
            document = claude_agent.correct(writer_session, mid_steering)
            phase = "revision"

        path = history.add(IterationRecord(
            iteration=iteration,
            phase=phase,
            author="claude",
            content=document,
        ))
        w.write_status(f"  Saved {path}")

        # --- Pre-review steering ---
        user_steering = steering.drain()
        if user_steering:
            w.write_status(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        # --- Review ---
        review_text, review_author = _do_review(
            active_reviewer, codex_session, claude_review_session,
            document, task, iteration,
            max_iterations=max_iterations,
            steering=user_steering,
        )

        # --- Immediate correction if steering arrived during review ---
        mid_steering = steering.drain()
        if mid_steering:
            w.write_status(f"  [bold magenta]Mid-review steering — applying correction:[/bold magenta] {mid_steering}")
            document = claude_agent.correct(writer_session, mid_steering)
            # Re-save the corrected document
            history.add(IterationRecord(
                iteration=iteration,
                phase="revision",
                author="claude",
                content=document,
            ))
            # Re-review with the corrected document
            w.write_status("  Re-reviewing corrected document...")
            review_text, review_author = _do_review(
                active_reviewer, codex_session, claude_review_session,
                document, task, iteration,
                max_iterations=max_iterations,
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
    return _write_outputs(task, cwd, history.work_dir, document, walkthrough)


def _write_outputs(
    task: str, cwd: Path, work_dir: Path, document: str, walkthrough: str,
) -> Path:
    """Save final document and walkthrough to cwd, return the document path."""
    w = get_writer()
    (work_dir / "walkthrough.md").write_text(walkthrough, encoding="utf-8")

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
    prompt = load("walkthrough.txt").format(
        task=task,
        iteration_history=history.build_iteration_history(),
    )
    return session.send(prompt)
