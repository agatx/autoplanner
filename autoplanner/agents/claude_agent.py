from __future__ import annotations

import re
from pathlib import Path

from autoplanner.agents.run import stream_command

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


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
    prompt_template = (PROMPTS_DIR / "claude_draft.txt").read_text(encoding="utf-8")
    prompt = prompt_template.format(task=task) + _steering_block(steering)
    return _run_claude(prompt, model=model)


def revise(
    document: str,
    review: str,
    *,
    steering: str | None = None,
    model: str = "sonnet",
) -> str:
    prompt_template = (PROMPTS_DIR / "claude_revise.txt").read_text(encoding="utf-8")
    prompt = prompt_template.format(document=document, review=review) + _steering_block(steering)
    return _run_claude(prompt, model=model)
