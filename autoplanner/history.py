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
    phase: Literal["draft", "review", "revision"]
    author: Literal["claude", "codex"]
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class History:
    task: str
    run_id: str
    work_dir: Path
    records: list[IterationRecord] = field(default_factory=list)
    _lock_fd: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()

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
        return path

    def generate_walkthrough(self) -> Path:
        lines: list[str] = []
        lines.append("# Document Evolution Walkthrough\n")
        lines.append(f"**Task:** {self.task}\n")
        lines.append(f"**Run ID:** {self.run_id}\n")
        lines.append(f"**Total iterations:** {max((r.iteration for r in self.records), default=0)}\n")
        lines.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n")

        for record in self.records:
            heading = f"## Iteration {record.iteration} — {record.phase.title()}"
            lines.append(f"\n{heading}\n")
            lines.append(f"**Author:** {record.author}  ")
            lines.append(f"**Timestamp:** {record.timestamp}\n")

            if record.phase == "review":
                lines.append("### Feedback\n")
            else:
                lines.append("### Document Snapshot\n")

            lines.append(f"\n{record.content}\n")
            lines.append("\n---\n")

        walkthrough = "\n".join(lines)
        path = self.work_dir / "walkthrough.md"
        path.write_text(walkthrough, encoding="utf-8")

        data = {
            "task": self.task,
            "run_id": self.run_id,
            "records": [asdict(r) for r in self.records],
        }
        json_path = self.work_dir / "history.json"
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        return path


def make_run_id(task: str) -> str:
    slug = _slugify(task)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{ts}"


def make_output_name(task: str, suffix: str) -> str:
    slug = _slugify(task)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{ts}-{suffix}.md"
