"""
test_vendor_compare.py
======================
Tests for the SKU-scoped vendor-quote tools and their deciders.

Two things are covered:

* **Decider/tool agreement.** ``sandbox._redeci`` compares a read's proposal
  against the live re-decision and flags any difference as ``stale``. The
  decider in ``decisions`` and the tool in ``tool_modules._tools_vendor_compare``
  are separate implementations of the same scoring, so a drift between them is
  a silent correctness bug: every call still succeeds, it just comes back
  ``stale``. The agreement tests assert the two produce identical output for
  the same state, including the weights, the rounding and the tie-break.

* **Error mapping.** ``NO_QUOTES_FOR_SKU`` must be a 404 and
  ``INVALID_WEIGHTS`` a 400. Both were unmapped before step 9, so a missing
  SKU surfaced as a 400 - a caller error for a missing record.

Seed facts (from ``tool_modules._state._seed_state``):

* ``SKU-HYD-001`` has three quotes: ``VQ-7001`` (VEND-001, 120.00, 7 days,
  LOW), ``VQ-7002`` (VEND-002, 115.00, 14 days, MEDIUM) and ``VQ-7003``
  (VEND-004, 110.00, 5 days, HIGH).
* ``SKU-GSK-002`` has two: ``VQ-7004`` (VEND-001, 20.00) and ``VQ-7005``
  (VEND-002, 18.00).
* All five are ``ACTIVE`` with ``valid_until: "2025-12-31"``.
* ``VEND-003`` is ``INACTIVE`` but has no vendor quote, so it never appears.

The store dicts are read through ``tool_modules._state`` rather than
``tools``: ``tools`` re-exports only the AP/procurement stores, not
``_VENDORS`` or ``_VENDOR_QUOTES``.
"""

import pytest

import agent
import app
import config
import decisions
import tools
import tool_modules._state as state
from tool_modules._codes import (
    NO_QUOTES_FOR_SKU,
    INVALID_WEIGHTS,
)
from tool_modules._tools_vendor_compare import (
    get_vendor_quotes,
    compare_vendors,
)


@pytest.fixture()
def clean_state(tmp_path, monkeypatch):
    """Point the audit log at a temp file and reset in-memory state."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    tools._reset_state()
    yield
    tools._reset_state()


def _snapshot(tool, **kwargs):
    """Build the snapshot the sandbox would build for this call."""
    return tools._snapshot_for(tool, **kwargs)


def _decided_data(tool, arguments, snapshot):
    """Run the decider and return its proposal's ``data``, or ``None``."""
    result = decisions.decide(tool, arguments, snapshot)
    if not result.get("ok"):
        return None
    return result["proposal"]["data"]


def _tool_data(result):
    """Strip ``ok`` from a tool result, leaving the decider's ``data`` shape."""
    return {key: value for key, value in result.items() if key != "ok"}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_no_quotes_for_sku_is_mapped_to_404():
    assert app._TOOL_ERROR_STATUS[NO_QUOTES_FOR_SKU] == 404


def test_invalid_weights_is_mapped_to_400():
    assert app._TOOL_ERROR_STATUS[INVALID_WEIGHTS] == 400


def test_vendor_compare_codes_are_mapped():
    """Both codes must be in the table, not left to the 400 default."""
    for code in (NO_QUOTES_FOR_SKU, INVALID_WEIGHTS):
        assert code in app._TOOL_ERROR_STATUS, (
            f"{code} is not mapped in app._TOOL_ERROR_STATUS; a failure "
            f"would surface as a 400"
        )


# ---------------------------------------------------------------------------
# Decider / tool agreement
# ---------------------------------------------------------------------------


def test_get_vendor_quotes_decider_matches_tool(clean_state):
    """The decider's proposal must equal the tool's result for the same state."""
    live = get_vendor_quotes("SKU-HYD-001")
    assert live["ok"] is True

    snapshot = _snapshot("get_vendor_quotes", sku="SKU-HYD-001")
    decided = _decided_data(
        "get_vendor_quotes", {"sku": "SKU-HYD-001"}, snapshot
    )

    assert decided == _tool_data(live)


def test_compare_vendors_decider_matches_tool(clean_state):
    """The default-weights ranking must match exactly, including rounding."""
    live = compare_vendors("SKU-HYD-001", as_of="2024-03-01")
    assert live["ok"] is True

    snapshot = _snapshot("compare_vendors", sku="SKU-HYD-001", as_of="2024-03-01")
    decided = _decided_data(
        "compare_vendors",
        {"sku": "SKU-HYD-001", "as_of": "2024-03-01"},
        snapshot,
    )

    assert decided == _tool_data(live)


def test_compare_vendors_decider_matches_tool_with_weights(clean_state):
    """A custom weights dict must normalise identically on both sides."""
    weights = {"price": 1.0, "lead_time": 0.0, "risk": 3.0}
    live = compare_vendors("SKU-HYD-001", weights=weights, as_of="2024-03-01")
    assert live["ok"] is True

    snapshot = _snapshot("compare_vendors", sku="SKU-HYD-001", as_of="2024-03-01")
    decided = _decided_data(
        "compare_vendors",
        {"sku": "SKU-HYD-001", "weights": weights, "as_of": "2024-03-01"},
        snapshot,
    )

    assert decided == _tool_data(live)


def test_compare_vendors_decider_matches_tool_when_all_excluded(clean_state):
    """An all-ineligible SKU is ``ok: True`` with an empty ranking on both sides."""
    with state._LOCK:
        for quote in state._VENDOR_QUOTES.values():
            if quote.get("sku") == "SKU-HYD-001":
                quote["status"] = "INACTIVE"

    live = compare_vendors("SKU-HYD-001", as_of="2024-03-01")
    assert live["ok"] is True
    assert live["ranking"] == []

    snapshot = _snapshot("compare_vendors", sku="SKU-HYD-001", as_of="2024-03-01")
    decided = _decided_data(
        "compare_vendors",
        {"sku": "SKU-HYD-001", "as_of": "2024-03-01"},
        snapshot,
    )

    assert decided == _tool_data(live)


def test_compare_vendors_decider_matches_tool_on_missing_sku(clean_state):
    """Both sides report ``NO_QUOTES_FOR_SKU`` for a SKU nobody quoted."""
    live = compare_vendors("SKU-NOPE", as_of="2024-03-01")
    assert live["ok"] is False
    assert live["error"] == NO_QUOTES_FOR_SKU

    snapshot = _snapshot("compare_vendors", sku="SKU-NOPE", as_of="2024-03-01")
    decided = decisions.decide(
        "compare_vendors", {"sku": "SKU-NOPE", "as_of": "2024-03-01"}, snapshot
    )

    assert decided["ok"] is False
    assert decided["error"] == NO_QUOTES_FOR_SKU


def test_compare_vendors_decider_matches_tool_on_bad_weights(clean_state):
    """Both sides reject a negative weight with ``INVALID_WEIGHTS``."""
    live = compare_vendors("SKU-HYD-001", weights={"price": -1.0})
    assert live["ok"] is False
    assert live["error"] == INVALID_WEIGHTS

    snapshot = _snapshot("compare_vendors", sku="SKU-HYD-001")
    decided = decisions.decide(
        "compare_vendors",
        {"sku": "SKU-HYD-001", "weights": {"price": -1.0}},
        snapshot,
    )

    assert decided["ok"] is False
    assert decided["error"] == INVALID_WEIGHTS


# ---------------------------------------------------------------------------
# Scoring behaviour
# ---------------------------------------------------------------------------


def test_compare_vendors_ranks_cheapest_fastest_first(clean_state):
    """VQ-7003 wins on price and lead time despite being HIGH risk.

    With the default weights the HIGH-risk penalty (0.2) is smaller than the
    price and lead-time advantage, so the ranking is VQ-7003, VQ-7001,
    VQ-7002. HIGH-risk vendors are ranked, not excluded - the block belongs at
    the payout gate, not here.
    """
    result = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    assert result["ok"] is True
    assert [row["quote_id"] for row in result["ranking"]] == [
        "VQ-7003",
        "VQ-7001",
        "VQ-7002",
    ]
    assert [row["rank"] for row in result["ranking"]] == [1, 2, 3]


def test_compare_vendors_reports_normalised_weights(clean_state):
    """The reported weights sum to 1 and are rounded to six places."""
    result = compare_vendors(
        "SKU-HYD-001", weights={"price": 1, "lead_time": 1, "risk": 2}
    )

    weights = result["weights"]
    assert set(weights) == {"price", "lead_time", "risk"}
    assert weights == {"price": 0.25, "lead_time": 0.25, "risk": 0.5}
    assert round(sum(weights.values()), 6) == 1.0


def test_compare_vendors_constant_criterion_normalises_to_zero(clean_state):
    """A criterion identical across every candidate contributes nothing.

    All three SKU-HYD-001 quotes are USD, so a currency criterion would be
    constant. The same rule applies to any criterion: it must not contribute a
    spurious difference. Here every lead time is forced equal.
    """
    with state._LOCK:
        for quote in state._VENDOR_QUOTES.values():
            if quote.get("sku") == "SKU-HYD-001":
                quote["lead_time_days"] = 7

    result = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    for row in result["ranking"]:
        assert row["components"]["lead_time"] == 0.0


def test_compare_vendors_excludes_inactive_vendor(clean_state):
    """A quote from an INACTIVE vendor is excluded with a reason, not dropped."""
    with state._LOCK:
        state._VENDORS["VEND-002"]["status"] = "INACTIVE"

    result = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    excluded = {row["quote_id"]: row["reason"] for row in result["excluded"]}
    assert excluded == {"VQ-7002": "VENDOR_INACTIVE"}
    assert "VQ-7002" not in [row["quote_id"] for row in result["ranking"]]


def test_compare_vendors_excludes_expired_quote(clean_state):
    """A quote past ``valid_until`` is excluded with ``QUOTE_EXPIRED``."""
    with state._LOCK:
        state._VENDOR_QUOTES["VQ-7001"]["valid_until"] = "2024-01-01"

    result = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    excluded = {row["quote_id"]: row["reason"] for row in result["excluded"]}
    assert excluded == {"VQ-7001": "QUOTE_EXPIRED"}


def test_compare_vendors_excludes_inactive_quote(clean_state):
    """A quote whose own status is not ACTIVE is excluded."""
    with state._LOCK:
        state._VENDOR_QUOTES["VQ-7001"]["status"] = "WITHDRAWN"

    result = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    excluded = {row["quote_id"]: row["reason"] for row in result["excluded"]}
    assert excluded == {"VQ-7001": "QUOTE_INACTIVE"}


def test_compare_vendors_is_deterministic_across_insertion_order(clean_state):
    """Reordering the store must not change the ranking.

    The candidates are sorted by ``quote_id`` before any arithmetic, so the
    float sums are computed in the same order regardless of how the dict was
    built. Without that, two runs over the same state could disagree on the
    last decimal place and every call would come back ``stale``.
    """
    first = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    with state._LOCK:
        reordered = dict(reversed(list(state._VENDOR_QUOTES.items())))
        state._VENDOR_QUOTES.clear()
        state._VENDOR_QUOTES.update(reordered)

    second = compare_vendors("SKU-HYD-001", as_of="2024-03-01")

    assert first == second


# ---------------------------------------------------------------------------
# get_vendor_quotes behaviour
# ---------------------------------------------------------------------------


def test_get_vendor_quotes_includes_inactive(clean_state):
    """The listing does not filter; ``compare_vendors`` is the tool that does."""
    with state._LOCK:
        state._VENDORS["VEND-002"]["status"] = "INACTIVE"

    result = get_vendor_quotes("SKU-HYD-001")

    assert result["ok"] is True
    by_id = {row["quote_id"]: row for row in result["quotes"]}
    assert by_id["VQ-7002"]["vendor_status"] == "INACTIVE"


def test_get_vendor_quotes_is_ordered_by_quote_id(clean_state):
    result = get_vendor_quotes("SKU-HYD-001")

    ids = [row["quote_id"] for row in result["quotes"]]
    assert ids == sorted(ids)


def test_get_vendor_quotes_rejects_blank_sku(clean_state):
    result = get_vendor_quotes("   ")

    assert result["ok"] is False
    assert result["error"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_tools_are_model_callable():
    """Both are in the agent allowlist, so the model may attempt them."""
    assert "get_vendor_quotes" in agent.ALLOWED_TOOLS
    assert "compare_vendors" in agent.ALLOWED_TOOLS


def test_both_tools_have_schemas():
    """Every allowlisted tool must have a schema, or the model cannot call it."""
    names = {schema["function"]["name"] for schema in agent._TOOL_SCHEMAS}
    assert "get_vendor_quotes" in names
    assert "compare_vendors" in names


def test_every_allowed_tool_has_a_schema():
    """The allowlist must be a subset of the schema list.

    A tool in ``ALLOWED_TOOLS`` with no schema is advertised to the model as
    callable but never described, so the model cannot emit a well-formed call
    for it. The reverse is allowed and expected: ``_TOOL_SCHEMAS`` also
    describes app-layer tools (``get_po``, ``close_po``, ...) that the model
    may not call, so the two sets are not equal.
    """
    names = {schema["function"]["name"] for schema in agent._TOOL_SCHEMAS}
    assert agent.ALLOWED_TOOLS <= names
