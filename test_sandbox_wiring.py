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
* A failed audit write leaves business state unchanged (audit-before-mutation).

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


def _fail_audit_write(entry):
    raise tools.ToolError("AUDIT_WRITE_FAILED", "simulated disk failure")


def test_audit_write_failure_leaves_match_state_unchanged(clean_state, monkeypatch):
    """A failed audit write must not leave an unrecorded state change.

    ``_apply_transition`` is the path ``sandbox.dispatch`` uses, so this covers
    the sandbox apply helper as well as ``run_three_way_match`` itself.
    """
    monkeypatch.setattr(tools, "_persist_audit", _fail_audit_write)

    result = tools._apply_transition(
        {"kind": "transition", "invoice_id": "INV-2001", "po_id": "PO-1001"},
        "cid-fail",
    )
    assert result["ok"] is False
    assert result["error"] == "AUDIT_WRITE_FAILED"

    invoice = tools.get_invoice("INV-2001")["invoice"]
    assert invoice["status"] == "RECEIVED"


def test_audit_write_failure_leaves_ingest_state_unchanged(clean_state, monkeypatch):
    """``ingest_invoice`` must not create the record if the audit write fails."""
    monkeypatch.setattr(tools, "_persist_audit", _fail_audit_write)

    with pytest.raises(tools.ToolError) as excinfo:
        tools.ingest_invoice("INV-9001", "VEND-001", "PO-1001", 12500.00, "USD", "cid")
    assert excinfo.value.code == "AUDIT_WRITE_FAILED"

    assert tools.get_invoice("INV-9001")["ok"] is False


def test_audit_write_failure_leaves_approval_state_unchanged(clean_state, monkeypatch):
    """``request_payout_approval`` must not open a gate if the audit write fails."""
    # Move INV-2001 to MATCHED first so the approval gate is reachable.
    assert tools.run_three_way_match("INV-2001", "PO-1001", "cid-setup")["ok"] is True

    monkeypatch.setattr(tools, "_persist_audit", _fail_audit_write)

    result = tools._apply_create_approval(
        {
            "kind": "create_approval",
            "vendor_id": "VEND-001",
            "amount": 12500.00,
            "po_id": "PO-1001",
            "invoice_id": "INV-2001",
        },
        "cid-fail",
    )
    assert result["ok"] is False
    assert result["error"] == "AUDIT_WRITE_FAILED"

    invoice = tools.get_invoice("INV-2001")["invoice"]
    assert invoice["status"] == "MATCHED"
    assert tools._APPROVALS == {}
