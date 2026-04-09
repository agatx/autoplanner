from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from autoplanner.agents.run import stream_command, StreamMode

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def preflight() -> bool:
    """Return True if Codex is available and has capacity."""
    try:
        result = subprocess.run(
            ["codex", "exec", "--json", "Reply with exactly: ok"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "rate" in stderr or "limit" in stderr or "quota" in stderr or "429" in stderr:
                print("  [codex] Rate limit detected in preflight check", file=sys.stderr)
                return False
            # Check stdout JSON for errors too
            for line in result.stdout.splitlines():
                if '"error"' in line.lower() or '"rate' in line.lower():
                    print("  [codex] Rate limit detected in preflight check", file=sys.stderr)
                    return False
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  [codex] Preflight timed out", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("  [codex] codex CLI not found", file=sys.stderr)
        return False


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
        ["codex", "exec", "--json", prompt],
        label="codex",
        mode=StreamMode.CODEX,
    )
