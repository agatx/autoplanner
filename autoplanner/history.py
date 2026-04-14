from __future__ import annotations

import fcntl
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


def _slugify(text: str, max_len: int = 50) -> str:
    """Turn a task description into a filesystem-safe slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text[:max_len]


@dataclass
class IterationRecord:
    iteration: int
    phase: Literal["draft", "review", "revision", "decision"]
    author: Literal["claude", "codex", "human"]
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class History:
    task: str
    run_id: str
    work_dir: Path
    records: list[IterationRecord] = field(default_factory=list)
    decisions: dict[str, dict] = field(default_factory=dict)
    _lock_fd: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        self.save_json()

    def _acquire_lock(self) -> None:
        lock_path = self.work_dir / "lock"
        self._lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RuntimeError(
                f"Another autoplanner run is active in {self.work_dir}. "
                "Wait for it to finish or remove the lock file."
            )

    def release(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def add(self, record: IterationRecord) -> Path:
        self.records.append(record)
        filename = f"{record.iteration:02d}_{record.phase}.md"
        path = self.work_dir / filename
        path.write_text(record.content, encoding="utf-8")
        self.save_json()
        return path

    def last_document_and_review(self) -> tuple[str, str]:
        """Return the most recent (document, review) from the history records."""
        document = ""
        review = ""
        for rec in self.records:
            if rec.phase in ("draft", "revision"):
                document = rec.content
            elif rec.phase == "review":
                review = rec.content
        return document, review

    # ---- Decision state methods ----

    def propose_decision(self, decision: dict) -> bool:
        """Add a proposed decision. Returns False (no-op) on dedup or already-proposed."""
        did = decision["id"]
        existing = self.decisions.get(did)

        conflict_ref = decision.get("conflict_with")

        if existing is not None:
            # Dedup/idempotent replay is only allowed for non-conflict proposals.
            if not conflict_ref and existing["state"] in ("proposed", "active"):
                return False

            # Any other ID reuse is invalid because it would overwrite existing state.
            raise ValueError(
                f"Decision ID {did} already exists with state {existing['state']}"
            )

        # Handle conflict: transition referenced decision active -> challenged
        if conflict_ref:
            target = self.decisions.get(conflict_ref)
            if target is None or target["state"] not in ("active", "challenged"):
                raise ValueError(
                    f"conflict_with references {conflict_ref} which is not active or challenged"
                )
            if target["state"] == "active":
                target["state"] = "challenged"

        self.decisions[did] = {
            "id": did,
            "state": "proposed",
            "title": decision["title"],
            "summary": decision.get("summary", ""),
            "options": decision.get("options", []),
            "current_choice": decision.get("current_choice"),
            "resolution": None,
            "conflict_with": conflict_ref,
            "superseded_by": None,
        }
        self.save_json()
        return True

    def lock_decision(self, decision_id: str, resolution: dict) -> None:
        """Transition proposed -> active, storing the human's resolution."""
        entry = self.decisions[decision_id]

        # Idempotent: already active with same resolution
        if entry["state"] == "active" and entry["resolution"] == resolution:
            return

        entry["state"] = "active"
        entry["resolution"] = resolution

        conflict_ref = entry.get("conflict_with")
        if conflict_ref and resolution.get("chosen_effect"):
            original = self.decisions[conflict_ref]
            if resolution["chosen_effect"] == "supersede":
                original["state"] = "superseded"
                original["superseded_by"] = decision_id
            elif resolution["chosen_effect"] == "keep_original":
                original["state"] = "active"
                # The conflict proposal itself lost — mark it superseded
                entry["state"] = "superseded"

        self.save_json()

    def active_decisions(self) -> list[dict]:
        """Return active and challenged decisions (for prompt injection)."""
        return [d for d in self.decisions.values() if d["state"] in ("active", "challenged")]

    def has_proposed(self) -> bool:
        """Return whether any proposed (unresolved) decisions exist."""
        return any(d["state"] == "proposed" for d in self.decisions.values())

    def pending_decisions(self) -> list[dict]:
        """Return proposed decisions (for resume re-presentation)."""
        return [d for d in self.decisions.values() if d["state"] == "proposed"]

    # ---- Iteration history ----

    def build_iteration_history(self) -> str:
        """Build a structured summary of all iterations for the walkthrough prompt."""
        lines: list[str] = []
        for record in self.records:
            lines.append(f"### Iteration {record.iteration} — {record.phase.title()} (by {record.author})\n")
            lines.append(record.content)
            lines.append("\n---\n")
        return "\n".join(lines)

    @classmethod
    def from_directory(cls, work_dir: Path, *, lock: bool = False) -> "History":
        """Load a History from an existing work directory's history.json.

        If *lock* is True the work directory is locked for writing (resume mode).
        """
        json_path = work_dir / "history.json"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        h = object.__new__(cls)
        h.task = data["task"]
        h.run_id = data["run_id"]
        h.work_dir = work_dir
        h.records = [IterationRecord(**r) for r in data["records"]]
        h.decisions = data.get("decisions", {})
        h._lock_fd = None
        if lock:
            h._acquire_lock()
        return h

    def save_json(self) -> Path:
        data = {
            "task": self.task,
            "run_id": self.run_id,
            "records": [asdict(r) for r in self.records],
            "decisions": self.decisions,
        }
        json_path = self.work_dir / "history.json"
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return json_path


def make_run_id(task: str) -> str:
    slug = _slugify(task)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{ts}"


def make_output_name(task: str, suffix: str) -> str:
    slug = _slugify(task)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{ts}-{suffix}.md"


def find_run_dir(base_dir: Path, run_id: str | None = None) -> Path:
    """Locate a run directory under *base_dir*.

    If *run_id* is ``None`` or one of the sentinels ``"last"`` / ``"latest"``,
    return the most-recently-modified directory that contains a ``history.json``.
    Otherwise look for an exact or substring match on the directory name.
    """
    candidates = sorted(
        (d for d in base_dir.iterdir() if d.is_dir() and (d / "history.json").exists()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No completed runs found in {base_dir}")

    if run_id is None or run_id in ("last", "latest"):
        return candidates[0]

    # Exact match
    for d in candidates:
        if d.name == run_id:
            return d

    # Substring match
    matches = [d for d in candidates if run_id in d.name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = "\n  ".join(d.name for d in matches)
        raise ValueError(f"Ambiguous run ID '{run_id}', matches:\n  {names}")

    names = "\n  ".join(d.name for d in candidates)
    raise FileNotFoundError(f"No run matching '{run_id}'. Available:\n  {names}")
