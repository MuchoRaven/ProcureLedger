"""
test_sandbox.py
===============
Tests for the control-plane side of the sandbox boundary.

Scope
-----
These cover the parts of ``sandbox.py`` that can be exercised without a live
``tools`` store: the re-decision comparison (including the ``stale`` flag) and
the cross-module invariants that keep ``decisions`` and ``sandbox`` in step.

The store-level gaps - chain verification, corrupt-log recovery, over-receipt,
currency mismatch, the PO/requisition cancel paths, ``audit_note`` apply, and
true concurrent mutation - need ``tools.py``, ``decisions.py`` and ``audit.py``
and are not here.

Why the drift tests matter
--------------------------
``decisions`` and ``sandbox`` agree by convention, not by construction. Three
separate tables have to line up:

* ``dispatch`` refuses a tool that is not in ``SNAPSHOT_SCOPES``.
* ``_validate`` refuses a proposal kind that is not in
  ``PROPOSAL_KINDS[tool]``.
* ``_apply`` refuses a kind it has no branch for.

A tool added to one table but not the others fails at request time with a 400
that looks like a caller error: a tool in ``SNAPSHOT_SCOPES`` but not
``PROPOSAL_KINDS`` passes dispatch and then has every proposal rejected by
``_validate``; a kind in ``PROPOSAL_KINDS`` with no ``_apply`` branch passes
validation and fails at apply time with ``UNKNOWN_PROPOSAL_KIND``. Neither is
visible until a request hits it. The tests below assert the tables agree, so
the failure is a red test instead.

Why the stale tests matter
--------------------------
``stale`` is only meaningful for reads. A regression that started flagging
transitions as stale would be invisible to a test that only covers the happy
path, so the mismatch cases are covered explicitly.
"""

from __future__ import annotations

import threading
import types
from typing import Any, Dict, List, Optional

import pytest

import config
import decisions
import sandbox


def _principal() -> Any:
    """A minimal principal. ``_build_request`` only uses ``getattr``."""
    return types.SimpleNamespace(
        sub="agent",
        roles=frozenset({"agent"}),
        auth_method="bearer",
    )


def _request(
    tool: str = "get_invoice",
    arguments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A request envelope shaped like ``sandbox._build_request``'s output."""
    return {
        "contract_version": decisions.CONTRACT_VERSION,
        "run_id": "run_test",
        "correlation_id": "cid",
        "tool": tool,
        "arguments": (
            arguments if arguments is not None else {"invoice_id": "INV-1"}
        ),
        "principal": {
            "sub": "agent",
            "roles": ["agent"],
            "auth_method": "bearer",
        },
        "snapshot": {},
        "limits": dict(config.SANDBOX_LIMITS),
    }


def _envelope(
    request: Dict[str, Any],
    proposal: Optional[Dict[str, Any]] = None,
    ok: bool = True,
) -> Dict[str, Any]:
    """A response envelope shaped like ``worker.py``'s output."""
    envelope: Dict[str, Any] = {
        "contract_version": decisions.CONTRACT_VERSION,
        "run_id": request["run_id"],
        "ok": ok,
    }
    if ok:
        envelope["proposal"] = proposal if proposal is not None else {}
    return envelope


# ---------------------------------------------------------------------------
# The stale flag
# ---------------------------------------------------------------------------


def test_read_divergence_sets_stale(monkeypatch):
    """A read whose live data moved since the snapshot is flagged stale.

    The applied proposal must be the *live* one, not the sandbox's: the
    sandbox's copy is only used for the comparison.
    """
    live = {"kind": "read", "data": {"invoice_id": "INV-1", "status": "approved"}}
    sandboxed = {
        "kind": "read",
        "data": {"invoice_id": "INV-1", "status": "matched"},
    }
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(
        sandbox.decisions, "decide", lambda *a, **k: {"ok": True, "proposal": live}
    )

    request = _request()
    error, outcome, stale = sandbox._redeci(request, _envelope(request, sandboxed))

    assert error == ""
    assert stale is True
    assert outcome == {"proposal": live}


def test_read_agreement_is_not_stale(monkeypatch):
    """Identical proposals mean the snapshot was current."""
    proposal = {"kind": "read", "data": {"invoice_id": "INV-1", "status": "matched"}}
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(
        sandbox.decisions,
        "decide",
        lambda *a, **k: {"ok": True, "proposal": proposal},
    )

    request = _request()
    error, outcome, stale = sandbox._redeci(request, _envelope(request, proposal))

    assert error == ""
    assert stale is False
    assert outcome == {"proposal": proposal}


def test_transition_mismatch_is_rejected_not_stale(monkeypatch):
    """A non-read mismatch is a rejection, never a stale read.

    ``stale`` tells a caller "your data was old, here is the current view".
    That is meaningless for a mutation: a transition that disagrees with live
    state must be refused outright.
    """
    live = {"kind": "transition", "invoice_id": "INV-1", "to": "approved"}
    sandboxed = {"kind": "transition", "invoice_id": "INV-1", "to": "paid"}
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(
        sandbox.decisions, "decide", lambda *a, **k: {"ok": True, "proposal": live}
    )

    request = _request()
    error, outcome, stale = sandbox._redeci(request, _envelope(request, sandboxed))

    assert error == "PROPOSAL_REJECTED"
    assert outcome is None
    assert stale is False


def test_transition_match_is_not_stale(monkeypatch):
    """An agreeing transition is applied and is never marked stale."""
    proposal = {"kind": "transition", "invoice_id": "INV-1", "to": "approved"}
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(
        sandbox.decisions,
        "decide",
        lambda *a, **k: {"ok": True, "proposal": proposal},
    )

    request = _request()
    error, outcome, stale = sandbox._redeci(request, _envelope(request, proposal))

    assert error == ""
    assert stale is False
    assert outcome == {"proposal": proposal}


def test_sandbox_ok_but_live_denies_is_rejected(monkeypatch):
    """A sandbox that says yes where live state says no is rejected.

    This is the containment property: the sandbox's proposal is never trusted
    on its own, so a compromised worker cannot talk the control plane into a
    mutation that live state forbids.
    """
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(
        sandbox.decisions,
        "decide",
        lambda *a, **k: {"ok": False, "error": "INVOICE_NOT_FOUND"},
    )

    request = _request()
    proposal = {"kind": "transition", "invoice_id": "INV-1", "to": "approved"}
    error, outcome, stale = sandbox._redeci(request, _envelope(request, proposal))

    assert error == "PROPOSAL_REJECTED"
    assert outcome is None
    assert stale is False


def test_both_sides_deny_returns_the_live_error(monkeypatch):
    """When both sides refuse, the caller gets the live error, not a rejection.

    The distinction matters: ``PROPOSAL_REJECTED`` means "the sandbox lied",
    which is an integrity event. A shared refusal is an ordinary business
    error and must reach the caller as one.
    """
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(
        sandbox.decisions,
        "decide",
        lambda *a, **k: {
            "ok": False,
            "error": "INVOICE_NOT_FOUND",
            "detail": "no such invoice",
        },
    )

    request = _request()
    error, outcome, stale = sandbox._redeci(request, _envelope(request, ok=False))

    assert error == ""
    assert stale is False
    assert outcome == {"error": "INVOICE_NOT_FOUND", "detail": "no such invoice"}


def test_decision_raise_is_decision_failed(monkeypatch):
    """A raise in the control-plane re-decision must not escape as a 500.

    ``worker.py`` guards its own ``decide`` call the same way; the two sides
    have to fail closed identically or a crash on one side looks like a
    disagreement on the other.
    """
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})

    def boom(*a, **k):
        raise RuntimeError("decide exploded")

    monkeypatch.setattr(sandbox.decisions, "decide", boom)

    request = _request()
    error, outcome, stale = sandbox._redeci(request, _envelope(request))

    assert error == "DECISION_FAILED"
    assert outcome is None
    assert stale is False


# ---------------------------------------------------------------------------
# Table drift: SNAPSHOT_SCOPES / PROPOSAL_KINDS / _apply
# ---------------------------------------------------------------------------


def test_every_snapshot_scope_declares_proposal_kinds():
    """A dispatchable tool must declare at least one proposal kind.

    ``_validate`` looks up ``PROPOSAL_KINDS.get(tool, ())``. A tool in
    ``SNAPSHOT_SCOPES`` with no entry therefore passes ``dispatch``'s guard and
    then has every proposal rejected with ``PROPOSAL_KIND_NOT_ALLOWED``.
    """
    for tool in decisions.SNAPSHOT_SCOPES:
        assert decisions.PROPOSAL_KINDS.get(tool), (
            f"{tool} is dispatchable (in SNAPSHOT_SCOPES) but declares no "
            f"PROPOSAL_KINDS; _validate would reject every proposal it makes"
        )


def test_every_allowed_tool_is_dispatchable():
    """A model-callable tool must be dispatchable.

    ``ALLOWED_TOOLS`` is what the model is told it may call. A tool there but
    not in ``SNAPSHOT_SCOPES`` is advertised and then refused with
    ``TOOL_NOT_ALLOWED``, which the model sees as a tool failure and retries.
    """
    for tool in decisions.ALLOWED_TOOLS:
        assert tool in decisions.SNAPSHOT_SCOPES, (
            f"{tool} is model-callable (in ALLOWED_TOOLS) but dispatch refuses "
            f"it with TOOL_NOT_ALLOWED"
        )


def test_every_proposal_kind_is_applied(monkeypatch):
    """Every declared kind must have a branch in ``sandbox._apply``.

    A kind added to ``PROPOSAL_KINDS`` without a matching branch passes
    ``_validate`` and then fails at apply time with ``UNKNOWN_PROPOSAL_KIND`` -
    a 400 for what is a server-side gap.
    """

    def _stub(name: str):
        def _inner(proposal: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
            return {"ok": True, "applied": name}

        return _inner

    monkeypatch.setattr(sandbox.tools, "_apply_transition", _stub("transition"))
    monkeypatch.setattr(
        sandbox.tools, "_apply_create_approval", _stub("create_approval")
    )
    monkeypatch.setattr(sandbox.tools, "_apply_audit_note", _stub("audit_note"))
    # ``create_requisition`` is applied by ``tools._apply_create_requisition``.
    # Stub it too: the loop below calls ``_apply`` with an empty ``data``
    # payload, and the real function would touch the store lock and audit log.
    monkeypatch.setattr(
        sandbox.tools, "_apply_create_requisition", _stub("create_requisition")
    )

    kinds = set()
    for tool_kinds in decisions.PROPOSAL_KINDS.values():
        kinds.update(tool_kinds)

    assert kinds, "PROPOSAL_KINDS is empty; the drift check would be vacuous"

    for kind in sorted(kinds):
        result = sandbox._apply("any_tool", {"kind": kind, "data": {}}, "cid")
        assert result.get("error") != "UNKNOWN_PROPOSAL_KIND", (
            f"proposal kind {kind!r} is declared in PROPOSAL_KINDS but "
            f"sandbox._apply has no branch for it"
        )


def test_validate_accepts_every_declared_kind():
    """``_validate`` must accept each kind its own table declares."""
    for tool, kinds in decisions.PROPOSAL_KINDS.items():
        request = _request(tool=tool)
        for kind in kinds:
            envelope = _envelope(request, {"kind": kind})
            assert sandbox._validate(request, envelope) == "", (
                f"_validate rejects {kind!r}, which PROPOSAL_KINDS declares "
                f"for {tool}"
            )


def test_validate_rejects_undeclared_kind():
    """A kind outside the tool's table is refused before it reaches ``_apply``."""
    tool = next(iter(decisions.SNAPSHOT_SCOPES))
    request = _request(tool=tool)
    envelope = _envelope(request, {"kind": "__not_a_kind__"})
    assert sandbox._validate(request, envelope) == "PROPOSAL_KIND_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Dispatch plumbing
# ---------------------------------------------------------------------------


def test_dispatch_rejects_unknown_tool_without_spawning(monkeypatch):
    """A refused tool must not pay for a sandbox spawn.

    The guard is before ``_build_request`` and ``_spawn``; if it ever moved
    after them, every refused call would fork a ``bwrap`` process first.
    """

    def boom(request: Dict[str, Any]) -> Any:
        raise AssertionError("dispatch spawned a sandbox for a refused tool")

    monkeypatch.setattr(sandbox, "_spawn", boom)

    result = sandbox.dispatch("__not_a_tool__", {}, _principal())

    assert result == {
        "ok": False,
        "error": "TOOL_NOT_ALLOWED",
        "tool": "__not_a_tool__",
    }


def test_concurrent_dispatch_uses_distinct_run_ids(monkeypatch):
    """Each ``dispatch`` call must build its own request envelope.

    ``dispatch`` runs on FastAPI's threadpool. A shared or module-level
    envelope would let one call's ``run_id`` leak into another's, and
    ``_validate`` would then reject a perfectly good response with
    ``RUN_ID_MISMATCH`` - an intermittent failure that only appears under
    load.
    """
    tool = next(iter(decisions.SNAPSHOT_SCOPES))
    monkeypatch.setattr(sandbox, "_snapshot", lambda *a, **k: {})

    seen: List[str] = []
    seen_lock = threading.Lock()

    def fake_spawn(request: Dict[str, Any]) -> Any:
        with seen_lock:
            seen.append(request["run_id"])
        return sandbox._SpawnResult(error="SANDBOX_TIMEOUT", detail="test")

    monkeypatch.setattr(sandbox, "_spawn", fake_spawn)

    results: List[Dict[str, Any]] = []
    results_lock = threading.Lock()

    def call() -> None:
        result = sandbox.dispatch(tool, {}, _principal(), correlation_id="cid")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 8
    assert len(set(seen)) == 8, "two dispatches shared a run_id"
    assert all(r.get("error") == "SANDBOX_TIMEOUT" for r in results)
