"""
test_followup.py
================
Tests for ``tool_modules._followup``: late-PO flagging, delay notification,
and the late-PO worklist.
"""

# Seed facts (from ``tool_modules._state._seed_state``):
#
# * ``_reset_state()`` reseeds ``PO-1001`` (``expected_delivery: "2024-01-20"``,
#   status ``OPEN``) and ``PO-1002`` (``expected_delivery: "2024-02-10"``,
#   status ``OPEN``).
# * Both are past ``2024-03-01`` and not past ``2024-01-01``.
# * ``_reset_state()`` truncates the audit log, so ``_events(...)`` starts
#   empty.
# * Every test must pin ``as_of`` explicitly; an unpinned test sees the two
#   seed POs as late and pollutes the audit log.

import pytest

import config
import tools
from tool_modules._followup import (
    run_followup,
    _apply_flag_late,
    _apply_notify_delay,
    list_late_pos,
)


@pytest.fixture()
def clean_state(tmp_path, monkeypatch):
    """Point the audit log at a temp file and reset in-memory state."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    tools._reset_state()
    yield
    tools._reset_state()


def _events(event_type):
    return [e for e in tools._load_audit_from_file() if e["event_type"] == event_type]


def test_run_followup_flags_both_seed_pos(clean_state):
    """Both seed POs are past 2024-03-01, so both are flagged in one pass."""
    result = run_followup("2024-03-01")

    assert result["ok"] is True
    assert sorted(result["flagged"]) == ["PO-1001", "PO-1002"]

    events = _events("PO_LATE")
    assert len(events) == 2
    assert all(entry["source"] == "tool" for entry in events)


def test_run_followup_is_idempotent(clean_state):
    """A second pass flags nothing and does not duplicate the audit entries."""
    run_followup("2024-03-01")
    result = run_followup("2024-03-01")

    assert result["flagged"] == []
    # ``result["skipped"]`` is also ``[]`` on the second run, because
    # ``_late_pos`` filters already-flagged POs out before ``_apply_flag_late``
    # is called. Do not assert ``skipped == ["PO-1001", "PO-1002"]``.
    assert len(_events("PO_LATE")) == 2


def test_run_followup_before_due_dates_flags_nothing(clean_state):
    """Neither seed PO is late as of 2024-01-01, so nothing is flagged."""
    result = run_followup("2024-01-01")

    assert result["flagged"] == []
    assert _events("PO_LATE") == []


def test_empty_expected_delivery_is_never_flagged(clean_state):
    """An unscheduled PO (``expected_delivery == ""``) is never flagged late.

    ``"" < "2024-03-01"`` is True lexicographically, so without the
    empty-string guard in ``_is_late`` this PO would be flagged on every run.
    """
    with tools._LOCK:
        tools._POS["PO-1001"]["expected_delivery"] = ""

    result = run_followup("2024-03-01")

    assert "PO-1001" not in result["flagged"]
    assert "PO-1002" in result["flagged"]


def test_closed_po_is_never_flagged(clean_state):
    """A CLOSED PO is never flagged late, even if its delivery date has passed."""
    with tools._LOCK:
        tools._POS["PO-1001"]["status"] = "CLOSED"

    result = run_followup("2024-03-01")

    assert "PO-1001" not in result["flagged"]
    assert "PO-1002" in result["flagged"]


def test_notify_delay_on_unflagged_po_returns_po_not_late(clean_state):
    """Notifying on a PO that was never flagged is refused, not applied.

    ``_apply_notify_delay`` returns the error dict; it does not raise. The
    ``check`` callable raises ``PO_NOT_LATE`` because ``late_flagged_at`` is
    unset, so the mutation never runs and no ``delay_notified_at`` is written.
    """
    result = _apply_notify_delay("PO-1001", "alice", "cid")

    assert result["ok"] is False
    assert result["error"] == "PO_NOT_LATE"
    assert "delay_notified_at" not in tools._POS["PO-1001"]


def test_notify_delay_writes_event_and_is_idempotent(clean_state):
    """A flagged PO can be notified once; a second call is a skip, not a write."""
    _apply_flag_late("PO-1001", "2024-03-01", "cid")

    result = _apply_notify_delay("PO-1001", "alice", "cid")

    assert result["ok"] is True
    assert tools._POS["PO-1001"]["delay_notified_by"] == "alice"
    assert len(_events("DELAY_NOTIFIED")) == 1

    result2 = _apply_notify_delay("PO-1001", "alice", "cid")

    assert result2["ok"] is True
    assert result2["skipped"] is True
    assert len(_events("DELAY_NOTIFIED")) == 1


def test_list_late_pos_reports_days_late_and_includes_notified(clean_state):
    """The worklist reports ``days_late`` and keeps already-notified POs.

    ``list_late_pos`` answers "what is late", not "what is newly late", so a
    PO that has been flagged and notified is still listed. ``days_late`` is
    computed against the pinned ``as_of``, not today.
    """
    _apply_flag_late("PO-1001", "2024-03-01", "cid")
    _apply_notify_delay("PO-1001", "alice", "cid")

    result = list_late_pos("2024-03-01")

    assert result["ok"] is True

    by_id = {po["po_id"]: po for po in result["pos"]}

    # 2024-03-01 - 2024-01-20 = 41 days (2024 is a leap year).
    assert by_id["PO-1001"]["days_late"] == 41
    # 2024-03-01 - 2024-02-10 = 20 days.
    assert by_id["PO-1002"]["days_late"] == 20

    # Already-notified POs are still included.
    assert "PO-1001" in by_id
