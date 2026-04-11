"""Tests for autoplanner.orchestrator.is_done."""

import pytest

from autoplanner.output import set_writer


class _NullWriter:
    """Absorbs all output — used to avoid print side effects in tests."""
    def write(self, text: str) -> None: pass
    def write_thinking(self, text: str) -> None: pass
    def write_status(self, text: str) -> None: pass
    def thinking_start(self, label: str) -> None: pass
    def thinking_end(self) -> None: pass


@pytest.fixture(autouse=True)
def _null_writer():
    set_writer(_NullWriter())
    yield
    # Restore default so other test modules aren't affected
    from autoplanner.output import TerminalWriter
    set_writer(TerminalWriter())


from autoplanner.orchestrator import is_done


class TestIsDone:
    def test_lgtm_exact(self):
        assert is_done("LGTM", iteration=1, max_iterations=5) is True

    def test_lgtm_with_trailing_text(self):
        assert is_done("LGTM with minor nits addressed.", iteration=1, max_iterations=5) is True

    def test_lgtm_case_insensitive(self):
        assert is_done("lgtm", iteration=1, max_iterations=5) is True

    def test_lgtm_with_whitespace(self):
        assert is_done("  LGTM  ", iteration=1, max_iterations=5) is True

    def test_not_lgtm_when_mid_text(self):
        assert is_done("Almost LGTM but not quite.", iteration=1, max_iterations=5) is False

    def test_max_iterations_reached(self):
        assert is_done("Needs more work.", iteration=5, max_iterations=5) is True

    def test_max_iterations_exceeded(self):
        assert is_done("Needs more work.", iteration=6, max_iterations=5) is True

    def test_normal_review_not_done(self):
        assert is_done("Please fix sections 2 and 3.", iteration=2, max_iterations=5) is False

    def test_empty_review_not_done(self):
        assert is_done("", iteration=1, max_iterations=5) is False
