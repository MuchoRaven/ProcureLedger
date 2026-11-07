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
