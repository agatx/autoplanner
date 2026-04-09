from __future__ import annotations

from autoplanner.agents.run import stream_command


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

    prompt = (
        "You are a critical reviewer of requirements documents. "
        "Review the following document for completeness, clarity, consistency, "
        "feasibility, and missing edge cases. Be specific and actionable.\n\n"
        f"Original task: {task}\n"
        f"Iteration: {iteration}\n\n"
        f"## Document to Review\n\n{document}\n\n"
        "Provide your review as a structured list of issues and suggestions. "
        "If the document is ready to ship, start your response with 'LGTM' "
        "and explain briefly why it's ready."
        + steering_part
    )
    return stream_command(
        ["codex", "exec", prompt],
        label="codex",
    )
