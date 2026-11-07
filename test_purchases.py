"""
test_purchases.py
=================
Tests for ``tool_modules._purchases``: the read-only purchase-monitoring view.

Seed facts (from ``tool_modules._state._seed_state``):

* ``PO-1001`` - vendor ``VEND-001``, amount ``12500.00``, status ``OPEN``,
  ``expected_delivery: "2024-01-20"``, one receipt (``RCPT-3001``, 12500.00)
  and one invoice (``INV-2001``, 12500.00).
* ``PO-1002`` - vendor ``VEND-002``, amount ``800.00``, status ``OPEN``,
  ``expected_delivery: "2024-02-10"``, one receipt and one invoice.
* ``_reset_state()`` truncates the audit log, so ``_load_audit_from_file()``
  starts empty.

Every test pins ``as_of`` explicitly where the late check matters; an unpinned
test would age the seed POs against today and drift.
"""

import pytest

import agent
import config
import decisions
import tools
from tool_modules._purchases import list_purchases, purchase_status


@pytest.fixture()
def clean_state(tmp_path, monkeypatch):
    """Point the audit log at a temp file and reset in-memory state."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    tools._reset_state()
    yield
    tools._reset_state()


def test_purchase_status_returns_derived_fields(clean_state):
    """PO-1001 is fully received and fully invoiced, so nothing is outstanding."""
    result = purchase_status("PO-1001", "2024-03-01")

    assert result["ok"] is True
    row = result["purchase"]

    assert row["po_id"] == "PO-1001"
    assert row["vendor_id"] == "VEND-001"
    assert row["vendor_name"] == "Acme Industrial Supply"
    assert row["status"] == "OPEN"
    assert row["currency"] == "USD"
    assert row["amount"] == 12500.00
    assert row["expected_delivery"] == "2024-01-20"
    assert row["requisition_id"] == "REQ-4001"

    assert row["received_amount"] == 12500.00
    assert row["invoiced_amount"] == 12500.00
    assert row["receipt_count"] == 1
    assert row["invoice_count"] == 1
    assert row["last_receipt_at"] != ""

    assert row["is_fully_received"] is True
    assert row["is_fully_invoiced"] is True
    assert row["outstanding_receipt"] == 0.0
    assert row["outstanding_invoice"] == 0.0
    assert row["as_of"] == "2024-03-01"


def test_purchase_status_unknown_po(clean_state):
    """An unknown PO is reported in-band, not raised."""
    result = purchase_status("PO-NOPE")

    assert result["ok"] is False
    assert result["error"] == "PO_NOT_FOUND"
    assert result["detail"] == "PO-NOPE"


def test_purchase_status_rejects_blank_po_id(clean_state):
    """A blank PO ID is a caller error, checked before any store read."""
    result = purchase_status("")

    assert result["ok"] is False
    assert result["error"] == "INVALID_ARGUMENTS"


def test_purchase_status_is_late(clean_state):
    """PO-1001 is OPEN and due 2024-01-20, so it is 41 days late on 2024-03-01."""
    row = purchase_status("PO-1001", "2024-03-01")["purchase"]

    assert row["is_late"] is True
    # 2024 is a leap year: 2024-01-20 -> 2024-03-01 is 41 days.
    assert row["days_late"] == 41


def test_purchase_status_not_late_before_due(clean_state):
    """Before its due date the PO is not late and reports zero days late."""
    row = purchase_status("PO-1001", "2024-01-01")["purchase"]

    assert row["is_late"] is False
    assert row["days_late"] == 0


def test_purchase_status_closed_po_is_not_late(clean_state):
    """A CLOSED PO is never late, even when its delivery date has passed."""
    with tools._LOCK:
        tools._POS["PO-1001"]["status"] = "CLOSED"

    row = purchase_status("PO-1001", "2024-03-01")["purchase"]

    assert row["is_late"] is False
    assert row["days_late"] == 0


def test_purchase_status_outstanding_receipt(clean_state):
    """A fresh PO with no receipts is entirely outstanding on the receipt side."""
    created = tools.create_po("VEND-001", 500.0)
    assert created["ok"] is True
    po_id = created["po"]["po_id"]

    row = purchase_status(po_id)["purchase"]

    assert row["amount"] == 500.0
    assert row["received_amount"] == 0.0
    assert row["receipt_count"] == 0
    assert row["outstanding_receipt"] == 500.0
    assert row["is_fully_received"] is False
    assert row["is_outstanding"] is True


def test_purchase_status_outstanding_invoice(clean_state):
    """A fully received PO with no invoice is outstanding on the invoice side."""
    created = tools.create_po("VEND-001", 500.0)
    assert created["ok"] is True
    po_id = created["po"]["po_id"]

    receipt = tools.record_goods_receipt(po_id, 500.0)
    assert receipt["ok"] is True

    row = purchase_status(po_id)["purchase"]

    assert row["received_amount"] == 500.0
    assert row["invoiced_amount"] == 0.0
    assert row["invoice_count"] == 0
    assert row["outstanding_invoice"] == 500.0
    assert row["is_fully_received"] is True
    assert row["is_fully_invoiced"] is False
    assert row["is_outstanding"] is True


def test_list_purchases_returns_every_po(clean_state):
    """Every PO is listed, sorted by ``po_id``."""
    result = list_purchases("2024-03-01")

    assert result["ok"] is True
    assert result["as_of"] == "2024-03-01"
    assert [row["po_id"] for row in result["purchases"]] == [
        "PO-1001",
        "PO-1002",
    ]


def test_list_purchases_pins_as_of(clean_state):
    """The late check is pinned to ``as_of``, not to the wall clock."""
    early = {row["po_id"]: row for row in list_purchases("2024-01-01")["purchases"]}
    late = {row["po_id"]: row for row in list_purchases("2024-03-01")["purchases"]}

    assert early["PO-1001"]["days_late"] == 0
    assert late["PO-1001"]["days_late"] == 41
    assert early["PO-1002"]["days_late"] == 0
    assert late["PO-1002"]["days_late"] == 20


def test_list_purchases_rejects_bad_as_of(clean_state):
    """A malformed ``as_of`` raises rather than silently meaning "today"."""
    with pytest.raises(tools.ToolError) as excinfo:
        list_purchases("not-a-date")

    assert excinfo.value.code == "INVALID_ARGUMENTS"


def test_purchases_writes_no_audit_events(clean_state):
    """Both reads leave the audit chain untouched."""
    purchase_status("PO-1001", "2024-03-01")
    list_purchases("2024-03-01")

    assert tools._load_audit_from_file() == []


def test_purchases_not_model_reachable():
    """The tools stay outside the model's allowlist and snapshot scopes."""
    for name in ("purchase_status", "list_purchases"):
        assert name not in decisions.PROPOSAL_KINDS
        assert name not in decisions.SNAPSHOT_SCOPES
        assert name not in agent.ALLOWED_TOOLS
