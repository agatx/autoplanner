"""Tests for autoplanner.history — slugify, find_run_dir, History methods."""

import json

import pytest

from autoplanner.history import (
    _slugify,
    find_run_dir,
    History,
    IterationRecord,
    make_run_id,
    make_output_name,
)


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert _slugify("Design a caching layer") == "design-a-caching-layer"

    def test_special_characters_stripped(self):
        assert _slugify("Hello, World! (v2)") == "hello-world-v2"

    def test_unicode_normalized(self):
        assert _slugify("cafe\u0301 latte") == "cafe-latte"

    def test_collapses_whitespace_and_hyphens(self):
        assert _slugify("too   many---dashes") == "too-many-dashes"

    def test_truncation(self):
        long_text = "a" * 100
        assert len(_slugify(long_text)) == 50

    def test_custom_max_len(self):
        assert len(_slugify("some text here", max_len=5)) <= 5

    def test_empty_string(self):
        assert _slugify("") == ""

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("--hello--") == "hello"


# ---------------------------------------------------------------------------
# make_run_id / make_output_name
# ---------------------------------------------------------------------------

class TestMakeRunId:
    def test_contains_slug_and_timestamp(self):
        rid = make_run_id("My Task")
        assert rid.startswith("my-task-")
        # Timestamp portion: YYYYMMDD-HHMMSS
        parts = rid.split("-")
        assert len(parts) >= 3

    def test_output_name_has_suffix(self):
        name = make_output_name("My Task", "requirements")
        assert name.endswith("-requirements.md")
        assert name.startswith("my-task-")


# ---------------------------------------------------------------------------
# find_run_dir
# ---------------------------------------------------------------------------

def _make_run(base, name: str, with_history: bool = True) -> None:
    d = base / name
    d.mkdir()
    if with_history:
        (d / "history.json").write_text("{}")


class TestFindRunDir:
    def test_last_returns_most_recent(self, tmp_path):
        _make_run(tmp_path, "run-a")
        _make_run(tmp_path, "run-b")
        # Touch run-b to make it newer
        (tmp_path / "run-b" / "history.json").write_text("{}")
        result = find_run_dir(tmp_path, None)
        assert result.name == "run-b"

    def test_latest_sentinel(self, tmp_path):
        _make_run(tmp_path, "run-a")
        result = find_run_dir(tmp_path, "last")
        assert result.name == "run-a"

    def test_exact_match(self, tmp_path):
        _make_run(tmp_path, "caching-layer-20260410")
        _make_run(tmp_path, "auth-system-20260410")
        result = find_run_dir(tmp_path, "caching-layer-20260410")
        assert result.name == "caching-layer-20260410"

    def test_substring_match_unique(self, tmp_path):
        _make_run(tmp_path, "caching-layer-20260410")
        _make_run(tmp_path, "auth-system-20260410")
        result = find_run_dir(tmp_path, "caching")
        assert result.name == "caching-layer-20260410"

    def test_substring_match_ambiguous_raises(self, tmp_path):
        _make_run(tmp_path, "caching-v1-20260410")
        _make_run(tmp_path, "caching-v2-20260410")
        with pytest.raises(ValueError, match="Ambiguous"):
            find_run_dir(tmp_path, "caching")

    def test_no_match_raises(self, tmp_path):
        _make_run(tmp_path, "caching-layer-20260410")
        with pytest.raises(FileNotFoundError, match="No run matching"):
            find_run_dir(tmp_path, "nonexistent")

    def test_empty_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No completed runs"):
            find_run_dir(tmp_path, None)

    def test_dir_without_history_json_ignored(self, tmp_path):
        _make_run(tmp_path, "incomplete-run", with_history=False)
        _make_run(tmp_path, "complete-run", with_history=True)
        result = find_run_dir(tmp_path, None)
        assert result.name == "complete-run"


# ---------------------------------------------------------------------------
# History.last_document_and_review
# ---------------------------------------------------------------------------

class TestLastDocumentAndReview:
    @staticmethod
    def _records(*phases):
        return [
            IterationRecord(iteration=i + 1, phase=p, author="claude", content=f"{p}-{i + 1}")
            for i, p in enumerate(phases)
        ]

    def test_draft_and_review(self):
        h = object.__new__(History)
        h.records = self._records("draft", "review")
        doc, rev = h.last_document_and_review()
        assert doc == "draft-1"
        assert rev == "review-2"

    def test_multiple_iterations(self):
        h = object.__new__(History)
        h.records = self._records("draft", "review", "revision", "review")
        doc, rev = h.last_document_and_review()
        assert doc == "revision-3"
        assert rev == "review-4"

    def test_draft_only_no_review(self):
        h = object.__new__(History)
        h.records = self._records("draft")
        doc, rev = h.last_document_and_review()
        assert doc == "draft-1"
        assert rev == ""

    def test_empty_records(self):
        h = object.__new__(History)
        h.records = []
        doc, rev = h.last_document_and_review()
        assert doc == ""
        assert rev == ""


# ---------------------------------------------------------------------------
# History JSON round-trip
# ---------------------------------------------------------------------------

class TestHistoryJson:
    def test_save_and_load_roundtrip(self, tmp_path):
        work_dir = tmp_path / "test-run"
        work_dir.mkdir()
        # Create history without locking (bypass __post_init__)
        h = object.__new__(History)
        h.task = "Test task"
        h.run_id = "test-run-20260410"
        h.work_dir = work_dir
        h._lock_fd = None
        h.records = [
            IterationRecord(iteration=1, phase="draft", author="claude", content="Draft v1"),
            IterationRecord(iteration=1, phase="review", author="codex", content="Needs work"),
            IterationRecord(iteration=2, phase="revision", author="claude", content="Draft v2"),
        ]

        h.save_json()

        loaded = History.from_directory(work_dir, lock=False)
        assert loaded.task == "Test task"
        assert loaded.run_id == "test-run-20260410"
        assert len(loaded.records) == 3
        assert loaded.records[0].phase == "draft"
        assert loaded.records[0].content == "Draft v1"
        assert loaded.records[1].author == "codex"
        assert loaded.records[2].phase == "revision"


# ---------------------------------------------------------------------------
# build_iteration_history
# ---------------------------------------------------------------------------

class TestBuildIterationHistory:
    def test_formats_all_records(self):
        h = object.__new__(History)
        h.records = [
            IterationRecord(iteration=1, phase="draft", author="claude", content="Doc v1"),
            IterationRecord(iteration=1, phase="review", author="codex", content="Fix X"),
        ]
        result = h.build_iteration_history()
        assert "### Iteration 1 — Draft (by claude)" in result
        assert "Doc v1" in result
        assert "### Iteration 1 — Review (by codex)" in result
        assert "Fix X" in result

    def test_empty_records(self):
        h = object.__new__(History)
        h.records = []
        assert h.build_iteration_history() == ""
