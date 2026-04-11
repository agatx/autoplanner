# Conditional Human Review Phase for AutoPlanner

## Overview

AutoPlanner's draft-review-revise loop is fully autonomous: two AI agents iterate until convergence or max iterations. This works well for straightforward documents, but for tasks involving high-stakes architectural decisions — technology choices, data model designs, security boundaries — the loop can confidently converge on a suboptimal direction that a human would have caught early.

This design introduces an optional **human decision phase** that activates only when the reviewer identifies architectural fork-in-the-road moments. When enabled, the reviewer emits a structured decision trailer on every review. If high-stakes decisions are present, the loop pauses, presents them to the human one at a time, and records the human's resolution as a **locked decision** — a binding constraint carried into every subsequent writer and reviewer prompt. If the reviewer finds no such decisions, the trailer is empty and the loop proceeds as today.

The key insight is that **the reviewer already identifies these decisions** — it just expresses them as review feedback directed at the writer. We give it a structured format to express them, elevate the human's response to first-class run state, and enforce that locked decisions cannot be silently overridden.

## Goals

- Let a human weigh in on consequential architectural decisions without babysitting every iteration.
- Activate only when the reviewer surfaces genuinely high-stakes choices — not on every iteration, not at a fixed cadence.
- Skip cleanly when there are no decisions worth escalating. A run with `--human-review` enabled but no high-stakes decisions should behave identically to a run without it, aside from a small trailer in the reviewer's output.
- **Bind** human decisions: once the human locks a direction, the writer must follow it and the reviewer must not reopen it unless raising an explicit new conflict.
- Work in both TUI and headless modes, with explicit policies for non-interactive environments.
- Persist human decisions as durable run state so resumed sessions, subsequent iterations, and walkthroughs all reflect what the human chose and why.

## Non-Goals

- **Multi-user review workflows.** This is a single-human-in-the-loop feature, not an approval chain.
- **Human review of the document itself.** The human reviews decision points, not the full document. Full-document review is what the walkthrough is for.
- **Guaranteed activation.** If the reviewer never flags high-stakes decisions, the human never sees a prompt. This is correct behavior, not a bug.
- **Custom decision taxonomies.** We don't let users define what counts as "high-stakes." The reviewer's judgment is the filter.
- **Dependent decision modeling.** For v1, the reviewer must collapse coupled choices into a single decision record. Multi-item dependency/conflict metadata is deferred.

## Requirements

### R1: Reviewer emits a structured decision trailer on every enabled review

When `--human-review` is enabled, the reviewer prompt includes an instruction to append a fenced JSON trailer tagged `decisions` at the end of every review. The trailer has a fixed structure:

```
```decisions
{
  "decision_status": "none" | "present",
  "decisions": []
}
```

When the reviewer identifies high-stakes architectural choices:

```
```decisions
{
  "decision_status": "present",
  "decisions": [
    {
      "id": "d1",
      "title": "Cache invalidation strategy",
      "summary": "The document proposes TTL-based expiration but event-driven invalidation would reduce staleness at the cost of coupling to the event bus.",
      "options": [
        {"key": "A", "label": "TTL-based expiration", "pros": "Simple, decoupled", "cons": "Stale reads up to TTL window"},
        {"key": "B", "label": "Event-driven invalidation", "pros": "Near-realtime freshness", "cons": "Coupling to event bus, more failure modes"}
      ],
      "current_choice": "A"
    }
  ]
}
```

When the reviewer wants to challenge a previously locked decision, it emits a conflict record. The conflict uses a new unique ID, references the original via `conflict_with`, and each option declares its `effect` on the original decision:

```
```decisions
{
  "decision_status": "present",
  "decisions": [
    {
      "id": "d1-v2",
      "title": "Cache invalidation strategy (conflict)",
      "summary": "The chosen event-driven approach requires the event bus to guarantee ordering, but R7 now specifies at-most-once delivery.",
      "conflict_with": "d1",
      "options": [
        {"key": "A", "label": "Revert to TTL-based expiration", "effect": "supersede", "pros": "Works with at-most-once", "cons": "Stale reads"},
        {"key": "B", "label": "Require at-least-once delivery on event bus", "effect": "supersede", "pros": "Preserves invalidation choice", "cons": "Changes R7 scope"},
        {"key": "C", "label": "Keep current choice, accept staleness risk", "effect": "keep_original", "pros": "No changes needed", "cons": "Known data consistency gap"}
      ],
      "current_choice": "B"
    }
  ]
}
```

The `effect` field on conflict options is either `"supersede"` (choosing this option supersedes the original decision) or `"keep_original"` (choosing this option reaffirms the original and discards the conflict). The field is only present on options of conflict decisions; non-conflict decision options do not have it. The orchestrator uses this field to determine the state transition — no heuristic or interpretation needed.

Schema invariants enforced by the parser:

- `decision_status: "present"` requires non-empty `decisions` array.
- `current_choice` must match one of the option `key` values.
- `conflict_with`, if present, must reference a decision ID that is currently `active` or `challenged`.
- Every option on a conflict decision must have an `effect` field with value `"supersede"` or `"keep_original"`.
- ID collision rules: if an emitted `id` matches an existing `active` or `proposed` entry and has no `conflict_with` field, treat as a deduplication no-op (skip silently). Any other ID collision (e.g., colliding with a `superseded` entry, or a conflict proposal reusing an existing ID) is a parse error.

Design choices:

- **Always-present trailer.** The `decision_status` field makes "no decisions" an explicit signal, distinguishable from "the reviewer failed to emit the block." A missing trailer when human review is enabled is a parse failure, not a silent skip.
- **No `stakes` field.** The reviewer only includes decisions it considers high-stakes. The prompt instruction defines the threshold; the schema doesn't need a redundant filter field.
- **Coupled decisions must be collapsed.** The prompt instructs the reviewer to merge dependent choices into a single decision record with combined options. Multi-item dependency modeling is deferred.
- **Conflicts use new IDs.** Each proposal gets its own unique ID. The `conflict_with` field links to the original. This avoids key collisions in the decisions map and makes the version chain explicit.
- **Explicit effect per option.** Conflict resolution is deterministic: the orchestrator reads the `effect` field of the chosen option to decide whether to supersede or keep the original. No ambiguity, no second interpretation step.

### R2: Decision extraction: simple parse with policy-driven failure handling

The orchestrator extracts the `decisions` trailer from the review text using a regex match on the fenced block and `json.loads`. Four outcomes:

| Outcome | Meaning | Action |
|---------|---------|--------|
| Trailer parsed, `decision_status: "none"` | Reviewer found no high-stakes decisions | Continue normally |
| Trailer parsed, `decision_status: "present"`, valid decisions | High-stakes decisions detected | Enter decision phase |
| Trailer parsed but fails schema invariants | Malformed content | Treat as parse error |
| Trailer missing or unparseable JSON | Parse failure | Apply `--on-parse-error` policy |

Parse-error handling is policy-driven via `--on-parse-error`, using the same policy model as `--on-decision`:

| Policy | Behavior |
|--------|----------|
| `warn` (default when TTY detected) | Log warning, continue without decision phase. Human can intervene via steering. |
| `fail` (default when no TTY) | Abort the run. A non-interactive environment that enables `--human-review` expects the trailer to work; silent continuation undermines the feature. |

### R3: Decision state machine

Each decision has a lifecycle managed by a simple state machine:

```
proposed  ──[human resolves]──>  active  ──[conflict proposed]──>  challenged
                                                                       │
                                                          [human resolves conflict]
                                                                       │
                                                    ┌──────────────────┼──────────────────┐
                                                    │                                     │
                                          [effect: supersede]                [effect: keep_original]
                                                    │                                     │
                                                    v                                     v
                                               superseded                             active
                                                                               (original restored)
```

States:

- **`proposed`**: The reviewer has surfaced a new decision. Awaiting human input. Blocks convergence.
- **`active`**: The human has resolved it. The `locked_direction` is injected into all subsequent prompts.
- **`challenged`**: A conflict has been raised against this decision (a new `proposed` decision references it via `conflict_with`). The original remains in the active prompt injection set until the conflict is resolved — the human has not yet decided whether to change direction, so the original lock holds.
- **`superseded`**: The conflict was resolved with `effect: "supersede"`. The old decision is removed from the active prompt injection set and retained only in history for audit.

State transitions:

- `proposed -> active`: Human resolves the decision.
- `active -> challenged`: The reviewer emits a new decision with `conflict_with` referencing this ID. The original stays in prompts while the human considers the conflict.
- `challenged -> superseded`: The human resolves the conflict by choosing an option with `effect: "supersede"`. The new proposal transitions `proposed -> active` and the old decision moves to `superseded`.
- `challenged -> active`: The human resolves the conflict by choosing an option with `effect: "keep_original"`. The original reverts to `active`. The conflict proposal is recorded as resolved (with its own `IterationRecord`) but does not become a new active decision.

**Idempotency guarantees for crash/resume safety.** All state transitions are idempotent:

- `propose_decision()` on an already-`proposed` or `active` ID with no `conflict_with` is a no-op (returns False).
- `challenge_decision()` on an already-`challenged` ID is a no-op.
- `lock_decision()` on an already-`active` ID with the same resolution is a no-op.
These guarantees ensure that replaying `_resolve_decisions()` after a crash mid-decision produces the same state without corruption.

**Prompt injection rule:** Only `active` and `challenged` decisions appear in the `## Locked Decisions` block. `challenged` decisions are annotated with `(under review)` so the writer knows the direction may change. `proposed` and `superseded` decisions do not appear.

### R4: Locked decisions are first-class run state

The authoritative source of decision state is the `decisions` map on the `History` object, keyed by decision ID. Each entry stores:

```python
{
    "id": "d1",
    "state": "active",          # proposed | active | challenged | superseded
    "title": "Cache invalidation strategy",
    "resolution": {              # present only when state is active/challenged/superseded
        "chosen_key": "B",
        "chosen_label": "Event-driven invalidation",
        "chosen_effect": null,   # "supersede" | "keep_original" | null (non-conflict decisions)
        "note": "but keep a TTL fallback for cold starts",
        "locked_direction": "Use event-driven invalidation. Note: but keep a TTL fallback for cold starts."
    },
    "conflict_with": null,      # ID of the decision this conflicts with, if any
    "superseded_by": null,      # ID of the decision that superseded this one, if any
}
```

The map stores **one entry per unique decision ID** — this includes both original decisions and conflict proposals, since they have distinct IDs. Superseded decisions remain in the map (for `superseded_by` lookups and audit) but are excluded from prompt injection.

Per-iteration `IterationRecord` entries with `phase: "decision"` are the audit log — they record what was presented and what the human chose at a specific point in time. The `decisions` map is the live state. On resume, `from_directory` loads the `decisions` map directly.

The map is written to disk on every state transition (propose, lock, challenge, supersede).

Locked decisions have three properties:

1. **Durable.** Written to `history.json` immediately upon state change.
2. **Injected into every subsequent prompt.** Both the writer and reviewer receive a `## Locked Decisions` block listing each `active` and `challenged` decision. `challenged` entries are annotated so the writer knows to treat the direction as provisional.
3. **Reopenable only via explicit conflict.** The reviewer prompt instructs: "Do not reopen locked decisions unless you have identified a new conflict or incompatibility that was not visible when the decision was made. If you must challenge a locked decision, emit a new decision record with a unique ID, `conflict_with` referencing the original, and an explanation of the new conflict."

### R5: Unresolved decisions block convergence; decision-driven iterations are separate from the normal budget

A run cannot terminate while `proposed` decisions exist. The `is_done()` function gains two additional checks:

```python
def is_done(review_text: str, iteration: int, max_iterations: int,
            *, has_proposed: bool = False,
            in_decision_pass: bool = False) -> bool:
    if has_proposed:
        w.write_status("[yellow]Unresolved decisions — cannot converge.[/yellow]")
        return False
    if in_decision_pass:
        w.write_status("[dim]Post-decision incorporation pass — continuing.[/dim]")
        return False
    # ... existing LGTM / max-iteration logic
```

**Decision-driven iterations do not count against `max_iterations`.** When decisions are resolved during an iteration, subsequent revise/review cycles needed to incorporate those decisions run outside the normal budget. These extra iterations are governed by a separate safety cap: `MAX_DECISION_PASSES = 3`.

**When `MAX_DECISION_PASSES` is exhausted:** The run terminates with a warning and exit code 2 (distinct from normal completion at exit code 0 and hard abort at exit code 1). Any `proposed` decisions at that point are left in `proposed` state in `history.json` so they can be resolved on resume. The warning message is: `"Decision pass budget exhausted ({MAX_DECISION_PASSES} passes). Stopping with {N} unresolved decision(s). Resume with -c to continue."` This bounds the worst case where the reviewer keeps surfacing new decisions during incorporation passes, while preserving progress for later continuation.

If the normal `max_iterations` is reached with `proposed` decisions still pending, the same termination behavior applies: warning, exit code 2, decisions left as `proposed`.

### R6: Human resolves decisions one at a time with structured input

For v1, decisions are presented sequentially — one decision per prompt.

For each decision, the human **must pick an option by key** (e.g., `B`) or type **`skip`** (shorthand for the `current_choice` key). They may optionally append a note after the key (e.g., `B — but keep a TTL fallback for cold starts`). The resolution is stored as two separate fields:

```json
{
  "decision_id": "d1",
  "title": "Cache invalidation strategy",
  "options_presented": [
    {"key": "A", "label": "TTL-based expiration"},
    {"key": "B", "label": "Event-driven invalidation"}
  ],
  "chosen_key": "B",
  "chosen_label": "Event-driven invalidation",
  "chosen_effect": null,
  "note": "but keep a TTL fallback for cold starts",
  "locked_direction": "Use event-driven invalidation. Note: but keep a TTL fallback for cold starts."
}
```

- **`chosen_key`** and **`chosen_label`**: Always present. The unambiguous architectural choice.
- **`chosen_effect`**: For conflict decisions, the `effect` value from the chosen option (`"supersede"` or `"keep_original"`). `null` for non-conflict decisions. This drives the state transition.
- **`note`**: Optional human-provided context. May be empty string.
- **`locked_direction`**: Derived deterministically: `"Use {chosen_label}."` if no note, or `"Use {chosen_label}. Note: {note}"` if a note is provided. This is the canonical text injected into prompts.

Input validation: if the human enters something that doesn't start with a valid option key or `skip`, re-prompt with an error message. In `accept` policy mode, the `current_choice` key is selected automatically. In `fail` policy mode, the run aborts before prompting.

The `await_decision_input` method on `Writer` accepts `valid_keys: list[str]` where `skip` is always included in the valid set. The method returns `(chosen_key, note)` where `skip` is normalized to the `current_choice` key before return.

### R7: Decision records in history

A new phase type `"decision"` is added to `IterationRecord` with `author: "human"`. The content is a JSON string containing the resolution record from R6. One `IterationRecord` per decision resolved.

These records are the audit log. The `decisions` map on `History` (R4) is the live state. On resume, the `decisions` map is loaded directly from `history.json`; iteration records provide the historical narrative for the walkthrough.

### R8: TUI presentation

In TUI mode, the decision phase:

1. Renders the decision as a status block in the output log: title, summary, options list with pros/cons, and the document's current choice highlighted. For conflict decisions, shows the original locked direction being challenged, the conflict rationale, and each option's effect (`supersede` / `keep_original`).
2. Changes the input placeholder to `"Pick A/B/C or 'skip' [decision 1/N: {title}]"`.
3. Blocks the orchestrator thread on a `threading.Event`. When the user submits input while the orchestrator is in decision mode, `on_input_submitted` validates the input (must start with a valid option key or `skip`), sets the event with the value, or re-prompts on invalid input. A flag `_in_decision_phase` on the app distinguishes the two input modes.
4. If there are previously locked decisions, shows a one-line summary above the new decision: `"Previously locked: d1 — Use event-driven invalidation"`.
5. After all decisions are resolved, restores normal steering input mode.

### R9: Headless mode with explicit resolution policy

Headless mode does not assume stdin is interactive. The `--on-decision` flag specifies the resolution policy:

| Policy | Behavior |
|--------|----------|
| `prompt` (default when TTY detected) | Print decisions to stdout, read response from stdin. Block indefinitely. |
| `accept` | Auto-accept the document's current choices. Log each auto-accepted decision as a warning. |
| `fail` | Abort the run with a non-zero exit code. Decisions are recorded as `proposed` in history. |

TTY detection: if `sys.stdin.isatty()` is true, default to `prompt`. Otherwise, default to `fail`.

### R10: `--human-review` is opt-in and off by default

A new CLI flag `--human-review` / `-H` enables the feature. When absent:
- The reviewer prompt does not include the decision-trailer instruction (zero prompt overhead).
- The orchestrator skips all decision-phase logic and parse-error handling.
- `is_done()` is called without decision-related checks.

### R11: Resume reconstructs decision state, including pending decisions

When resuming a run with `-c`, the `decisions` map is loaded from `history.json` and attached to the `History` object.

**If the map contains `proposed` entries** (e.g., the prior run aborted under `fail` policy, hit `MAX_DECISION_PASSES`, or the process was killed mid-decision), the orchestrator enters the decision phase immediately before the first revise/review call. Pending decisions are presented to the human using the same sequential flow as mid-run decisions. All state transition methods are idempotent (see R3), so replaying a partially-completed decision sequence after a crash produces correct state.

Active and challenged decisions are injected into the `## Locked Decisions` block of the very first revise and review prompts of the resumed session. This uses the same code path as the live run.

### R12: Observability

All decision-phase transitions emit `write_status` messages visible in the TUI log and headless stdout:

- `Decision trailer parsed: {N} high-stakes decision(s) detected` / `Decision trailer parsed: no decisions`
- `Decision trailer parse error — applying {policy} policy` (warning/error depending on policy)
- `Decision skipped (duplicate of active {id})` — deduplication no-op
- `Awaiting human input for decision: {title}`
- `Decision locked: {id} — {locked_direction}`
- `Decision auto-accepted: {id} — {current_choice}` (warning, `accept` policy only)
- `Decision challenged: {original_id} (conflict raised as {new_id})`
- `Decision superseded: {original_id} (replaced by {new_id})`
- `Decision kept: {original_id} (conflict {new_id} resolved with keep_original)`
- `Post-decision incorporation pass {N}/{MAX_DECISION_PASSES}`
- `Decision pass budget exhausted — stopping with {N} unresolved decision(s)`
- `Run aborted: unresolved decisions` (`fail` policy only)
- `Resuming with {N} pending decision(s) from prior run`

Debug-level detail (raw trailer content, parse errors, schema validation failures) goes to `autoplanner-debug.log` via the existing debug module.

## Execution Plan

### Step 1: Add decision state to History

Extend `History` with a `decisions: dict[str, dict]` field (keyed by decision ID). Each value stores the record described in R4. Add methods:
- `propose_decision(decision: dict) -> bool` — adds a `proposed` entry. Returns False (no-op) if the ID already exists as `active` or `proposed` without `conflict_with` (deduplication). If the decision has `conflict_with`, also transitions the referenced decision from `active -> challenged` atomically within the same method — this ensures the challenge and proposal are always consistent.
- `lock_decision(decision_id: str, resolution: dict) -> None` — transitions `proposed -> active`, stores resolution. If `resolution["chosen_effect"]` is `"supersede"` and this decision has `conflict_with`, transitions the referenced decision from `challenged -> superseded` and sets its `superseded_by`. If `"keep_original"`, transitions the referenced decision from `challenged -> active` (restoring it) and marks this conflict as resolved without becoming active itself.
- `active_decisions() -> list[dict]` — returns `active` and `challenged` entries (for prompt injection).
- `has_proposed() -> bool` — returns whether any `proposed` entries exist.
- `pending_decisions() -> list[dict]` — returns `proposed` entries (for resume re-presentation).

All mutating methods are idempotent: calling them with already-transitioned state is a no-op. All mutating methods immediately write `history.json`. Extend `from_directory` to load the `decisions` map. Extend `IterationRecord.phase` to include `"decision"` and `IterationRecord.author` to include `"human"`.

### Step 2: Add decision trailer parsing

A new function `extract_decisions(review_text: str) -> tuple[str, list[dict]]` in `orchestrator.py`. Returns `("none", [])`, `("present", [...])`, or `("parse_error", [])`. Uses regex to find the ` ```decisions ` fence and `json.loads` to parse. Validates schema invariants (including `effect` fields on conflict options and the ID collision rules from R1). Strips the trailer from the review text before storing in history.

### Step 3: Extend the reviewer prompt

A new prompt fragment `prompts/decisions_instruction.txt` with the trailer schema (including `effect` field for conflict options), threshold definition, coupled-decision collapsing rule, conflict escalation protocol, and deduplication guidance. Agent review functions gain a `human_review: bool` parameter; when true, the fragment is appended.

A `locked_decisions_block(decisions: list[dict]) -> str` function in `prompts.py` formats active/challenged decisions into the `## Locked Decisions` prompt block. Challenged decisions are annotated with `(under review — conflict pending)`. This function is called by `claude_agent.review()`, `claude_agent.revise()`, and `codex_agent.review()`, receiving `history.active_decisions()` as input. The parameter type is `list[dict]`; formatting to string happens inside this function.

### Step 4: Add decision presentation to the Writer protocol

Two new methods on `Writer`:
- `present_decision(decision: dict, prior_decisions: list[dict]) -> None` — renders one decision to the output, with prior locked decisions shown for context. For conflict decisions, shows each option's `effect`.
- `await_decision_input(valid_keys: list[str]) -> tuple[str, str]` — blocks until the human responds with a valid key or `skip`. `skip` is always in `valid_keys`. Returns `(chosen_key, note)` where `skip` is normalized to the `current_choice` key. Re-prompts on invalid input.

`TuiWriter`: renders in the output log, sets `_in_decision_phase` flag, validates input in `on_input_submitted`, blocks on a `threading.Event`. `TerminalWriter`: prints formatted text, reads from `sys.stdin.readline()` with validation loop (only called when policy is `prompt`).

### Step 5: Add the decision phase to `_run_loop`

The decision phase is factored into a helper `_resolve_decisions` called from two places: (a) after each review in the main loop, and (b) at the top of `_run_loop` when resuming with pending decisions.

```python
def _resolve_decisions(
    decisions: list[dict], history: History, on_decision_policy: str, w: Writer,
    iteration: int,
) -> bool:
    """Present and resolve decisions. Returns True if any were resolved."""
    resolved_any = False
    for d in decisions:
        if not history.propose_decision(d):  # handles challenge + dedup internally
            w.write_status(f"  Decision skipped (duplicate of active {d['id']})")
            continue
        w.present_decision(d, history.active_decisions())
        chosen_key, note = _get_decision_input(on_decision_policy, d, w)
        resolution = _build_resolution(chosen_key, note, d)
        history.lock_decision(d["id"], resolution)
        history.add(IterationRecord(
            iteration=iteration, phase="decision",
            author="human", content=json.dumps(resolution),
        ))
        w.write_status(f"  Decision locked: {d['id']} — {resolution['locked_direction']}")
        resolved_any = True
    return resolved_any
```

Note: `propose_decision()` handles the `active -> challenged` transition internally when `conflict_with` is set. This ensures the challenge and proposal are atomic — the original is never left `challenged` without a corresponding `proposed` conflict.

In the main loop, after the review is recorded and before `is_done()`:

```python
force_continue = False
if human_review:
    status, raw_decisions = extract_decisions(review_text)
    if status == "parse_error":
        _handle_parse_error(on_parse_error_policy, w)
    elif status == "present" and raw_decisions:
        force_continue = _resolve_decisions(
            raw_decisions, history, on_decision_policy, w, iteration)

if is_done(review_text, iteration, max_iterations,
           has_proposed=history.has_proposed(),
           in_decision_pass=force_continue):
    break
```

Decision-driven incorporation passes do not count against `max_iterations`. They are tracked by a separate counter capped at `MAX_DECISION_PASSES = 3`. When the cap is hit, the run terminates with a warning message and exit code 2. Any `proposed` decisions remain in that state for resume.

At the top of `_run_loop`, before the main `for` loop:

```python
if human_review and history.has_proposed():
    pending = history.pending_decisions()
    w.write_status(f"  Resuming with {len(pending)} pending decision(s)")
    _resolve_decisions(pending, history, on_decision_policy, w, start_iteration)
```

### Step 6: Wire up CLI flags

Add to `main.py`:
- `--human-review` / `-H` (bool, default False)
- `--on-decision` (choice of `prompt`/`accept`/`fail`, default auto-detected by TTY)
- `--on-parse-error` (choice of `warn`/`fail`, default auto-detected by TTY)

Pass through to `orchestrator.run()` and `orchestrator.resume()`.

### Step 7: Update walkthrough prompt

Add a note to `walkthrough.txt` that `decision` phase records represent human-locked architectural directions. Instruct the walkthrough to narrate them as deliberate choices: what was at stake, what the human chose, and how the document evolved to incorporate the choice. Superseded decisions should be narrated as "initially chose X, later revised to Y because of Z."

## Alternatives Considered

### Alternative 1: Dedicated "decision analyst" LLM role

A third AI agent that reads the review and decides whether to escalate to the human.

**Rejected.** Adding a third LLM call on every iteration doubles the cost of the review phase. The reviewer is already the right agent to identify decision points — it's already analyzing the document for exactly these issues. Asking it to tag its own findings with structured metadata is simpler and cheaper than having a second model re-analyze the first model's output.

### Alternative 2: Fixed decision phase at iteration N

Insert the human review at a fixed iteration (e.g., always after iteration 2).

**Rejected.** The timing should depend on when decisions are identified, not on a fixed schedule. Some tasks surface architectural choices in the first draft; others don't have any. A fixed schedule either interrupts too early (before decisions crystallize) or too late (after the loop has already committed). The reviewer-driven trigger is both simpler and more correct.

### Alternative 3: Human reviews every iteration

Present the full review to the human every iteration, letting them optionally provide input.

**Rejected.** This defeats the purpose of an autonomous loop. The whole point is that the human only intervenes on high-stakes decisions. Showing every review creates notification fatigue and slows the loop to human speed on every turn.

### Alternative 4: Decision detection via a separate parsing prompt

Send the review text to a cheap/fast model (e.g., Haiku) to extract decisions.

**Rejected.** This adds latency, cost, and a failure mode for marginal benefit. The reviewer can emit structured JSON directly. If the JSON is occasionally malformed, we handle it via the parse-error policy. A separate parsing call only makes sense if we couldn't modify the reviewer's prompt, but we control it entirely.

### Alternative 5: Steering-only approach (no structured decisions)

Instead of structured decision presentation, just pause and say "the reviewer flagged important decisions — type your guidance."

**Rejected for default UX, but the note field preserves the escape hatch.** Structured options with trade-offs are far more useful to the human than a wall of review text. The required option key ensures a concrete choice; the optional note lets the human add nuance.

### Alternative 6: Async notification model (don't block the loop)

Continue the loop but flag decisions for later human review; apply human input retroactively.

**Rejected.** If the loop continues past a high-stakes decision, the writer may build 2-3 iterations of work on top of the wrong choice. Retroactive correction wastes those iterations and confuses the conversation context. Blocking is the right behavior for genuinely high-stakes decisions.

### Alternative 7: Emit decisions in the review prompt at all times, gate only in the orchestrator

Always include the decisions instruction in the reviewer prompt, but only act on the decisions block when `--human-review` is enabled.

**Rejected.** The decisions instruction adds ~200 tokens to the reviewer prompt and changes the reviewer's output format. When human review is disabled, this is pure overhead. Conditional prompt inclusion is cleaner.

### Alternative 8: Bulk decision resolution (all decisions in one prompt)

Present all decisions at once and let the human respond in one message.

**Rejected for v1.** Sequential one-at-a-time presentation is unambiguous, simpler to implement, and gives the human time to think about each choice. Bulk mode can be added later if sequential feels slow.

### Alternative 9: Timeout-based auto-accept in headless mode

A `--human-review-timeout` flag that auto-accepts after N seconds.

**Rejected.** The explicit `--on-decision` policy (`prompt`/`accept`/`fail`) is clearer: the operator chooses the behavior at invocation time rather than hoping they notice within a time window.

### Alternative 10: Free-text-only resolution (no required option key)

Allow the human to type anything as a locked direction without picking a concrete option.

**Rejected.** Ambiguous free text like "lean toward B if latency is acceptable" becomes a binding constraint that neither the writer nor a future human can unambiguously interpret. Requiring an option key ensures every locked decision has a concrete, machine-parseable choice. The optional note field preserves human nuance without sacrificing clarity.

### Alternative 11: Single flat list for decision state

Store all decisions (active, superseded, proposed) in one list and filter by state at query time.

**Rejected in favor of a keyed map.** A map keyed by decision ID makes lookups O(1) for conflict-with validation, supersede operations, and active-set queries.

### Alternative 12: Single ID with version chain

Use the same decision ID for both the original and conflict proposals, maintaining a version array within the entry.

**Rejected.** Separate IDs per proposal are simpler to implement, avoid mutation of existing map entries during the conflict presentation flow, and make the audit log unambiguous — each `IterationRecord` references exactly one decision ID. The `conflict_with` / `superseded_by` links provide the version chain without overloading a single ID.

### Alternative 13: Infer conflict outcome from option choice heuristically

Instead of an explicit `effect` field, determine whether a conflict option supersedes or keeps the original based on the option label or position.

**Rejected.** Heuristic interpretation is fragile — "keep current choice" in position C is not reliably distinguishable from "new approach C" without understanding the semantics. An explicit `effect` field costs ~20 tokens per option in the reviewer's output and makes the state transition deterministic.

## Open Questions

1. **Max decisions per iteration.** Should we cap the number of decisions the reviewer can surface in a single review? A reviewer that emits 8 decisions is probably being too granular. Leaning toward a prompt instruction ("surface at most 3 high-stakes decisions per review, prioritized by reversibility cost") rather than a hard orchestrator-level cap. A hard cap risks silently dropping valid decisions.

2. **Conflict chain depth.** The state machine supports arbitrary depth (a conflict decision can itself be conflicted). In practice more than one level suggests the reviewer is thrashing. Leaning toward no cap for v1 — the prompt instruction to only raise genuinely new conflicts should be sufficient, and `MAX_DECISION_PASSES` bounds the total iteration cost.