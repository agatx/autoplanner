"""Tests for autoplanner.decisions — trailer extraction and validation."""
import pytest

from autoplanner.decisions import extract_decisions, strip_decisions_trailer


# ---------------------------------------------------------------------------
# extract_decisions — happy paths
# ---------------------------------------------------------------------------

class TestExtractDecisionsHappy:
    def test_status_none(self):
        review = 'Good doc.\n\n```decisions\n{"decision_status": "none", "decisions": []}\n```'
        status, decisions = extract_decisions(review, {})
        assert status == "none"
        assert decisions == []

    def test_status_present_single_decision(self):
        review = (
            "Some review text.\n\n"
            '```decisions\n'
            '{\n'
            '  "decision_status": "present",\n'
            '  "decisions": [\n'
            '    {\n'
            '      "id": "d1",\n'
            '      "title": "Cache strategy",\n'
            '      "summary": "TTL vs event-driven",\n'
            '      "options": [\n'
            '        {"key": "A", "label": "TTL", "pros": "simple", "cons": "stale"},\n'
            '        {"key": "B", "label": "Event", "pros": "fresh", "cons": "complex"}\n'
            '      ],\n'
            '      "current_choice": "A"\n'
            '    }\n'
            '  ]\n'
            '}\n'
            '```'
        )
        status, decisions = extract_decisions(review, {})
        assert status == "present"
        assert len(decisions) == 1
        assert decisions[0]["id"] == "d1"
        assert decisions[0]["current_choice"] == "A"

    def test_conflict_decision_valid(self):
        existing = {
            "d1": {"state": "active", "title": "Cache strategy"},
        }
        review = (
            "Review.\n\n"
            '```decisions\n'
            '{\n'
            '  "decision_status": "present",\n'
            '  "decisions": [\n'
            '    {\n'
            '      "id": "d1-v2",\n'
            '      "title": "Cache conflict",\n'
            '      "summary": "New issue found",\n'
            '      "conflict_with": "d1",\n'
            '      "options": [\n'
            '        {"key": "A", "label": "Revert", "effect": "supersede", "pros": "x", "cons": "y"},\n'
            '        {"key": "B", "label": "Keep", "effect": "keep_original", "pros": "a", "cons": "b"}\n'
            '      ],\n'
            '      "current_choice": "A"\n'
            '    }\n'
            '  ]\n'
            '}\n'
            '```'
        )
        status, decisions = extract_decisions(review, existing)
        assert status == "present"
        assert decisions[0]["conflict_with"] == "d1"

    def test_dedup_candidate_still_returns_present(self):
        """A decision with ID matching an existing active entry (no conflict_with) is a dedup
        candidate. extract_decisions still returns it — propose_decision handles the no-op."""
        existing = {"d1": {"state": "active"}}
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{'
            '"id": "d1", "title": "T", "summary": "S", '
            '"options": [{"key": "A", "label": "L", "pros": "", "cons": ""}], '
            '"current_choice": "A"'
            '}]}\n```'
        )
        status, decisions = extract_decisions(review, existing)
        assert status == "present"


# ---------------------------------------------------------------------------
# extract_decisions — parse errors
# ---------------------------------------------------------------------------

class TestExtractDecisionsErrors:
    def test_no_trailer(self):
        status, decisions = extract_decisions("Just a plain review.", {})
        assert status == "parse_error"
        assert decisions == []

    def test_malformed_json(self):
        review = '```decisions\n{not valid json}\n```'
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_invalid_decision_status(self):
        review = '```decisions\n{"decision_status": "maybe", "decisions": []}\n```'
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_present_but_empty_decisions(self):
        review = '```decisions\n{"decision_status": "present", "decisions": []}\n```'
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_missing_required_field(self):
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{"id": "d1"}]}\n'
            '```'
        )
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_current_choice_not_in_options(self):
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{'
            '"id": "d1", "title": "T", "summary": "S", '
            '"options": [{"key": "A", "label": "L", "pros": "", "cons": ""}], '
            '"current_choice": "Z"'
            '}]}\n```'
        )
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_conflict_missing_effect(self):
        existing = {"d1": {"state": "active"}}
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{'
            '"id": "d1-v2", "title": "T", "summary": "S", '
            '"conflict_with": "d1", '
            '"options": [{"key": "A", "label": "L", "pros": "", "cons": ""}], '
            '"current_choice": "A"'
            '}]}\n```'
        )
        status, _ = extract_decisions(review, existing)
        assert status == "parse_error"

    def test_conflict_references_nonexistent(self):
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{'
            '"id": "d1-v2", "title": "T", "summary": "S", '
            '"conflict_with": "d999", '
            '"options": [{"key": "A", "label": "L", "effect": "supersede", "pros": "", "cons": ""}], '
            '"current_choice": "A"'
            '}]}\n```'
        )
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_non_conflict_with_effect_field(self):
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{'
            '"id": "d1", "title": "T", "summary": "S", '
            '"options": [{"key": "A", "label": "L", "effect": "supersede", "pros": "", "cons": ""}], '
            '"current_choice": "A"'
            '}]}\n```'
        )
        status, _ = extract_decisions(review, {})
        assert status == "parse_error"

    def test_id_collision_with_superseded(self):
        existing = {"d1": {"state": "superseded"}}
        review = (
            '```decisions\n'
            '{"decision_status": "present", "decisions": [{'
            '"id": "d1", "title": "T", "summary": "S", '
            '"options": [{"key": "A", "label": "L", "pros": "", "cons": ""}], '
            '"current_choice": "A"'
            '}]}\n```'
        )
        status, _ = extract_decisions(review, existing)
        assert status == "parse_error"


# ---------------------------------------------------------------------------
# strip_decisions_trailer
# ---------------------------------------------------------------------------

class TestStripDecisionsTrailer:
    def test_removes_trailer(self):
        review = 'Some review.\n\n```decisions\n{"decision_status": "none", "decisions": []}\n```'
        stripped = strip_decisions_trailer(review)
        assert "decisions" not in stripped
        assert "Some review." in stripped

    def test_no_trailer_returns_unchanged(self):
        review = "Just a plain review."
        assert strip_decisions_trailer(review) == review

    def test_preserves_text_around_trailer(self):
        review = 'Before.\n\n```decisions\n{"x": 1}\n```\n\nAfter.'
        stripped = strip_decisions_trailer(review)
        assert "Before." in stripped
        assert "After." in stripped
        assert "```decisions" not in stripped
