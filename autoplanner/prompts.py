from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def locked_decisions_block(decisions: list[dict]) -> str:
    """Format active/challenged decisions into a prompt section."""
    if not decisions:
        return ""
    lines = ["\n\n## Locked Decisions\n"]
    for d in decisions:
        status_note = " (under review \u2014 conflict pending)" if d["state"] == "challenged" else ""
        lines.append(f"- **{d['id']}: {d['title']}**{status_note}")
        if d.get("resolution"):
            lines.append(f"  Direction: {d['resolution']['locked_direction']}")
    lines.append(
        "\nThe writer MUST follow these locked directions. "
        "The reviewer MUST NOT reopen them unless raising an explicit new conflict.\n"
    )
    return "\n".join(lines)


def steering_block(steering: str | None) -> str:
    if not steering:
        return ""
    return (
        f"\n\n## Author's Guidance\n\n"
        f"The document author has provided additional direction:\n{steering}\n"
        f"Incorporate this guidance into your work."
    )
