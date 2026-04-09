from __future__ import annotations

import subprocess
import sys

from autoplanner.agents.session import CodexSession
from autoplanner.prompts import load, steering_block


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
            combined = (result.stderr + result.stdout).lower()
            if any(s in combined for s in ["rate", "limit", "quota", "429", "error"]):
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
    session: CodexSession,
    document: str,
    task: str,
    iteration: int,
    *,
    max_iterations: int = 5,
    steering: str | None = None,
) -> str:
    remaining = max_iterations - iteration
    prompt = load("codex_review.txt").format(
        task=task, iteration=iteration, document=document,
        max_iterations=max_iterations, remaining=remaining,
    ) + steering_block(steering)
    return session.send(prompt)
