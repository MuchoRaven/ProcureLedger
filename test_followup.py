"""
test_followup.py
================
Tests for ``tool_modules._followup``: late-PO flagging, delay notification,
and the late-PO worklist.
"""

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
