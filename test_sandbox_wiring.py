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
* The authenticated principal is threaded into the sandbox, never taken from
  the model's tool arguments.

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


def _events(event_type):
    """Return the durable audit entries of one type."""
    return [
        entry
        for entry in tools._load_audit_from_file()
        if entry["event_type"] == event_type
    ]


def _call(fn, *args):
    """Call a tool function, normalising a raised ``ToolError`` to a result.

    The tool surface is mixed: some entry points return ``{"ok": False, ...}``
    and some raise ``ToolError``. Tests should assert on the outcome, not on
    which of the two styles a given function happens to use.
    """
    try:
        return fn(*args)
    except tools.ToolError as exc:
        return {"ok": False, "error": exc.code, "detail": exc.detail}


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


def test_dispatch_tool_forwards_principal_and_strips_model_args(
    clean_state, monkeypatch
):
    """The principal comes from the HTTP layer, never from the model's args.

    ``dispatch_tool`` must forward the authenticated ``Principal`` and the
    loop's ``correlation_id`` to ``sandbox.dispatch``, and must drop any
    ``principal``/``correlation_id`` the model put in its tool arguments so a
    prompt-injected call cannot impersonate a caller or forge a trace ID.
    """
    captured = {}

    def fake_dispatch(tool, arguments, principal=None, correlation_id=""):
        captured["tool"] = tool
        captured["arguments"] = arguments
        captured["principal"] = principal
        captured["correlation_id"] = correlation_id
        return {"ok": True, "invoice": {"invoice_id": "INV-2001"}}

    monkeypatch.setattr(sandbox, "dispatch", fake_dispatch)

    principal = tools.Principal(sub="agent", roles=frozenset({"agent"}))
    result = agent.dispatch_tool(
        "get_invoice",
        {
            "invoice_id": "INV-2001",
            "principal": "attacker",
            "correlation_id": "attacker-cid",
        },
        "cid-real",
        principal,
    )

    assert result["ok"] is True
    assert captured["tool"] == "get_invoice"
    # Identity, not equality: the exact object from the HTTP layer is forwarded.
    assert captured["principal"] is principal
    # The loop's correlation ID wins over the model-supplied one.
    assert captured["correlation_id"] == "cid-real"
    # The model's principal/correlation_id never reach the sandbox.
    assert captured["arguments"] == {"invoice_id": "INV-2001"}


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


# ---------------------------------------------------------------------------
# Procurement lifecycle: PO / receipt / requisition
# ---------------------------------------------------------------------------


def test_create_po_emits_po_created(clean_state):
    """Creating a PO records a ``PO_CREATED`` link attributed to the tool."""
    result = _call(
        tools.create_po,
        "VEND-001",
        500.00,
        "USD",
        "2024-03-01",
        None,
        "cid-po",
    )
    assert result["ok"] is True
    po_id = result["po"]["po_id"]
    assert po_id in tools._POS

    events = _events("PO_CREATED")
    assert len(events) == 1
    assert events[0]["source"] == "tool"
    assert events[0]["payload"]["po_id"] == po_id


def test_create_po_converts_requisition(clean_state):
    """A PO raised against an open requisition converts it and links it back."""
    result = _call(
        tools.create_po,
        "VEND-001",
        800.00,
        "USD",
        "2024-03-01",
        "REQ-4002",
        "cid-req",
    )
    assert result["ok"] is True
    po_id = result["po"]["po_id"]

    requisition = tools._REQUISITIONS["REQ-4002"]
    assert requisition["status"] == "CONVERTED"
    assert requisition["po_id"] == po_id


def test_record_receipt_emits_goods_receipt(clean_state):
    """A receipt against an open PO records a ``GOODS_RECEIPT_RECORDED`` link.

    The seed POs are both fully received, so this raises a fresh PO first.
    """
    assert (
        _call(
            tools.create_po,
            "PO-9003",
            "VEND-001",
            500.00,
            "USD",
            "2024-03-01",
            None,
            "cid-po",
        )["ok"]
        is True
    )

    result = _call(tools.record_receipt, "RCPT-9001", "PO-9003", 500.00, "cid-rcpt")
    assert result["ok"] is True
    assert "RCPT-9001" in tools._RECEIPTS

    events = _events("GOODS_RECEIPT_RECORDED")
    assert len(events) == 1
    assert events[0]["source"] == "tool"


def test_record_receipt_rejects_over_receipt(clean_state):
    """Receiving more than the PO is worth is refused, and nothing is stored.

    ``PO-1002`` is already fully covered by ``RCPT-3002``.
    """
    result = _call(tools.record_receipt, "RCPT-9002", "PO-1002", 100.00, "cid-over")
    assert result["ok"] is False
    assert result["error"] == "RECEIPT_EXCEEDS_PO"
    assert "RCPT-9002" not in tools._RECEIPTS


def test_po_status_change_emits_event(clean_state):
    """Closing a PO records a ``PO_STATUS_CHANGED`` link and moves the status."""
    result = tools._apply_transition(
        {"kind": "transition", "po_id": "PO-1001", "status": "CLOSED"},
        "cid-close",
    )
    assert result["ok"] is True
    assert tools._POS["PO-1001"]["status"] == "CLOSED"

    events = _events("PO_STATUS_CHANGED")
    assert len(events) == 1
    assert events[0]["source"] == "tool"


def test_create_requisition_emits_event(clean_state):
    """Raising a requisition records a ``REQUISITION_CREATED`` link."""
    result = _call(
        tools.create_requisition,
        "REQ-9001",
        "Spare belts",
        10,
        250.00,
        "USD",
        "operator",
        "cid-req",
    )
    assert result["ok"] is True
    assert "REQ-9001" in tools._REQUISITIONS

    events = _events("REQUISITION_CREATED")
    assert len(events) == 1
    assert events[0]["source"] == "tool"


def test_aging_reachable_by_model(clean_state):
    """``get_invoice_aging`` is on the agent allowlist.

    The call itself may fail for any other reason (no sandbox on this host, a
    missing argument, ...); what must not happen is the allowlist rejecting it.
    """
    result = agent.dispatch_tool(
        "get_invoice_aging", {"as_of": "2024-02-01"}, "cid-aging"
    )
    assert result.get("error") != "TOOL_NOT_ALLOWED"


def test_audit_write_failure_leaves_po_state_unchanged(clean_state, monkeypatch):
    """A failed audit write must not leave an unrecorded PO status change."""
    before = {po_id: dict(po) for po_id, po in tools._POS.items()}

    monkeypatch.setattr(tools, "_persist_audit", _fail_audit_write)

    result = tools._apply_transition(
        {"kind": "transition", "po_id": "PO-1001", "status": "CLOSED"},
        "cid-fail",
    )
    assert result["ok"] is False
    assert result["error"] == "AUDIT_WRITE_FAILED"
    assert tools._POS == before
