from __future__ import annotations

from pathlib import Path

from autoplanner.agents.run import stream_command

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def review(
    document: str,
    task: str,
    iteration: int,
    *,
    steering: str | None = None,
) -> str:
    steering_part = ""
    if steering:
        steering_part = (
            f"\n\nThe document author has additional guidance for this review:\n"
            f"{steering}\nTake this into account in your review."
        )

    prompt_template = (PROMPTS_DIR / "codex_review.txt").read_text(encoding="utf-8")
    prompt = prompt_template.format(task=task, iteration=iteration, document=document) + steering_part
    return stream_command(
        ["codex", "exec", prompt],
        label="codex",
    )
