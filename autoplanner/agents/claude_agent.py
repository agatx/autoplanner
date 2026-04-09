from __future__ import annotations

import re

from autoplanner.agents.run import stream_command


def _extract_markdown(text: str) -> str:
    """Strip conversational preamble, return only the markdown document."""
    fence_match = re.search(r"```markdown\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    heading_match = re.search(r"^(#\s+.+)$", text, re.MULTILINE)
    if heading_match:
        return text[heading_match.start():].strip()

    return text.strip()


def _steering_block(steering: str | None) -> str:
    if not steering:
        return ""
    return (
        f"\n\n## Author's Guidance\n\n"
        f"The document author has provided additional direction:\n{steering}\n"
        f"Incorporate this guidance into your work."
    )


def _run_claude(prompt: str, *, model: str = "sonnet") -> str:
    output = stream_command(
        [
            "claude",
            "-p",
            "--model", model,
            "--no-session-persistence",
            prompt,
        ],
        label="claude",
    )
    return _extract_markdown(output)


def draft(task: str, *, steering: str | None = None, model: str = "sonnet") -> str:
    prompt = (
        "You are a senior technical writer. Draft a requirements document for the "
        "following task. Use clear sections (Overview, Goals, Non-Goals, Requirements, "
        "Open Questions). Output ONLY the markdown document — no preamble, no commentary, "
        "no wrapping code fences. Start directly with the top-level heading.\n\n"
        f"Task: {task}"
        + _steering_block(steering)
    )
    return _run_claude(prompt, model=model)


def revise(
    document: str,
    review: str,
    *,
    steering: str | None = None,
    model: str = "sonnet",
) -> str:
    prompt = (
        "You are a senior technical writer. You have received a review of your "
        "requirements document. Revise the document to address the feedback. "
        "Output ONLY the revised markdown document — no preamble, no commentary, "
        "no wrapping code fences. Start directly with the top-level heading.\n\n"
        f"## Current Document\n\n{document}\n\n"
        f"## Review Feedback\n\n{review}"
        + _steering_block(steering)
    )
    return _run_claude(prompt, model=model)
