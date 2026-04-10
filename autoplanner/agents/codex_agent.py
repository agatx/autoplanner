from __future__ import annotations

import subprocess

from autoplanner.agents.session import CodexSession
from autoplanner.prompts import load, steering_block


def preflight() -> bool:
    """Return True if Codex is available and has capacity."""
    from autoplanner.output import get_writer
    w = get_writer()
    try:
        result = subprocess.run(
            ["codex", "exec", "--json", "-"],
            input="Reply with exactly: ok",
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            combined = (result.stderr + result.stdout).lower()
            if any(s in combined for s in ["rate", "limit", "quota", "429", "error"]):
                w.write_status("  [codex] Rate limit detected in preflight check")
                return False
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        w.write_status("  [codex] Preflight timed out")
        return False
    except FileNotFoundError:
        w.write_status("  [codex] codex CLI not found")
        return False


def review(
    session: CodexSession,
    document: str,
    task: str,
    iteration: int,
    *,
    max_iterations: int = 5,
    steering: str | None = None,
) -> str:
    remaining = max_iterations - iteration
    prompt = load("review.txt").format(
        task=task, iteration=iteration, document=document,
        max_iterations=max_iterations, remaining=remaining,
    ) + steering_block(steering)
    return session.send(prompt)
