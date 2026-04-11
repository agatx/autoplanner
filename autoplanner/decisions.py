"""Decision trailer extraction and validation.

The reviewer emits a fenced ```decisions``` block at the end of every review
when --human-review is enabled.  This module parses and validates that block.
"""
from __future__ import annotations

import json
import re

from autoplanner.debug import debug

_FENCE_RE = re.compile(r"```decisions\s*\n(.*?)\n```", re.DOTALL)


def extract_decisions(
    review_text: str, existing_decisions: dict[str, dict],
) -> tuple[str, list[dict]]:
    """Parse the decisions trailer from review text.

    Returns ``(status, decisions_list)`` where *status* is one of:

    * ``"none"``        – trailer present, no decisions
    * ``"present"``     – trailer present with valid decisions
    * ``"parse_error"`` – trailer missing or malformed
    """
    m = _FENCE_RE.search(review_text)
    if m is None:
        debug("decisions: no fenced block found")
        return ("parse_error", [])

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        debug(f"decisions: JSON decode error: {exc}")
        return ("parse_error", [])

    status = data.get("decision_status")
    if status not in ("none", "present"):
        debug(f"decisions: invalid decision_status: {status!r}")
        return ("parse_error", [])

    if status == "none":
        return ("none", [])

    decisions = data.get("decisions")
    if not isinstance(decisions, list) or len(decisions) == 0:
        debug("decisions: decision_status is 'present' but decisions array empty or missing")
        return ("parse_error", [])

    validated: list[dict] = []
    for d in decisions:
        err = _validate_decision(d, existing_decisions)
        if err is not None:
            debug(f"decisions: validation failed for {d.get('id', '?')}: {err}")
            return ("parse_error", [])
        validated.append(d)

    return ("present", validated)


def strip_decisions_trailer(review_text: str) -> str:
    """Remove the ```decisions``` fenced block from review text."""
    return _FENCE_RE.sub("", review_text).rstrip()


def _validate_decision(d: dict, existing: dict[str, dict]) -> str | None:
    """Validate a single decision record.  Returns None on success, error string on failure."""
    # Required fields
    for field in ("id", "title", "summary", "options", "current_choice"):
        if field not in d:
            return f"missing required field: {field}"

    options = d["options"]
    if not isinstance(options, list) or len(options) == 0:
        return "options must be a non-empty array"

    option_keys = {o.get("key") for o in options}
    if d["current_choice"] not in option_keys:
        return f"current_choice {d['current_choice']!r} not in option keys {option_keys}"

    conflict_with = d.get("conflict_with")

    if conflict_with:
        # Conflict: referenced ID must be active or challenged
        target = existing.get(conflict_with)
        if target is None or target["state"] not in ("active", "challenged"):
            return (
                f"conflict_with references {conflict_with!r} which is not active or challenged"
            )
        # Every option must have effect field
        for o in options:
            effect = o.get("effect")
            if effect not in ("supersede", "keep_original"):
                return f"conflict option {o.get('key')!r} missing or invalid effect: {effect!r}"
    else:
        # Non-conflict: options must NOT have effect field
        for o in options:
            if "effect" in o:
                return f"non-conflict option {o.get('key')!r} has unexpected effect field"

    # ID collision rules
    did = d["id"]
    ex = existing.get(did)
    if ex is not None:
        if ex["state"] in ("active", "proposed") and not conflict_with:
            # Dedup candidate — handled by propose_decision(), not a parse error
            pass
        elif ex["state"] == "superseded":
            return f"ID {did!r} collides with superseded entry"

    return None
