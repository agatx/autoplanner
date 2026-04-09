from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def steering_block(steering: str | None) -> str:
    if not steering:
        return ""
    return (
        f"\n\n## Author's Guidance\n\n"
        f"The document author has provided additional direction:\n{steering}\n"
        f"Incorporate this guidance into your work."
    )
