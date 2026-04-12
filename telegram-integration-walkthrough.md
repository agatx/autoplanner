# Walkthrough: Remote Messaging Integration for HITL Review

## Executive Summary

This document designs a Telegram-based remote review mode for AutoPlanner, allowing a human reviewer to participate in the draft-review-revise loop from a phone or chat client instead of requiring terminal access. After four iterations, the final design ships a Telegram DM-only integration (v1) built on a `RemoteReviewBridge` that implements the existing `Writer` and `SteeringSource` protocols, with a semantic `MessagingBackend` abstraction that enables future Slack support. The reviewer approved the document at iteration 4 with only minor polish items remaining.

## Evolution Narrative

**Iteration 1 (Draft → Review):** Claude produced a broad initial design covering both Slack and Telegram, with a thin `post_message`/`update_message`/`listen` backend abstraction, a simple `_in_decision_phase` boolean for routing, and ephemeral messaging threads that reset on resume. The human locked three architectural decisions before the Codex review: keep threads ephemeral (d1), bind runs to a single authorized user (d2), and keep the modal boolean routing model rather than a state machine (d3).

Codex's first review was aggressive — 12 items, three critical. The sharpest critiques: the `listen()` abstraction hides deduplication and cursor management that are essential for correctness; the identity/trust model is hand-waved; resume semantics are contradictory (the doc says both "same thread" and "new thread"); and the boolean routing model is too naive for async chat where reviewers reply late or out-of-order. Codex also challenged the raw `post_message` backend as leaking platform differences upward, and called out that polling "only during active waits" contradicts the stated support for mid-phase steering. The reviewer pushed hard for a Telegram-first strategy, noting Slack's webhook requirement makes it a weaker v1 candidate.

**Iteration 2 (Revision → Review):** Claude accepted nearly everything. Major changes: reframed as Telegram-primary with Slack as text-only compatibility mode; replaced the raw message backend with a semantic protocol (`post_decision`, `post_status`, `post_artifact`, `poll_events`); introduced `IncomingEvent` with dedup keys and monotonic cursors; added reviewer identity enforcement with platform-specific user IDs; added startup validation (token, channel access, reviewer identity, handshake); switched to continuous listener thread for steering; defined artifact delivery policy (file attachments per review phase, not per token). The resume contradiction was resolved: new thread explicitly, old thread abandoned. Claude also added a `DecisionView` dataclass so backends render decisions semantically rather than from raw text.

However, Claude kept the `_in_decision_phase` boolean, honoring the human's d3 lock. This created tension — the reviewer had locked the modal boolean, but the Codex review kept pushing for more explicit state management.

Codex's second review was still skeptical on several fronts: Slack v1 is underspecified enough to be non-viable; the boolean routing model remains too weak (the human lock notwithstanding); resume UX doesn't help the reviewer discover the old thread is stale; and the `Writer` contract change (discarding `write()` output) isn't called out as a semantic break. Codex recommended dropping Slack entirely from v1 and persisting minimal session metadata for resume notices.

**Iteration 3 (Revision → Review):** This was the pivotal iteration. Claude made the biggest structural changes: dropped Slack from v1 entirely (Telegram-only); introduced the three-state routing model (`idle`, `decision_open`, `discussion_open`) — effectively superseding the human's d3 lock by adding decision ID correlation and a discussion state while keeping the model minimal; persisted session metadata (chat ID, session ID) to `history.json` for resume notices; added a visible session ID to decision prompts; defined the review-focused writer contract explicitly; specified DM-only trust model where chat ID is the authority boundary; and added a failure model with per-operation retry policies.

Notably, this revision handled the d3 tension gracefully: rather than jumping to a full state machine (which the human rejected), Claude found a middle ground — a three-state model that addresses the real async risks (stale callbacks, messages during author round-trips) without the complexity of a comprehensive state machine. The locked decisions were updated to reflect this evolution.

Codex's third review was much warmer. The remaining issues were narrower: messages during `discussion_open` shouldn't be silently queued as steering (they're probably continued discussion); stale text commands lack decision ID correlation (only callbacks have it); session metadata should be written at handshake time, not `stop()` time; and `validate_connection` mixes validation with side effects (handshake posting).

**Iteration 4 (Revision → LGTM):** Claude tightened the remaining gaps: renamed `discussion_open` to `awaiting_author_reply` and changed its behavior from silent steering-queue to explicit rejection ("Waiting for author response"); separated `validate_connection()` (no visible messages) from `post_handshake()` (session establishment); moved session metadata persistence to immediately after handshake; added `decision_id` field to `IncomingEvent` to make the callback/text correlation asymmetry explicit; defined the artifact hash as SHA-256 of raw file bytes; and added setup prerequisites (bot creation, DM initiation, chat ID discovery) to the CLI section.

Codex approved with five minor items: `post_text()` retry policy for discussion replies vs status, `/options` command documentation, "session" terminology in Telegram context, duplicate delivery tests, and operator-facing chat ID discovery tooling.

## Key Decisions

- **Telegram-only in v1** (emerged iter 2, locked iter 3): Slack's interactive components require a webhook endpoint, making its UX strictly worse. Codex pushed for this aggressively; Claude initially tried to keep both platforms but accepted the scope reduction. The `MessagingBackend` protocol preserves the Slack path for later.

- **Semantic backend protocol over raw message API** (emerged iter 2): The original `post_message(text, blocks)` leaked platform differences. `post_decision(DecisionView)` / `post_artifact(path)` / `poll_events()` lets each backend own its rendering. This was a Codex-driven change that Claude adopted fully.

- **Three-state routing model** (emerged iter 3, replacing human-locked boolean): The human originally locked the modal boolean (d3), but Codex's persistent pushback on async correctness led Claude to introduce `idle` / `decision_open` / `awaiting_author_reply` as a minimal upgrade. This preserved the spirit of simplicity while addressing real-world risks (stale callbacks, messages during author round-trips).

- **DM-only trust boundary** (evolved across iters 1-3): Started as "one human per run" without enforcement, evolved through "reviewer ID filtering" to "DM chat ID is the authority boundary." The simplification came from recognizing that DM-only eliminates multi-user ambiguity entirely.

- **Explicit rejection during author round-trips** (emerged iter 4): Messages during `awaiting_author_reply` are rejected with a visible explanation rather than silently requeued as steering. Codex flagged that silent reinterpretation of likely discussion text was surprising behavior.

- **Session metadata persisted at handshake, not stop** (emerged iter 4): Ensures resume works after crashes. The human locked ephemeral threads (d1) in iteration 1, but this evolved into "ephemeral logical sessions with best-effort stale-session notices" — a pragmatic middle ground.

- **Review-focused writer contract** (emerged iter 2, formalized iter 3): `write()` output is discarded in remote mode. This is a deliberate semantic break from the terminal writer, justified by the medium — chat is not a terminal, and the reviewer's job is decisions, not watching tokens stream.

- **Validation/handshake separation** (emerged iter 4): `validate_connection()` checks prerequisites silently; `post_handshake()` establishes the visible session. Prevents orphaned handshake messages for runs that fail during setup.

## Unresolved Items

1. **Reviewer absence handling**: The process blocks indefinitely if the reviewer never responds. The document leans toward "do nothing" with an optional reminder, but no timeout mechanism is specified.

2. **Inline text vs file attachment threshold**: Draft artifacts are always posted as files. A length-based threshold (inline if short, file if long) was considered but deferred.

3. **Explicit reviewer acknowledgment at startup**: `--require-ack` was proposed but deferred — no mechanism to confirm the reviewer is actually paying attention before burning compute.

4. **`post_text()` retry policy for discussion replies**: Codex flagged that grouping discussion replies with status messages as "best-effort" may leave the reviewer waiting with no explanation if a reply post fails. Not resolved.

5. **`/options` command documentation**: The listener handles it, but the requirements don't document it as a reviewer-facing command.

6. **Chat ID discovery UX**: Manual `getUpdates` or `@userinfobot` is the only documented path. A helper command (`autoplanner telegram get-chat-id`) was suggested but deferred.

7. **Duplicate delivery / idempotency testing**: The design mentions dedup via `update_id`, but the test plan doesn't explicitly cover repeated callback delivery.

## Attribution

**Claude (writer)** produced the initial architecture and drove all revisions. Claude's strongest original contribution was recognizing that the existing `Writer` / `SteeringSource` abstractions could be bridged to messaging with minimal orchestrator changes. Claude also found the three-state routing compromise that satisfied Codex's correctness concerns without the full state machine the reviewer rejected.

**Codex (reviewer)** drove the most consequential design improvements across three review rounds. Codex was responsible for: pushing Telegram-only scope (iters 1-2), demanding the semantic backend protocol (iter 1), exposing the resume semantics contradiction (iter 1), insisting on explicit identity enforcement (iter 1), identifying the `discussion_open` steering-reinterpretation bug (iter 3), and demanding validation/handshake separation (iter 3). Codex's reviews were consistently sharper on operational correctness than on architecture — the structural ideas were mostly Claude's, but the hardening was Codex's.

**Human (decision-maker)** made three early architectural locks: ephemeral threads (d1, upheld with refinement), single-reviewer identity binding (d2, upheld and strengthened), and modal boolean routing (d3, superseded by the three-state model after Codex demonstrated that pure boolean routing was insufficient for async chat). The human's most impactful choice was d1 — keeping threads ephemeral forced a cleaner session model and avoided the complexity of cross-restart thread persistence.