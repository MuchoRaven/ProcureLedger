"""
test_sandbox_wiring.py
======================
Regression tests for the sandbox wiring.

These assert the properties the integration is supposed to guarantee, not the
sandbox's internals:

* ``agent.dispatch_tool`` routes through ``sandbox.dispatch``.
* Every allowlisted call is bracketed by ``TOOL_CALL_STARTED`` /
  ``TOOL_CALL_COMPLETED`` audit links.
* The agent allowlist and ``decisions.PROPOSAL_KINDS`` cannot drift.
* ``decisions.AMOUNT_TOLERANCE`` and ``tools.AMOUNT_TOLERANCE`` cannot drift.

The end-to-end tests need ``bwrap`` and ``prlimit`` on the host and are skipped
otherwise. Enforcing their presence in a deployment is the job of
``sandbox.preflight``, which runs from the app's startup hook.
"""

import shutil

import pytest

import agent
import config
import decisions
import sandbox
import tools


def _sandbox_available() -> bool:
    return all(shutil.which(name) for name in ("bwrap", "prlimit"))


requires_sandbox = pytest.mark.skipif(
    not _sandbox_available(), reason="bwrap/prlimit not installed on this host"
)


@pytest.fixture()
def clean_state(tmp_path, monkeypatch):
    """Point the audit log at a temp file and reset in-memory state."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    tools._reset_state()
    yield
    tools._reset_state()


def test_allowlist_matches_decisions():
    assert agent.ALLOWED_TOOLS == set(decisions.PROPOSAL_KINDS)


def test_tolerance_matches_tools():
    assert decisions.AMOUNT_TOLERANCE == tools.AMOUNT_TOLERANCE


def test_dispatch_tool_rejects_non_allowlisted(clean_state):
    result = agent.dispatch_tool(
        "approve_payout", {"approval_id": "APR-X", "approver": "alice"}, "cid"
    )
    assert result["ok"] is False
    assert result["error"] == "TOOL_NOT_ALLOWED"


@requires_sandbox
def test_sandbox_dispatch_read(clean_state):
    result = sandbox.dispatch(
        "get_invoice",
        {"invoice_id": "INV-2001"},
        principal=None,
        correlation_id="cid",
    )
    assert result["ok"] is True
    assert result["invoice"]["invoice_id"] == "INV-2001"


@requires_sandbox
def test_dispatch_tool_brackets_audit(clean_state):
    result = agent.dispatch_tool("get_invoice", {"invoice_id": "INV-2001"}, "cid-b")
    assert result["ok"] is True
    types = [entry["event_type"] for entry in tools._load_audit_from_file()]
    assert "TOOL_CALL_STARTED" in types
    assert "TOOL_CALL_COMPLETED" in types


@requires_sandbox
def test_dispatch_tool_reports_unknown_invoice(clean_state):
    result = agent.dispatch_tool("get_invoice", {"invoice_id": "INV-NOPE"}, "cid")
    assert result["ok"] is False
    assert result["error"] == "INVOICE_NOT_FOUND"
