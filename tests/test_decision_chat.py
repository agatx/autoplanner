"""Tests for decision chat — slash-command parsing and discuss prompt formatting."""
import pytest

from autoplanner.output import _parse_decision_input


VALID_KEYS = ["A", "B", "C"]


class TestParseDecisionInput:
    """Test slash-command choice-vs-question detection."""

    # --- Slash-command choices ---

    def test_slash_key(self):
        assert _parse_decision_input("/A", VALID_KEYS) == ("A", "")

    def test_slash_key_lowercase(self):
        assert _parse_decision_input("/a", VALID_KEYS) == ("A", "")

    def test_slash_key_with_note(self):
        assert _parse_decision_input("/A prefer this", VALID_KEYS) == ("A", "prefer this")

    def test_slash_key_with_dash_dash_note(self):
        assert _parse_decision_input("/A -- prefer this", VALID_KEYS) == ("A", "prefer this")

    def test_slash_key_with_emdash_note(self):
        assert _parse_decision_input("/B \u2014 for performance", VALID_KEYS) == ("B", "for performance")

    def test_slash_skip(self):
        assert _parse_decision_input("/skip", VALID_KEYS) == ("skip", "")

    def test_slash_skip_uppercase(self):
        assert _parse_decision_input("/SKIP", VALID_KEYS) == ("skip", "")

    def test_slash_options(self):
        assert _parse_decision_input("/options", VALID_KEYS) == ("options", "")

    def test_slash_custom_with_text(self):
        assert _parse_decision_input("/custom Use Redis with TTL", VALID_KEYS) == ("custom", "Use Redis with TTL")

    def test_slash_custom_no_text(self):
        assert _parse_decision_input("/custom", VALID_KEYS) == ("custom", "")

    # --- Questions (no slash = question) ---

    def test_bare_text_is_question(self):
        result = _parse_decision_input("Why is option A better?", VALID_KEYS)
        assert result is None

    def test_sentence_starting_with_key_letter(self):
        """'A little bit of info' is a question, not a choice."""
        result = _parse_decision_input("A little bit of info about B", VALID_KEYS)
        assert result is None

    def test_bare_key_is_question(self):
        """Without slash, even 'A' alone is a question."""
        result = _parse_decision_input("A", VALID_KEYS)
        assert result is None

    def test_what_about_combining(self):
        result = _parse_decision_input("What happens if we combine both?", VALID_KEYS)
        assert result is None

    # --- Edge cases ---

    def test_slash_alone(self):
        result = _parse_decision_input("/", VALID_KEYS)
        assert result is None

    def test_slash_invalid_key(self):
        result = _parse_decision_input("/X", VALID_KEYS)
        assert result is None

    def test_empty_string(self):
        result = _parse_decision_input("", VALID_KEYS)
        assert result is None


class TestDiscussPromptFormatting:
    """Test that claude_agent.discuss formats prompts correctly."""

    def test_first_ask_includes_decision_context(self):
        from unittest.mock import MagicMock
        from autoplanner.agents.claude_agent import discuss

        session = MagicMock()
        session.send.return_value = "Here's my explanation..."

        decision = {
            "id": "d1",
            "title": "Cache strategy",
            "summary": "TTL vs event-driven",
            "options": [
                {"key": "A", "label": "TTL", "description": "Fixed expiry"},
                {"key": "B", "label": "Events", "description": "Event-driven"},
            ],
            "current_choice": "A",
        }

        discuss(session, decision, "Why TTL?", first_ask=True)

        prompt = session.send.call_args[0][0]
        assert "Cache strategy" in prompt
        assert "TTL vs event-driven" in prompt
        assert "[A] TTL" in prompt
        assert "[B] Events" in prompt
        assert "Why TTL?" in prompt

    def test_followup_skips_full_context(self):
        from unittest.mock import MagicMock
        from autoplanner.agents.claude_agent import discuss

        session = MagicMock()
        session.send.return_value = "More details..."

        decision = {
            "id": "d1",
            "title": "Cache strategy",
            "summary": "TTL vs event-driven",
            "options": [
                {"key": "A", "label": "TTL", "description": "Fixed expiry"},
            ],
            "current_choice": "A",
        }

        discuss(session, decision, "What about latency?", first_ask=False)

        prompt = session.send.call_args[0][0]
        assert "Cache strategy" in prompt
        assert "What about latency?" in prompt
        # Should NOT include the full options listing
        assert "[A] TTL" not in prompt
