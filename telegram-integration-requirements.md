# Remote Messaging Integration for Human-in-the-Loop Review

## Overview

AutoPlanner's human-in-the-loop (HITL) review currently requires a human at the terminal — either interacting via the Textual TUI or reading from stdin in headless mode. This design extends the existing `SteeringSource` and `Writer` abstractions to support review and decision resolution through Telegram, allowing a reviewer to participate from a phone or chat client without terminal access.

The core insight is that the existing architecture already separates I/O concerns cleanly: `Writer` handles all output, `SteeringSource` handles all input, and `await_decision_input` provides the blocking synchronization point for decisions. A messaging integration implements these same protocols, bridging them to a remote chat.

The initial implementation targets Telegram DMs as the sole platform. Telegram's inline keyboards and polling-based callback queries provide a strong mobile review experience without requiring public infrastructure. Slack is deferred to a later phase: its interactive components require a webhook endpoint (conflicting with the no-public-endpoint constraint), and its text-only fallback produces a materially worse experience. The `MessagingBackend` protocol is designed so that adding Slack (or other chat platforms) later requires implementing six semantic methods with no changes to the bridge or orchestrator.

## Goals

- Allow a human reviewer to receive document output, review decisions, ask questions, and provide steering entirely through Telegram — no terminal required.
- Preserve all existing HITL capabilities: multi-option decisions, `/skip`, `/custom`, author discussion (multi-turn Q&A), conflict resolution, and mid-phase steering.
- Support resume (`-c`) — pending decisions re-presented in a new chat session on restart, with a notice posted to the old session if reachable.
- Keep the integration loosely coupled: the orchestrator should not know or care whether its reviewer is local or remote.
- Enforce single-reviewer identity: each run is bound to one authorized Telegram user via DM, and the chat ID itself is the authority boundary.

## Non-Goals

- Multi-reviewer workflows (voting, approval chains). One human per run.
- Real-time collaborative editing of the document itself through messaging.
- Replacing the TUI or headless modes — this is an additional mode, not a replacement.
- Push notifications or mobile-native rich UX beyond what Telegram's message formatting supports.
- Bot management UI, OAuth flows, or team-wide deployment tooling.
- Slack or other platforms in v1. The backend protocol supports future platforms, but only Telegram ships initially.
- Full conversation continuity across resume. Resume recovers decision state into a fresh session; it does not replay prior chat history.
- Auto-discovery of Telegram chat IDs. The operator must obtain the DM chat ID manually in v1.

## Requirements

### Functional

1. **Decision presentation**: Format decisions as structured messages with option labels, pros/cons, current-choice indicator, and a visible `decision_id` tag. Use Telegram inline keyboards for option buttons; each button carries callback data in the format `decision_id:key`.

2. **Decision input**: Accept slash-command replies (`/A`, `/skip`, `/custom <text>`) and bare-text questions. Normalize Telegram callback queries and text messages into a common `IncomingEvent` before routing. Callback queries carry an embedded `decision_id` and are correlated to the active decision — stale callbacks (referencing an already-resolved decision) are rejected with a visible "decision already resolved" reply. Text commands (`/A`, `/skip`, `/custom`) do not carry a decision ID; they are accepted only while a decision is currently open in the active session. Late text replies to a historical decision will apply to whatever decision is currently open, not the historical one. This is a known limitation of text-mode input; inline keyboards are the recommended interaction path for unambiguous decision correlation.

3. **Author discussion**: When the reviewer asks a question (bare text, no slash prefix) while a decision is open, relay it to `claude_agent.discuss()` and post the response back to the chat. While the author response is in flight, the bridge is in an `awaiting_author_reply` state and rejects further messages with a visible reply: "Waiting for author response — send your next message after the reply." After posting the author's answer, the bridge re-enters the decision-open state, and the reviewer can send another question or a terminal command. Multi-turn discussion within a single decision is supported; the loop exits only on a choice command (`/A`, `/skip`, `/custom`). Questions sent outside of a decision phase are treated as steering, not discussion.

4. **Steering**: Accept mid-run messages that are not part of a decision flow. These are queued by the continuously running listener thread and applied at the orchestrator's existing phase-boundary drain points (pre-phase, mid-phase, pre-review). Steering is not applied mid-stream during agent execution — it is collected opportunistically and applied at the next drain. When multiple steering messages accumulate between drains, they are concatenated in arrival order and delivered as a single steering block.

5. **Status updates**: Post phase transitions (drafting, reviewing, iteration N of M) and decision lock confirmations to the chat.

6. **Artifact delivery**: Post the current draft as a file attachment at the start of a review phase only if the document content has changed since the last posted artifact (tracked via SHA-256 hash of raw file bytes). Files are named `draft-iterNN.md` (e.g., `draft-iter03.md`) so the reviewer can identify which version is current. Post the final document as `final.md` on run completion. Walkthrough summaries are posted as `walkthrough.md`.

7. **Resume**: On `autoplanner -c last --remote telegram ...`, the bridge reads persisted session metadata (chat ID, session ID) and attempts to post a "Session `<old_id>` has ended — a new session is starting" notice to the old chat. It then posts a new handshake message, establishing a fresh session context. Pending decisions from `history.json` are re-presented. If the old session notice fails (bot blocked, chat deleted), resume proceeds silently.

8. **Reviewer identity enforcement**: In Telegram DM mode, the chat ID is the authority boundary — the bot's DM partner is the only possible sender, so no separate reviewer ID configuration is needed. Updates received from any other chat ID (e.g., if the bot is added to a group) are discarded and logged at debug level with the source chat ID, to aid diagnosis of misconfiguration.

### Non-Functional

9. **Latency tolerance**: The orchestrator already blocks on `await_decision_input`. Remote latency (seconds to hours) is acceptable — the process simply waits.

10. **Crash recovery**: All decision state is already persisted in `history.json`. Session metadata is written immediately after the handshake succeeds (not deferred to `stop()`), so it is available for resume even after an unclean exit. If the autoplanner process dies, `autoplanner -c` reconstructs state and re-posts pending decisions to a new session.

11. **Rate limiting**: Agent output is buffered and discarded — only status updates, decisions, discussion replies, and artifacts are posted to the chat.

12. **Startup validation**: Before entering the orchestration loop, the bridge runs a two-step startup: (a) `validate_connection()` verifies best-effort prerequisites — bot token is valid (`getMe`) and bot can access the target chat; (b) `post_handshake()` posts the session startup message and persists session metadata. This separation ensures that if the handshake post succeeds but later setup fails, the reviewer at least sees a message explaining the run. Fail fast on any prerequisite failure.

13. **Failure model**: Retry behavior is defined per operation class:
    - **`poll_events()`**: Long-poll timeouts are expected and not errors. Transient network failures (connection reset, DNS timeout) retry indefinitely with capped backoff (max 30s) while the run remains alive. The listener thread continues polling as long as `_running` is true.
    - **`post_decision()` / `post_artifact()`**: Bounded retries (3 attempts: 5s, 15s, 30s). If retries are exhausted, the bridge posts a degraded-mode notice to the chat (if reachable) and raises an error to the orchestrator, which aborts the run.
    - **`post_status()` / `post_text()`**: Best-effort — bounded retries (3 attempts), then log and swallow on exhaustion. Status messages are informational, not load-bearing.
    - **All operations**: The `Retry-After` header is respected for 429 responses. Permanent errors (401, 403, 400) are raised immediately without retry.
    - If the Telegram API becomes persistently unreachable, the run aborts with a clear local error message directing the user to resume with `-c`.

## Design

### Architecture

The integration introduces two new components that implement existing protocols:

```
┌─────────────────────────────────────────────────┐
│                  orchestrator                    │
│                                                  │
│  steering.drain() ──┐    ┌── w.present_decision()│
│                     │    │   w.await_decision_input()
│                     │    │   w.write / w.write_status()
└─────────────────────┼────┼───────────────────────┘
                      │    │
              ┌───────┴────┴───────┐
              │  RemoteReviewBridge │  (new)
              │                    │
              │  implements:       │
              │   Writer protocol  │
              │   SteeringSource   │
              └────────┬───────────┘
                       │
              ┌────────┴───────────┐
              │  MessagingBackend   │  (protocol)
              │                    │
              └── TelegramBackend  │  (v1)
              └────────────────────┘
```

**`RemoteReviewBridge`** is a single object that implements both `Writer` and `SteeringSource`. This is deliberate: the bridge owns the session state, the pending-input event, and the message queue. Splitting it into two objects that share state would add complexity without benefit. The bridge implements a **review-focused writer** contract: `write()` and `write_thinking()` buffer and discard agent output rather than forwarding it to the reviewer. This is a deliberate semantic change from the terminal writer, where `write()` output is visible. Remote mode optimizes for actionable reviewer feed (decisions, status, artifacts, discussion replies) over streaming observability. No existing orchestrator flow depends on `write()` visibility for correctness — it is purely observational.

**`MessagingBackend`** is a protocol abstracting platform-specific API calls at the semantic level — decisions, status, artifacts, events — rather than raw message posting. Only `TelegramBackend` ships in v1. The protocol is designed so that adding a Slack or other backend later requires implementing six methods with no changes to the bridge.

### Event Model

Inbound messages from Telegram are normalized into a common event type before the bridge processes them:

```python
@dataclass
class IncomingEvent:
    text: str               # normalized text (e.g. callback "A" → "/A")
    user_id: str            # Telegram from.id
    event_id: str           # dedup key (update_id)
    timestamp: float        # message timestamp for staleness checks
    source_type: str        # "text", "callback", "edit"
    decision_id: str | None # extracted from callback data; None for text messages
```

Telegram callback queries (button presses) are normalized to their text equivalents before reaching the bridge: a callback with data `"d1:A"` becomes `IncomingEvent(text="/A", source_type="callback", decision_id="d1", ...)`. Text messages always have `decision_id=None` — text commands carry no embedded correlation. Edited messages are ignored (`source_type="edit"` is filtered out). This preserves command vocabulary compatibility with `_parse_decision_input` while keeping platform-specific event handling in the backend.

The backend tracks a monotonic cursor internally (`update_id` offset) and never yields an event it has already yielded. On process startup, the backend initializes its offset so that all pre-session updates are ignored. The exact Telegram API mechanism for cursor initialization is a backend implementation detail (e.g., consuming pending updates via `getUpdates` and tracking from the returned offset onward). On process restart, the cursor resets — this is acceptable because the bridge starts a fresh session.

### MessagingBackend Protocol

```python
class MessagingBackend(Protocol):
    def validate_connection(self) -> None:
        """Verify bot token and chat access.
        Best-effort prerequisites — does not prove reply observability.
        Raise on failure — called once at startup before orchestration.
        Must not post any visible messages."""
        ...

    def post_handshake(self, task_summary: str, max_iterations: int,
                       session_id: str) -> str:
        """Post a session startup message to the chat.
        Returns message ID. Called once after validate_connection()."""
        ...

    def post_decision(self, decision: DecisionView) -> str:
        """Post a formatted decision prompt with inline keyboard.
        Returns message ID."""
        ...

    def post_status(self, text: str) -> str:
        """Post a status update. Returns message ID."""
        ...

    def post_artifact(self, path: Path, title: str) -> None:
        """Upload a document or walkthrough as a file attachment."""
        ...

    def post_text(self, text: str) -> str:
        """Post a plain text message (author discussion replies, etc.)."""
        ...

    def poll_events(self) -> list[IncomingEvent]:
        """Return new events since last poll. Non-blocking; returns [] if none.
        Backend manages its own cursor and deduplication.
        Long-poll timeouts are expected, not errors.
        Transient network failures retry internally with capped backoff."""
        ...
```

`DecisionView` is a dataclass containing the decision's `id`, `title`, `summary`, options (with pros/cons), `current_choice`, prior locked decisions, and conflict metadata. The Telegram backend maps this to inline keyboards for option buttons and formatted text for the decision body. Each decision prompt includes a visible session ID (e.g., `[session abc123]`) so the reviewer can identify which run a message belongs to.

The bridge never constructs platform-specific payloads. It passes semantic views; the backend decides how to render them.

### Routing State Model

The bridge uses an explicit three-state routing model to dispatch incoming events:

```python
@dataclass
class _RouterState:
    mode: Literal["idle", "decision_open", "awaiting_author_reply"]
    decision_id: str | None = None  # set when mode != "idle"
```

- **`idle`**: No decision is active. Incoming messages are queued as steering.
- **`decision_open(decision_id)`**: A decision prompt has been posted and the bridge is waiting for a terminal command (`/A`, `/skip`, `/custom`). Incoming slash commands are routed as decision input. Bare text is routed as a discussion question for the author.
- **`awaiting_author_reply(decision_id)`**: A question has been sent to the author and the bridge is waiting for the discussion response to be posted. Incoming messages during this window are rejected with a visible reply: "Waiting for author response — send your next message after the reply." This prevents messages from being silently reinterpreted as steering when the reviewer likely intends them as discussion. After the discussion reply is posted, the state returns to `decision_open`.

The key properties of this model:

- **Decision ID correlation for callbacks**: Inline keyboard callbacks carry an embedded `decision_id`. Stale callbacks (referencing a resolved decision) are rejected with a visible "decision already resolved" reply.
- **No decision ID correlation for text commands**: Text replies (`/A`, `/skip`, `/custom`) do not carry a decision ID. They are accepted against whatever decision is currently open. This is a known limitation — the inline keyboard is the recommended path for unambiguous correlation.
- **Explicit rejection during author round-trips**: Rather than silently queuing messages as steering during the brief author response window, the bridge rejects them with a visible explanation. This avoids the surprising behavior of follow-up discussion text being reinterpreted as phase-boundary steering.

### RemoteReviewBridge

```python
class RemoteReviewBridge:
    """Implements Writer + SteeringSource, bridging to Telegram."""

    def __init__(self, backend: MessagingBackend, session_id: str):
        self._backend = backend
        self._session_id = session_id
        self._steering_queue: Queue[str] = Queue()
        self._decision_event = threading.Event()
        self._decision_result: tuple[str, str] | None = None
        self._router = _RouterState(mode="idle")
        self._listener_thread: threading.Thread | None = None
        self._phase_buffer: list[str] = []
        self._last_posted_artifact_hash: str | None = None
```

**Writer methods:**

- `write(text)` / `write_thinking(text)`: Append to `_phase_buffer`. Not posted to chat — agent output is too verbose for messaging. Buffer is discarded on phase boundaries. This is a deliberate departure from the terminal writer contract; see Architecture section.
- `write_status(text)`: Strip Rich markup, post via `backend.post_status()`. Transient post failures are logged and swallowed.
- `present_decision(decision, prior_decisions)`: Compute SHA-256 hash of raw draft file bytes; if changed since `_last_posted_artifact_hash`, post draft as artifact with name `draft-iterNN.md` and update the stored hash. Construct `DecisionView` from decision dict and prior decisions, including `_session_id`. Post via `backend.post_decision()`. Set router to `decision_open(decision["id"])`.
- `await_decision_input(valid_keys, prompt_text)`: Store `valid_keys` for the listener. Block on `_decision_event`. Return the parsed `(key, text)` tuple. Clear `_decision_event` before blocking (allows re-entry for multi-turn discussion).
- `bell()`: No-op (the decision message posting itself is the notification).

**SteeringSource methods:**

- `start()`: Call `backend.validate_connection()` (fail fast). Call `backend.post_handshake()` with task summary, max iterations, and session ID. Persist session metadata to `history.json` immediately after successful handshake. Spawn `_listener_thread`.
- `stop()`: Signal the listener to exit. Post a run-complete status.
- `drain()`: Drain `_steering_queue` (identical to existing `_drain_queue` logic).
- `put(message)`: Enqueue directly.

**Listener thread logic:**

The listener runs continuously for the lifetime of the run, polling on a 2-second interval.

```python
def _listen_loop(self):
    while self._running:
        events = self._backend.poll_events()
        for event in events:
            if event.source_type == "edit":
                continue  # ignore edits

            if self._router.mode == "idle":
                self._steering_queue.put(event.text)

            elif self._router.mode == "decision_open":
                # Check callback decision ID if present
                if event.decision_id is not None:
                    if event.decision_id != self._router.decision_id:
                        self._backend.post_text("That decision has already been resolved.")
                        continue

                result = _parse_decision_input(event.text, self._decision_valid_keys)
                if result is not None and result[0] not in ("", "options"):
                    # Terminal command (/A, /skip, /custom)
                    self._decision_result = result
                    self._router = _RouterState(mode="idle")
                    self._decision_event.set()
                elif result is not None and result[0] == "options":
                    # Options re-display request
                    self._decision_result = result
                    self._decision_event.set()
                else:
                    # Bare text = question for author discussion
                    self._router = _RouterState(
                        mode="awaiting_author_reply",
                        decision_id=self._router.decision_id,
                    )
                    self._decision_result = ("", event.text)
                    self._decision_event.set()

            elif self._router.mode == "awaiting_author_reply":
                # Reject with visible explanation
                self._backend.post_text(
                    "Waiting for author response — send your next message after the reply."
                )

        time.sleep(2)
```

After the orchestrator posts the discussion reply via `backend.post_text()`, it sets the router back to `decision_open(decision_id)` and calls `await_decision_input` again. This ensures the reviewer's next message is routed as decision input.

### Discussion Loop Contract

When `await_decision_input` returns `("", question_text)`, the orchestrator calls `claude_agent.discuss()` and posts the response via `backend.post_text()`. It then sets the router back to `decision_open(decision_id)` and calls `await_decision_input` again with the same `valid_keys`, re-entering the blocking wait. This loop continues until a terminal input arrives — any valid `/KEY`, `/skip`, or `/custom` command. The loop is bounded only by the reviewer's patience; there is no turn limit.

This is the same contract as the existing TUI discussion flow, where `_resolve_decisions` loops on `await_decision_input` until a non-question result is returned.

### Output and Artifact Strategy

Agent output (drafting, revising) is buffered in `_phase_buffer` and discarded at phase boundaries. It is not posted to chat. The remote reviewer's feed contains only:

- **Status messages**: Phase transitions, iteration counts, steering confirmations, decision locks.
- **Review summary**: The reviewer agent's output (from `codex_agent.review()` or `claude_agent.review()`) is the text that precedes decision prompts in the orchestration flow. When the review phase completes, its output is posted as a message if it fits within Telegram's 4096-character limit, or as a file attachment named `review-iterNN.md` if it exceeds that limit. This gives the reviewer the full review context before making decisions.
- **Draft artifact**: Posted as a file attachment named `draft-iterNN.md` at the start of each review phase, but only if the document content has changed since the last posted artifact (tracked via SHA-256 hash of raw draft file bytes). This avoids flooding the chat with near-duplicate files on long iterative runs.
- **Decision prompts**: Posted with inline keyboards and visible session ID.
- **Author discussion replies**: Posted as plain text messages.
- **Final artifact**: Posted as `final.md` on run completion or interruption.
- **Walkthrough**: Posted as `walkthrough.md`.

This is a deliberate trade-off: the remote reviewer sees less streaming detail than the terminal user, but gets a cleaner, more actionable feed. The reviewer's job is to make decisions, not watch tokens stream.

### Telegram Backend

Uses raw HTTP via `httpx` against the Telegram Bot API. Requires a bot token from BotFather.

**Conversation model**: Messages are posted to a specific chat ID representing a DM with the authorized reviewer. DM-only in v1 — this eliminates the identity ambiguity of group chats and makes the chat ID itself the authority boundary. The reviewer must have started a conversation with the bot before the run begins (Telegram requirement for bot-initiated DMs). Group chat support is out of scope for v1.

**Trust model**: In Telegram DMs, the bot's conversation partner is the only possible sender. The chat ID is the authority boundary — no separate reviewer ID configuration is needed. The backend does not perform independent cryptographic verification of sender identity; it trusts Telegram's authenticated `from.id` field. Updates received from any chat ID other than the configured target (e.g., if the bot is added to a group) are discarded and logged at debug level with the source chat ID and update type, to aid diagnosis of misconfiguration.

**Decision rendering**: Uses inline keyboards for option buttons. Each button carries callback data in the format `decision_id:key` (e.g., `d1:A`). When pressed, the callback query is answered (to clear the loading indicator) and normalized to an `IncomingEvent` with the `decision_id` extracted. Text replies are also accepted for `/custom`, questions, and steering.

**Listening**: Uses `getUpdates` long-polling (30-second timeout, built into the Bot API). On startup, the backend initializes its offset so that all pre-session updates are ignored (implementation detail: consume pending updates and track from the latest offset onward). The backend filters to only yield events from the target chat ID. Long-poll timeouts are expected and not errors. Transient network failures (connection reset, DNS timeout) retry internally with capped backoff (max 30s) while the listener remains alive.

**Artifact delivery**: Uses `sendDocument` to post file attachments.

**Startup validation** (`validate_connection`): Calls `getMe` to verify the token. No messages are posted — this is a pure prerequisite check. Fails fast if the token is invalid.

**Handshake** (`post_handshake`): Posts a `sendMessage` to the target chat confirming the run configuration (task summary, max iterations, session ID). Fails fast if the bot is blocked or the chat doesn't exist. This proves post access but not reply observability — the first real proof comes when the reviewer responds to the first decision.

**Error handling**: Transient API errors (429 rate limit, 5xx server errors, network timeouts) are retried per the operation-class policy defined in the Failure Model requirement. The `Retry-After` header is respected for 429 responses. Permanent errors (401 unauthorized, 403 forbidden, 400 bad request) are raised immediately.

### Session Identity and Resume

Each run generates a short session ID (8-character hex) at startup. This ID appears in handshake messages and decision prompts so the reviewer can distinguish between runs.

Session metadata is persisted to `history.json` immediately after a successful handshake — not deferred to `stop()`. This ensures the metadata is available for resume even after an unclean exit (crash, `kill -9`). The persisted fields are:

```json
{
  "remote_session": {
    "platform": "telegram",
    "chat_id": "123456789",
    "session_id": "a1b2c3d4"
  }
}
```

On resume (`autoplanner -c last --remote telegram ...`), the bridge reads prior session metadata and attempts to post a notice to the old chat: "Session `a1b2c3d4` has ended. A new session is starting — please respond to the new decision prompts below." If this post fails (bot blocked, chat deleted), resume proceeds silently. The bridge then posts a new handshake with a new session ID, persists new metadata (overwriting the prior entry), and re-presents pending decisions.

This is not seamless conversation continuity. It is state recovery into a fresh session with a best-effort notice that the old session is stale. The reviewer must respond to the new decision prompts.

### CLI Integration

A new `--remote` flag specifies the messaging backend:

```
autoplanner --remote telegram --telegram-chat 123456789 "Design a caching layer"
```

When `--remote telegram` is set:
- `--headless` is implied (no TUI).
- `--human-review` is implied (remote mode exists for decision review).
- `--on-decision` defaults to `prompt`.
- The bridge is constructed and passed as both `steering_source` and the active writer via `set_writer()`.

The bot token is read from the `TELEGRAM_BOT_TOKEN` environment variable, following the existing pattern of not storing secrets in config files.

**Setup prerequisites**: Before running in remote mode, the operator must: (1) create a Telegram bot via BotFather and set `TELEGRAM_BOT_TOKEN`, (2) have the reviewer start a DM conversation with the bot, and (3) obtain the DM chat ID (e.g., by sending a message to the bot and calling `getUpdates` manually, or using a helper like `@userinfobot`). Auto-discovery of chat IDs is out of scope for v1.

### Module Layout

```
autoplanner/
  remote/
    __init__.py          # RemoteReviewBridge, IncomingEvent, DecisionView, _RouterState
    backend.py           # MessagingBackend protocol
    telegram.py          # TelegramBackend
```

Dependencies are optional extras:

```toml
[project.optional-dependencies]
telegram = ["httpx>=0.27"]
```

## Execution Plan

### Phase 1: Core bridge and Telegram backend

1. Define `IncomingEvent`, `DecisionView`, `_RouterState`, and `MessagingBackend` protocol in `backend.py`.
2. Implement `RemoteReviewBridge` with all `Writer` and `SteeringSource` methods, three-state router, and session metadata persistence (written immediately after handshake).
3. Implement `TelegramBackend` with inline keyboards, `getUpdates` polling, cursor initialization, DM-only identity model, split `validate_connection` / `post_handshake`, and per-operation retry policy.
4. Add `--remote telegram` and `--telegram-chat` CLI flags to `main.py`. Wire bridge construction and injection.
5. Test end-to-end: run with Telegram DM, resolve decisions via buttons and text, verify steering, verify discussion loop with rejection during author round-trip, verify resume with session notice.

### Phase 2: Hardening

6. Add tests for event normalization, routing state transitions, decision ID correlation (callbacks), stale-callback rejection, and text-command-against-current-decision semantics.
7. Add tests for startup validation failure modes (bad token, blocked bot, inaccessible chat) and handshake/validation separation.
8. Add tests for per-operation-class retry and permanent error propagation.
9. Update README with remote mode documentation, setup instructions (bot creation, DM initiation, chat ID discovery), and credential configuration.

### Phase 3: Slack backend (future)

10. Implement `SlackBackend` — either text-only with `conversations.replies` polling, or Socket Mode if the webhook-free constraint can be relaxed.
11. Add `--remote slack`, `--slack-channel`, and `--reviewer-id` CLI flags.
12. Define exact Slack event eligibility rules: only threaded replies under `thread_ts`, ignore bot messages, ignore reactions/edits, ignore channel-level messages.
13. Specify token/scope matrix for supported Slack modes (DM vs channel).

## Alternatives Considered

### 1. Webhook-based listening instead of polling

Telegram supports webhook mode with push-based message delivery. This would eliminate polling latency.

**Rejected because**: Push requires a publicly routable HTTP endpoint. AutoPlanner runs on a developer's laptop — exposing a public endpoint requires ngrok or similar tunneling, which is fragile and adds setup steps. The polling approach works behind any NAT/firewall, and the 2-second latency is irrelevant when the human takes minutes to respond. Polling wins on simplicity.

### 2. Separate Writer and SteeringSource implementations

Instead of a single `RemoteReviewBridge`, have a `RemoteWriter` and a `RemoteSteering` that share a backend reference.

**Rejected because**: The decision flow requires tight coordination between output (posting the decision) and input (waiting for a reply). The `await_decision_input` method on `Writer` already couples these concerns — it both prompts and waits. A single bridge object with shared state (router, decision event) is simpler than two objects that must synchronize through a shared lock or event. The existing `TuiWriter` follows this same pattern.

### 3. Generic "remote" abstraction that also covers email, SMS, etc.

Design a maximally generic `RemoteChannel` that could support any async communication medium.

**Rejected because**: Email and SMS have fundamentally different interaction models (no threading, high latency, no editing). Over-abstracting for hypothetical platforms would complicate the platform we actually need. The `MessagingBackend` protocol is intentionally scoped to real-time chat platforms with inline interaction affordances. Adding a new chat platform means implementing six semantic methods, which is already easy. YAGNI.

### 4. Running a web server with a chat-like UI

Serve a simple web page with a chat interface that the reviewer opens in a browser.

**Rejected because**: The goal is to meet the reviewer where they already are (Telegram on their phone, in their existing notification flow). A custom web UI requires the reviewer to open a new tab, bookmark it, and check it manually — it provides no push notifications and no integration with their existing workflow. It also requires serving HTTP, handling WebSocket connections, and building a frontend.

### 5. Posting full streaming output to the chat

Stream every token to the chat in real-time, matching the terminal experience.

**Rejected because**: Chat platforms are not terminals. Rapid message edits hit rate limits (Telegram: ~30 edits/minute per message). Posting a new message per chunk would create hundreds of messages per phase, burying the decision prompt. The review-focused approach (buffer during agent work, post artifact on review) respects the medium.

### 6. Using Claude Code's existing Telegram MCP plugin

The runtime already has a Telegram MCP server with `reply`, `react`, `edit_message`, and `download_attachment` tools.

**Rejected because**: The MCP plugin is designed for Claude-as-agent responding to Telegram messages, not for AutoPlanner's orchestrator loop driving messages programmatically. The orchestrator needs to post structured decisions, block on responses, and route replies based on phase state. Shoehorning this into MCP tool calls would invert the control flow — the orchestrator would need to call MCP tools through Claude, adding a Claude round-trip to every message post. Direct API calls from the bridge are simpler and faster.

### 7. Raw post/update message backend instead of semantic operations

Use a minimal `post_message(text)` / `update_message(id, text)` / `listen()` backend protocol, with the bridge constructing all formatting.

**Rejected because**: Platform-specific affordances (Telegram inline keyboards, future Slack Block Kit) are not representable by a shared text parameter. Telegram callback queries are not plain text messages. A semantic backend contract (`post_decision(DecisionView)`, `post_status(text)`, `post_artifact(path)`, `poll_events()`) lets each backend map to its own rendering and interaction model without the bridge knowing platform details.

### 8. Full multi-state machine for message routing

Replace the routing model with a comprehensive state machine covering every possible message ordering, including out-of-band messages, concurrent decisions, and cross-session replay.

**Rejected because**: The three-state model (`idle`, `decision_open`, `awaiting_author_reply`) with decision ID correlation on callbacks handles the real async risks: stale callbacks, messages during author round-trips, and steering/decision disambiguation. The orchestrator is inherently single-decision-at-a-time, so concurrent-decision states cannot occur. Cross-session replay is handled by cursor reset and session notices. A more elaborate state machine would add complexity without covering new real-world scenarios.

### 9. Shipping Slack alongside Telegram in v1

Include Slack support in the initial release to cover both major team chat platforms.

**Rejected because**: Slack's interactive components require a webhook endpoint (conflicting with the no-public-endpoint constraint), making its experience strictly worse than Telegram's. Text-only Slack with `conversations.replies` polling requires additional specification work: bot self-message filtering, thread vs channel reply disambiguation, exact scope matrices for DM vs public/private channels, and authorized identity enforcement in multi-user contexts. Shipping Telegram-only in v1 produces a tighter, more testable system. The `MessagingBackend` protocol ensures Slack can be added later without bridge changes.

### 10. Silently queuing messages as steering during author round-trips

When the reviewer sends messages while an author discussion reply is in flight, queue them as steering to be applied at the next phase boundary.

**Rejected because**: A reviewer who sends "actually compare A vs B on latency too" immediately after their question almost certainly intends it as continued discussion, not as global steering. Silently reinterpreting likely discussion text as future steering creates surprising behavior. The explicit rejection approach ("Waiting for author response — send your next message after the reply") is more predictable and prevents accidental steering injection. The author round-trip is typically brief (seconds), so the wait is minimal.

## Locked Decisions

- **d1: Persist minimal remote session metadata for resume** — Write platform, chat ID, and session ID to `history.json` immediately after successful handshake (not deferred to `stop()`). On resume, post a "session moved" notice to the old chat if reachable, then start a fresh session. Full conversation continuity remains out of scope.
- **d2: Define the trust boundary for reviewer identity** — Telegram v1 is DM-only; the chat ID is the authority boundary. No separate reviewer ID configuration. Trust platform-authenticated sender identity; no independent cryptographic verification. Off-target updates are discarded and logged at debug level.
- **d3: Use a three-state routing model for remote input** — Route incoming events through `idle`, `decision_open(decision_id)`, and `awaiting_author_reply(decision_id)` states. Callback-based decision input is correlated by embedded `decision_id`; text-based commands apply to the currently open decision only (no embedded correlation). Messages during author round-trips are explicitly rejected, not silently requeued.
- **d4: Ship Telegram-only in v1** — Slack is deferred to a later phase. The `MessagingBackend` protocol supports future platforms without bridge changes.
- **d5: Separate validation from handshake in startup** — `validate_connection()` checks prerequisites without posting messages; `post_handshake()` establishes the session. This prevents visible messages for runs that fail during setup.

## Open Questions

1. **How should the bridge handle reviewer absence?** If the reviewer never responds to a decision prompt, the process blocks indefinitely. Options: (a) add a `--decision-timeout` flag that falls back to `accept` after N minutes, (b) post a reminder after a configurable interval, (c) do nothing and let the user kill/resume. Leaning toward (c) with (b) as a lightweight addition — the process is cheap to leave running, and `autoplanner -c` handles recovery.

2. **Should the draft artifact be posted as inline text or always as a file?** File attachments are cleaner for long documents but require an extra tap to open on mobile. Inline text is immediately visible but may be truncated (Telegram's 4096-char limit) or overwhelming. Current design uses file attachments unconditionally when content has changed. This could be refined based on document length (inline if under 2000 chars, file otherwise), but that adds a decision point without clear benefit for v1.

3. **Should startup validation include an explicit reviewer acknowledgment?** The current preflight proves post access but not that the reviewer is paying attention. An optional `--require-ack` flag could block until the reviewer replies to the handshake message before starting orchestration. This would prevent wasted compute if the reviewer's notifications are off, but adds friction to every run start. Leaning toward not requiring it in v1 and adding it if real usage reveals a problem.