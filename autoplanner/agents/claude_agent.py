from __future__ import annotations

import re

from autoplanner.agents.session import ClaudeSession
from autoplanner.prompts import load, steering_block


def _extract_markdown(text: str) -> str:
    """Strip conversational preamble, return only the markdown document."""
    fence_match = re.search(r"```markdown\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    heading_match = re.search(r"^(#\s+.+)$", text, re.MULTILINE)
    if heading_match:
        return text[heading_match.start():].strip()

    return text.strip()


def draft(session: ClaudeSession, task: str, *, steering: str | None = None) -> str:
    prompt = load("claude_draft.txt").format(task=task) + steering_block(steering)
    return _extract_markdown(session.send(prompt))


def revise(
    session: ClaudeSession,
    document: str,
    review: str,
    *,
    iteration: int = 1,
    max_iterations: int = 5,
    steering: str | None = None,
) -> str:
    remaining = max_iterations - iteration
    prompt = load("claude_revise.txt").format(
        document=document, review=review,
        iteration=iteration, max_iterations=max_iterations, remaining=remaining,
    ) + steering_block(steering)
    return _extract_markdown(session.send(prompt))


def review(
    session: ClaudeSession,
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
