"""Tests for decision state machine on History."""
import json
import pytest

from autoplanner.history import History, IterationRecord


def _make_history(tmp_path):
    """Create a minimal History bypassing __post_init__ (no file lock)."""
    work_dir = tmp_path / "test-run"
    work_dir.mkdir()
    h = object.__new__(History)
    h.task = "Test"
    h.run_id = "test-run"
    h.work_dir = work_dir
    h._lock_fd = None
    h.records = []
    h.decisions = {}
    return h


def _sample_decision(id="d1", **overrides):
    d = {
        "id": id,
        "title": "Cache strategy",
        "summary": "TTL vs event",
        "options": [
            {"key": "A", "label": "TTL", "pros": "simple", "cons": "stale"},
            {"key": "B", "label": "Event", "pros": "fresh", "cons": "complex"},
        ],
        "current_choice": "A",
    }
    d.update(overrides)
    return d


def _sample_resolution(**overrides):
    r = {
        "decision_id": "d1",
        "title": "Cache strategy",
        "options_presented": [{"key": "A", "label": "TTL"}, {"key": "B", "label": "Event"}],
        "chosen_key": "B",
        "chosen_label": "Event",
        "chosen_effect": None,
        "note": "",
        "locked_direction": "Use Event.",
    }
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------
# propose_decision
# ---------------------------------------------------------------------------

class TestProposeDecision:
    def test_normal_proposal(self, tmp_path):
        h = _make_history(tmp_path)
        assert h.propose_decision(_sample_decision()) is True
        assert "d1" in h.decisions
        assert h.decisions["d1"]["state"] == "proposed"

    def test_dedup_active(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        h.lock_decision("d1", _sample_resolution())
        assert h.decisions["d1"]["state"] == "active"
        # Re-propose same ID without conflict_with → dedup no-op
        assert h.propose_decision(_sample_decision()) is False

    def test_dedup_proposed(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        # Re-propose while still proposed → no-op
        assert h.propose_decision(_sample_decision()) is False

    def test_conflict_transitions_original_to_challenged(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        h.lock_decision("d1", _sample_resolution())
        assert h.decisions["d1"]["state"] == "active"

        conflict = _sample_decision(
            id="d1-v2",
            conflict_with="d1",
            options=[
                {"key": "A", "label": "Revert", "effect": "supersede", "pros": "", "cons": ""},
                {"key": "B", "label": "Keep", "effect": "keep_original", "pros": "", "cons": ""},
            ],
        )
        assert h.propose_decision(conflict) is True
        assert h.decisions["d1"]["state"] == "challenged"
        assert h.decisions["d1-v2"]["state"] == "proposed"

    def test_conflict_with_nonexistent_raises(self, tmp_path):
        h = _make_history(tmp_path)
        conflict = _sample_decision(id="d1-v2", conflict_with="d999")
        with pytest.raises(ValueError, match="d999"):
            h.propose_decision(conflict)

    def test_collision_with_superseded_raises(self, tmp_path):
        h = _make_history(tmp_path)
        # Manually set up a superseded entry
        h.decisions["d1"] = {"state": "superseded"}
        with pytest.raises(ValueError, match="superseded"):
            h.propose_decision(_sample_decision())


# ---------------------------------------------------------------------------
# lock_decision
# ---------------------------------------------------------------------------

class TestLockDecision:
    def test_basic_lock(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        h.lock_decision("d1", _sample_resolution())
        assert h.decisions["d1"]["state"] == "active"
        assert h.decisions["d1"]["resolution"]["chosen_key"] == "B"
        assert h.decisions["d1"]["resolution"]["locked_direction"] == "Use Event."

    def test_idempotent_lock(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        res = _sample_resolution()
        h.lock_decision("d1", res)
        # Locking again with same resolution is a no-op
        h.lock_decision("d1", res)
        assert h.decisions["d1"]["state"] == "active"

    def test_supersede_conflict(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        h.lock_decision("d1", _sample_resolution())

        conflict = _sample_decision(
            id="d1-v2",
            conflict_with="d1",
            options=[
                {"key": "A", "label": "Revert", "effect": "supersede", "pros": "", "cons": ""},
                {"key": "B", "label": "Keep", "effect": "keep_original", "pros": "", "cons": ""},
            ],
        )
        h.propose_decision(conflict)
        h.lock_decision("d1-v2", _sample_resolution(
            decision_id="d1-v2",
            chosen_key="A",
            chosen_label="Revert",
            chosen_effect="supersede",
            locked_direction="Use Revert.",
        ))
        assert h.decisions["d1"]["state"] == "superseded"
        assert h.decisions["d1"]["superseded_by"] == "d1-v2"
        assert h.decisions["d1-v2"]["state"] == "active"

    def test_keep_original_conflict(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        h.lock_decision("d1", _sample_resolution())

        conflict = _sample_decision(
            id="d1-v2",
            conflict_with="d1",
            options=[
                {"key": "A", "label": "Revert", "effect": "supersede", "pros": "", "cons": ""},
                {"key": "B", "label": "Keep", "effect": "keep_original", "pros": "", "cons": ""},
            ],
        )
        h.propose_decision(conflict)
        h.lock_decision("d1-v2", _sample_resolution(
            decision_id="d1-v2",
            chosen_key="B",
            chosen_label="Keep",
            chosen_effect="keep_original",
            locked_direction="Use Keep.",
        ))
        # Original restored to active, conflict proposal is superseded (it lost)
        assert h.decisions["d1"]["state"] == "active"
        assert h.decisions["d1-v2"]["state"] == "superseded"


# ---------------------------------------------------------------------------
# active_decisions / has_proposed / pending_decisions
# ---------------------------------------------------------------------------

class TestQueryMethods:
    def test_active_decisions(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision("d1"))
        h.lock_decision("d1", _sample_resolution())
        h.propose_decision(_sample_decision("d2"))
        # d1 is active, d2 is proposed
        active = h.active_decisions()
        assert len(active) == 1
        assert active[0]["id"] == "d1"

    def test_active_includes_challenged(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision("d1"))
        h.lock_decision("d1", _sample_resolution())
        conflict = _sample_decision(
            id="d1-v2",
            conflict_with="d1",
            options=[
                {"key": "A", "label": "Revert", "effect": "supersede", "pros": "", "cons": ""},
            ],
        )
        h.propose_decision(conflict)
        active = h.active_decisions()
        ids = {d["id"] for d in active}
        assert "d1" in ids  # challenged
        assert "d1-v2" not in ids  # proposed, not active

    def test_has_proposed(self, tmp_path):
        h = _make_history(tmp_path)
        assert h.has_proposed() is False
        h.propose_decision(_sample_decision())
        assert h.has_proposed() is True
        h.lock_decision("d1", _sample_resolution())
        assert h.has_proposed() is False

    def test_pending_decisions(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision("d1"))
        h.propose_decision(_sample_decision("d2"))
        pending = h.pending_decisions()
        assert len(pending) == 2


# ---------------------------------------------------------------------------
# JSON round-trip with decisions
# ---------------------------------------------------------------------------

class TestDecisionsJsonRoundTrip:
    def test_save_and_load_with_decisions(self, tmp_path):
        h = _make_history(tmp_path)
        h.propose_decision(_sample_decision())
        h.lock_decision("d1", _sample_resolution())

        loaded = History.from_directory(h.work_dir, lock=False)
        assert len(loaded.decisions) == 1
        assert loaded.decisions["d1"]["state"] == "active"
        assert loaded.decisions["d1"]["resolution"]["chosen_key"] == "B"

    def test_backward_compat_no_decisions_key(self, tmp_path):
        """Old history.json without a 'decisions' key loads with empty dict."""
        work_dir = tmp_path / "old-run"
        work_dir.mkdir()
        data = {
            "task": "Old task",
            "run_id": "old-run",
            "records": [],
        }
        (work_dir / "history.json").write_text(json.dumps(data))
        loaded = History.from_directory(work_dir, lock=False)
        assert loaded.decisions == {}
