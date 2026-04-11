"""Tests for session-level helpers and prompt utilities."""

import pytest

from autoplanner.agents.session import _is_transient
from autoplanner.prompts import steering_block


# ---------------------------------------------------------------------------
# _is_transient
# ---------------------------------------------------------------------------

class TestIsTransient:
    @pytest.mark.parametrize("msg", [
        "Service overloaded, try again",
        "Error 529: server busy",
        "HTTP 503 Service Unavailable",
        "too many requests",
        "at capacity",
        "temporarily unavailable",
    ])
    def test_transient_errors(self, msg):
        assert _is_transient(msg) is True

    @pytest.mark.parametrize("msg", [
        "invalid API key",
        "model not found",
        "permission denied",
        "malformed request body",
        "",
    ])
    def test_permanent_errors(self, msg):
        assert _is_transient(msg) is False

    def test_case_insensitive(self):
        assert _is_transient("OVERLOADED") is True
        assert _is_transient("Too Many Requests") is True


# ---------------------------------------------------------------------------
# steering_block
# ---------------------------------------------------------------------------

class TestSteeringBlock:
    def test_none_returns_empty(self):
        assert steering_block(None) == ""

    def test_empty_string_returns_empty(self):
        assert steering_block("") == ""

    def test_content_returns_formatted_block(self):
        result = steering_block("focus on performance")
        assert "## Author's Guidance" in result
        assert "focus on performance" in result

    def test_block_starts_with_newlines(self):
        result = steering_block("anything")
        assert result.startswith("\n\n")
