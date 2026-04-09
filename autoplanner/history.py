from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


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
    records: list[IterationRecord] = field(default_factory=list)
    output_dir: Path = field(default_factory=lambda: Path("output"))

    def __post_init__(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def add(self, record: IterationRecord) -> Path:
        self.records.append(record)
        filename = f"{record.iteration:02d}_{record.phase}.md"
        path = self.output_dir / filename
        path.write_text(record.content, encoding="utf-8")
        return path

    def generate_walkthrough(self) -> Path:
        lines: list[str] = []
        lines.append("# Document Evolution Walkthrough\n")
        lines.append(f"**Task:** {self.task}\n")
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
        path = self.output_dir / "walkthrough.md"
        path.write_text(walkthrough, encoding="utf-8")

        # Also save structured data
        data = {
            "task": self.task,
            "records": [asdict(r) for r in self.records],
        }
        json_path = self.output_dir / "history.json"
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        return path
