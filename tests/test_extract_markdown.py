"""Tests for autoplanner.agents.claude_agent._extract_markdown."""

import pytest

from autoplanner.agents.claude_agent import _extract_markdown


class TestFencedMarkdown:
    def test_basic_fenced_block(self):
        text = "Here is the doc:\n```markdown\n# Title\n\nBody text.\n```\n"
        assert _extract_markdown(text) == "# Title\n\nBody text."

    def test_fenced_block_strips_preamble(self):
        text = (
            "Sure! I've drafted the document for you.\n\n"
            "```markdown\n# Design Doc\n\nContent here.\n```\n\n"
            "Let me know if you'd like changes."
        )
        assert _extract_markdown(text) == "# Design Doc\n\nContent here."

    def test_fenced_block_with_nested_code_fence(self):
        """Nested code blocks (e.g. ```python inside ```markdown) may break extraction.

        The current regex grabs until the first ```, so nested fences truncate.
        This test documents the actual behavior.
        """
        text = '```markdown\n# Doc\n\nExample:\n```python\nprint("hi")\n```\n\nMore text.\n```\n'
        result = _extract_markdown(text)
        # The regex stops at the first ``` after opening — it captures up to the nested fence
        assert result.startswith("# Doc")

    def test_fenced_block_extra_whitespace(self):
        text = "```markdown\n\n  # Title  \n\nBody.\n\n```"
        assert _extract_markdown(text).startswith("# Title")

    def test_multiple_fenced_blocks_returns_first(self):
        text = (
            "```markdown\n# First\n```\n\n"
            "```markdown\n# Second\n```\n"
        )
        assert _extract_markdown(text) == "# First"


class TestHeadingFallback:
    def test_heading_after_preamble(self):
        text = "Here's what I came up with:\n\n# My Document\n\nBody content."
        assert _extract_markdown(text) == "# My Document\n\nBody content."

    def test_heading_at_start(self):
        text = "# Already Clean\n\nNo preamble here."
        assert _extract_markdown(text) == "# Already Clean\n\nNo preamble here."

    def test_h2_heading_falls_through_to_plain(self):
        text = "Some preamble.\n\n## Section Title\n\nContent."
        # The heading regex `^(#\s+.+)$` only matches H1 (`# ...`), not H2+.
        # With no H1 or fenced block, the full text is returned as-is.
        result = _extract_markdown(text)
        assert result == text.strip()

    def test_heading_preserves_everything_after(self):
        text = "Ignore this.\n# Title\n\nParagraph 1.\n\n## Sub\n\nParagraph 2."
        result = _extract_markdown(text)
        assert result.startswith("# Title")
        assert "Paragraph 2." in result


class TestPlainFallback:
    def test_no_fence_no_heading(self):
        text = "Just some plain text with no structure."
        assert _extract_markdown(text) == "Just some plain text with no structure."

    def test_empty_string(self):
        assert _extract_markdown("") == ""

    def test_whitespace_only(self):
        assert _extract_markdown("   \n\n  ") == ""

    def test_strips_surrounding_whitespace(self):
        text = "\n\n  Some content  \n\n"
        assert _extract_markdown(text) == "Some content"
