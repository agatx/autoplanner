from __future__ import annotations

import json
import threading
from enum import Enum
from pathlib import Path

from autoplanner.agents import claude_agent, codex_agent
from autoplanner.agents.session import ClaudeSession, CodexSession
from autoplanner.decisions import extract_decisions, strip_decisions_trailer
from autoplanner.history import History, IterationRecord, make_run_id, make_output_name, find_run_dir
from autoplanner.output import get_writer
from autoplanner.prompts import load
from autoplanner.steering import SteeringSource, StdinSteering

AUTOPLANNER_DIR = ".autoplanner"
MAX_DECISION_PASSES = 3


def _close_sessions(sessions: list) -> None:
    for s in sessions:
        try:
            s.close()
        except Exception:
            pass


def _make_sessions(
    claude_model: str, claude_effort: str, codex_model: str, codex_effort: str,
) -> tuple[ClaudeSession, ClaudeSession, CodexSession, ClaudeSession]:
    return (
        ClaudeSession(model=claude_model, effort=claude_effort, label="claude"),
        ClaudeSession(model=claude_model, effort=claude_effort, label="claude-review"),
        CodexSession(model=codex_model, effort=codex_effort, label="codex"),
        ClaudeSession(model=claude_model, effort=claude_effort, label="claude-walkthrough"),
    )


class Reviewer(str, Enum):
    AUTO = "auto"
    CODEX = "codex"
    CLAUDE = "claude"


def is_done(
    review_text: str, iteration: int, max_iterations: int,
    *, has_proposed: bool = False, in_decision_pass: bool = False,
) -> bool:
    w = get_writer()
    if has_proposed:
        w.write_status("[yellow]Unresolved decisions \u2014 cannot converge.[/yellow]")
        return False
    if in_decision_pass:
        w.write_status("[dim]Post-decision incorporation pass \u2014 continuing.[/dim]")
        return False
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
    human_review: bool = False,
    locked_decisions: list[dict] | None = None,
) -> tuple[str, str]:
    w = get_writer()
    if active_reviewer == Reviewer.CODEX:
        try:
            w.write_status("  Reviewing with Codex...")
            text = codex_agent.review(
                codex_session, document, task, iteration,
                max_iterations=max_iterations, steering=steering,
                human_review=human_review, locked_decisions=locked_decisions,
            )
            return text, "codex"
        except RuntimeError as e:
            w.write_status(f"  [yellow]Codex failed ({e}), falling back to Claude...[/yellow]")

    w.write_status("  Reviewing with Claude...")
    text = claude_agent.review(
        claude_review_session, document, task, iteration,
        max_iterations=max_iterations, steering=steering,
        human_review=human_review, locked_decisions=locked_decisions,
    )
    return text, "claude"


def _build_resolution(chosen_key: str, note: str, decision: dict) -> dict:
    chosen_option = next(o for o in decision["options"] if o["key"] == chosen_key)
    locked_direction = f"Use {chosen_option['label']}."
    if note:
        locked_direction += f" Note: {note}"
    return {
        "decision_id": decision["id"],
        "title": decision["title"],
        "options_presented": [{"key": o["key"], "label": o["label"]} for o in decision["options"]],
        "chosen_key": chosen_key,
        "chosen_label": chosen_option["label"],
        "chosen_effect": chosen_option.get("effect"),
        "note": note,
        "locked_direction": locked_direction,
    }


def _handle_parse_error(policy: str, w) -> None:
    if policy == "fail":
        w.write_status("[red]Decision trailer parse error \u2014 aborting (--on-parse-error=fail)[/red]")
        raise SystemExit(1)
    w.write_status(
        "[yellow]Decision trailer parse error \u2014 continuing without decisions "
        "(--on-parse-error=warn)[/yellow]"
    )


def _resolve_decisions(
    decisions: list[dict],
    history: History,
    on_decision_policy: str,
    w,
    iteration: int,
) -> bool:
    """Present and resolve proposed decisions.  Returns True if any were resolved."""
    resolved_any = False
    for d in decisions:
        if not history.propose_decision(d):
            w.write_status(f"  Decision skipped (duplicate of active {d['id']})")
            continue

        if on_decision_policy == "fail":
            w.write_status("[red]Run aborted: unresolved decisions[/red]")
            raise SystemExit(1)

        prior = history.active_decisions()
        w.present_decision(d, prior)
        w.write_status(f"  Awaiting human input for decision: {d['title']}")

        if on_decision_policy == "accept":
            chosen_key = d["current_choice"]
            note = ""
            w.write_status(f"  [yellow]Decision auto-accepted: {d['id']} \u2014 {chosen_key}[/yellow]")
        else:  # "prompt"
            valid_keys = [opt["key"] for opt in d["options"]]
            prompt_text = (
                f"Pick {'/'.join(valid_keys)} or 'skip' "
                f"[decision: {d['title']}]"
            )
            chosen_key, note = w.await_decision_input(valid_keys + ["skip"], prompt_text)
            if chosen_key == "skip":
                chosen_key = d["current_choice"]

        resolution = _build_resolution(chosen_key, note, d)
        history.lock_decision(d["id"], resolution)
        history.add(IterationRecord(
            iteration=iteration, phase="decision",
            author="human", content=json.dumps(resolution),
        ))
        w.write_status(f"  Decision locked: {d['id']} \u2014 {resolution['locked_direction']}")

        if d.get("conflict_with"):
            if resolution.get("chosen_effect") == "supersede":
                w.write_status(
                    f"  Decision superseded: {d['conflict_with']} (replaced by {d['id']})"
                )
            else:
                w.write_status(
                    f"  Decision kept: {d['conflict_with']} "
                    f"(conflict {d['id']} resolved with keep_original)"
                )

        resolved_any = True
    return resolved_any


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
    human_review: bool = False,
    on_decision_policy: str = "prompt",
    on_parse_error_policy: str = "warn",
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

    writer_session, claude_review_session, codex_session, walkthrough_session = \
        _make_sessions(claude_model, claude_effort, codex_model, codex_effort)

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
            human_review=human_review,
            on_decision_policy=on_decision_policy,
            on_parse_error_policy=on_parse_error_policy,
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
    human_review: bool = False,
    on_decision_policy: str = "prompt",
    on_parse_error_policy: str = "warn",
) -> Path:
    """Resume a previous run from where it left off."""
    cwd = Path.cwd()
    base_dir = cwd / AUTOPLANNER_DIR
    w = get_writer()

    work_dir = find_run_dir(base_dir, run_id)
    history = History.from_directory(work_dir, lock=True)
    w.write_status(f"[dim]Resuming run: {history.run_id}[/dim]")

    last_document, last_review = history.last_document_and_review()
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

    if start_iteration > max_iterations:
        raise RuntimeError(
            f"Cannot resume run {history.run_id}: next iteration would be "
            f"{start_iteration}, but max_iterations is {max_iterations}. "
            "Increase --max-iter to continue this run."
        )

    w.write_status(
        f"[dim]Loaded {len(history.records)} records, "
        f"resuming from iteration {start_iteration}[/dim]"
    )

    writer_session, claude_review_session, codex_session, walkthrough_session = \
        _make_sessions(claude_model, claude_effort, codex_model, codex_effort)

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
            human_review=human_review,
            on_decision_policy=on_decision_policy,
            on_parse_error_policy=on_parse_error_policy,
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

    document, _ = history.last_document_and_review()

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
    human_review: bool = False,
    on_decision_policy: str = "prompt",
    on_parse_error_policy: str = "warn",
) -> Path:
    w = get_writer()
    steering.start()

    w.write_status(f"[dim]Run: {history.run_id}[/dim]")
    w.write_status(f"[dim]Work dir: {history.work_dir}[/dim]")
    w.write_status(f"[dim]Writer: {writer_session.model} (effort: {writer_session.effort})[/dim]")
    w.write_status(f"[dim]Codex:  {codex_session.model} (effort: {codex_session.effort})[/dim]")
    if human_review:
        w.write_status(f"[dim]Human review: enabled (on-decision={on_decision_policy})[/dim]")

    active_reviewer = _resolve_reviewer(reviewer)
    w.write_status(f"[dim]Reviewer: {active_reviewer.value}[/dim]")

    document = ""
    review_text = initial_review

    # --- Resume: resolve any pending decisions from prior run ---
    if human_review and history.has_proposed():
        pending = history.pending_decisions()
        w.write_status(f"  Resuming with {len(pending)} pending decision(s) from prior run")
        _resolve_decisions(pending, history, on_decision_policy, w, start_iteration)

    # Decision-pass tracking
    decision_passes_used = 0
    force_decision_continue = False

    iteration = start_iteration
    while iteration <= max_iterations or force_decision_continue:
        # Check decision-pass budget when extending beyond max_iterations
        if force_decision_continue and iteration > max_iterations:
            decision_passes_used += 1
            if decision_passes_used > MAX_DECISION_PASSES:
                n_pending = sum(1 for d in history.decisions.values() if d["state"] == "proposed")
                w.write_status(
                    f"[yellow]Decision pass budget exhausted ({MAX_DECISION_PASSES} passes). "
                    f"Stopping with {n_pending} unresolved decision(s). "
                    f"Resume with -c to continue.[/yellow]"
                )
                history.save_json()
                raise SystemExit(2)
            w.write_status(
                f"  Post-decision incorporation pass {decision_passes_used}/{MAX_DECISION_PASSES}"
            )
            force_decision_continue = False

        locked = history.active_decisions() if human_review else None

        # --- Pre-phase steering ---
        user_steering = steering.drain()
        if user_steering:
            w.write_status(f"  [bold magenta]Steering applied:[/bold magenta] {user_steering}")

        # --- Draft or Revise ---
        if resume_skip_write and iteration == start_iteration:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Resuming from prior draft...")
            document = initial_document
            skip_history_save = True
        elif iteration == 1 and initial_document is not None:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Using ingested document...")
            document = initial_document
            phase = "draft"
            skip_history_save = False
        elif iteration == start_iteration and initial_document is not None and start_iteration > 1:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Revising with Claude (resumed)...")
            document = claude_agent.revise(
                writer_session, initial_document, review_text,
                iteration=iteration, max_iterations=max_iterations,
                steering=user_steering, locked_decisions=locked,
            )
            phase = "revision"
            skip_history_save = False
        elif iteration == 1:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Drafting with Claude...")
            document = claude_agent.draft(writer_session, task, steering=user_steering)
            phase = "draft"
            skip_history_save = False
        else:
            w.write_status(f"\n[bold cyan]Iteration {iteration}:[/bold cyan] Revising with Claude...")
            document = claude_agent.revise(
                writer_session, document, review_text,
                iteration=iteration, max_iterations=max_iterations,
                steering=user_steering, locked_decisions=locked,
            )
            phase = "revision"
            skip_history_save = False

        # --- Immediate correction if steering arrived during draft/revise ---
        mid_steering = steering.drain()
        if mid_steering:
            w.write_status(f"  [bold magenta]Mid-phase steering \u2014 applying correction:[/bold magenta] {mid_steering}")
            document = claude_agent.correct(writer_session, mid_steering)
            phase = "revision"
            skip_history_save = False

        if not skip_history_save:
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
            human_review=human_review,
            locked_decisions=locked,
        )

        # --- Immediate correction if steering arrived during review ---
        mid_steering = steering.drain()
        if mid_steering:
            w.write_status(f"  [bold magenta]Mid-review steering \u2014 applying correction:[/bold magenta] {mid_steering}")
            document = claude_agent.correct(writer_session, mid_steering)
            history.add(IterationRecord(
                iteration=iteration,
                phase="revision",
                author="claude",
                content=document,
            ))
            w.write_status("  Re-reviewing corrected document...")
            review_text, review_author = _do_review(
                active_reviewer, codex_session, claude_review_session,
                document, task, iteration,
                max_iterations=max_iterations,
                human_review=human_review,
                locked_decisions=locked,
            )

        # --- Decision extraction (before storing review) ---
        force_decision_continue = False
        if human_review:
            status, raw_decisions = extract_decisions(review_text, history.decisions)
            if status == "parse_error":
                _handle_parse_error(on_parse_error_policy, w)
            elif status == "present" and raw_decisions:
                w.write_status(
                    f"  Decision trailer parsed: {len(raw_decisions)} high-stakes decision(s) detected"
                )
                force_decision_continue = _resolve_decisions(
                    raw_decisions, history, on_decision_policy, w, iteration,
                )
            elif status == "none":
                w.write_status("  Decision trailer parsed: no decisions")
            # Strip trailer before storing
            review_text = strip_decisions_trailer(review_text)

        history.add(IterationRecord(
            iteration=iteration,
            phase="review",
            author=review_author,
            content=review_text,
        ))
        w.write_status(f"  Review received from {review_author} ({len(review_text)} chars)")

        if is_done(
            review_text, iteration, max_iterations,
            has_proposed=history.has_proposed() if human_review else False,
            in_decision_pass=force_decision_continue,
        ):
            break

        iteration += 1

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
