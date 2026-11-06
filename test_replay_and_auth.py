"""
test_replay_and_auth.py
=======================
Regression tests for five properties of the control plane:

* **Boot replay** - ``_recover_audit_state`` reloads the durable audit chain
  into memory at startup, preserves the hash chain, tolerates a corrupt log,
  and continues the sequence from the recovered head.
* **Separation of duties** - ``_execute_payout`` refuses when the executor is
  the approver, and the model cannot reach ``approve_payout`` or
  ``_execute_payout`` at all.
* **Source labelling** - reserved event types are control-plane only, and
  every persisted entry carries the ``source`` it was written with.
* **Principal threading** - the authenticated principal reaches the sandbox
  envelope, and the model's tool arguments cannot override it.
* **Auth preflight** - ``app.preflight_auth`` fails closed on a missing,
  whitespace-only, or duplicated token, and ``_principals`` assigns one role
  per credential.

The tests are pure-Python: the sandbox boundary is monkeypatched where it
would otherwise need ``bwrap``/``prlimit``, so the file runs on any host.
"""

import pytest

import agent
import app
import config
import sandbox
import tools
import tool_modules._state as state


@pytest.fixture()
def clean_state(tmp_path, monkeypatch):
    """Point the audit log at a temp file and reset in-memory state."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))
    tools._reset_state()
    yield
    tools._reset_state()


# ---------------------------------------------------------------------------
# Boot replay
# ---------------------------------------------------------------------------


def test_boot_replay_restores_audit_chain(clean_state):
    """_recover_audit_state reloads the durable chain into memory."""
    for i in range(3):
        result = tools.log_audit_event("AGENT_NOTE", {"i": i}, "cid", source="agent")
        assert result["ok"] is True

    assert len(tools._load_audit_from_file()) == 3

    # Simulate a process restart: memory is empty, disk is not.
    state._AUDIT = []
    state._AUDIT_SEQ = 0
    assert tools._audit_entries() == []

    state._recover_audit_state()

    recovered = tools._audit_entries()
    assert len(recovered) == 3
    assert [entry["seq"] for entry in recovered] == [1, 2, 3]
    assert state._AUDIT_SEQ == 3


def test_boot_replay_preserves_hash_chain(clean_state):
    """Recovered entries still verify, in memory and on disk."""
    for i in range(3):
        tools.log_audit_event("AGENT_NOTE", {"i": i}, "cid", source="agent")

    state._AUDIT = []
    state._AUDIT_SEQ = 0
    state._recover_audit_state()

    assert tools.verify_audit_chain(from_file=False)["valid"] is True
    assert tools.verify_audit_chain(from_file=True)["valid"] is True


def test_boot_replay_continues_sequence(clean_state):
    """The next append continues from the recovered sequence number."""
    for i in range(3):
        tools.log_audit_event("AGENT_NOTE", {"i": i}, "cid", source="agent")

    state._AUDIT = []
    state._AUDIT_SEQ = 0
    state._recover_audit_state()

    result = tools.log_audit_event("AGENT_NOTE", {"i": 99}, "cid", source="agent")
    assert result["ok"] is True
    assert result["audit"]["seq"] == 4


def test_boot_replay_links_new_entries_to_recovered_head(clean_state):
    """A post-recovery append links to the recovered head, not genesis."""
    for i in range(2):
        tools.log_audit_event("AGENT_NOTE", {"i": i}, "cid", source="agent")
    head_before = tools._load_audit_from_file()[-1]["hash"]

    state._AUDIT = []
    state._AUDIT_SEQ = 0
    state._recover_audit_state()

    tools.log_audit_event("AGENT_NOTE", {"i": 99}, "cid", source="agent")
    new_entry = tools._load_audit_from_file()[-1]
    assert new_entry["prev_hash"] == head_before
    assert tools.verify_audit_chain(from_file=True)["valid"] is True


def test_boot_replay_is_idempotent(clean_state):
    """Recovering twice yields the same chain."""
    for i in range(2):
        tools.log_audit_event("AGENT_NOTE", {"i": i}, "cid", source="agent")

    state._AUDIT = []
    state._AUDIT_SEQ = 0
    state._recover_audit_state()
    first = tools._audit_entries()

    state._recover_audit_state()
    second = tools._audit_entries()

    assert first == second


def test_boot_replay_tolerates_corrupt_log(clean_state):
    """An unreadable log leaves an empty chain rather than raising at boot."""
    with open(config.AUDIT_LOG_PATH, "w", encoding="utf-8") as fh:
        fh.write("{not json}\n")

    state._AUDIT = []
    state._AUDIT_SEQ = 0
    state._recover_audit_state()

    assert tools._audit_entries() == []
    assert state._AUDIT_SEQ == 0


# ---------------------------------------------------------------------------
# Separation of duties
# ---------------------------------------------------------------------------


def _drive_to_approved(approver="alice"):
    """Drive INV-2001 through match -> approval -> approve. Return approval_id."""
    assert tools.run_three_way_match("INV-2001", "PO-1001", "cid")["ok"] is True
    created = tools.request_payout_approval(
        "VEND-001", 12500.00, "PO-1001", "INV-2001", "cid"
    )
    assert created["ok"] is True
    approval_id = created["approval"]["approval_id"]
    approved = tools.approve_payout(approval_id, approver, "cid")
    assert approved["ok"] is True
    return approval_id


def test_execute_refuses_when_executor_is_approver(clean_state):
    approval_id = _drive_to_approved("alice")

    with pytest.raises(tools.ToolError) as excinfo:
        tools._execute_payout(approval_id, "alice", "cid")
    assert excinfo.value.code == "SEPARATION_OF_DUTIES"

    # The refusal must not have moved the approval.
    assert tools.get_approval(approval_id)["approval"]["status"] == "APPROVED"


def test_execute_allows_distinct_executor(clean_state):
    approval_id = _drive_to_approved("alice")

    result = tools._execute_payout(approval_id, "bob", "cid")
    assert result["ok"] is True
    assert result["approval"]["status"] == "EXECUTED"
    assert result["approval"]["executor"] == "bob"


def test_execute_refuses_unapproved(clean_state):
    assert tools.run_three_way_match("INV-2001", "PO-1001", "cid")["ok"] is True
    created = tools.request_payout_approval(
        "VEND-001", 12500.00, "PO-1001", "INV-2001", "cid"
    )
    approval_id = created["approval"]["approval_id"]

    with pytest.raises(tools.ToolError) as excinfo:
        tools._execute_payout(approval_id, "bob", "cid")
    assert excinfo.value.code == "APPROVAL_NOT_APPROVED"


def test_execute_refuses_unknown_approval(clean_state):
    with pytest.raises(tools.ToolError) as excinfo:
        tools._execute_payout("APR-NOPE", "bob", "cid")
    assert excinfo.value.code == "APPROVAL_NOT_FOUND"


def test_sod_violation_writes_no_execution_audit(clean_state):
    """The check runs before the audit link, so a refusal leaves no trace."""
    approval_id = _drive_to_approved("alice")

    with pytest.raises(tools.ToolError):
        tools._execute_payout(approval_id, "alice", "cid")

    types = [entry["event_type"] for entry in tools._load_audit_from_file()]
    assert "PAYOUT_EXECUTED" not in types


def test_agent_cannot_reach_execute_payout(clean_state):
    result = agent.dispatch_tool(
        "_execute_payout", {"approval_id": "APR-X", "executor": "bob"}, "cid"
    )
    assert result["ok"] is False
    assert result["error"] == "TOOL_NOT_ALLOWED"


def test_agent_cannot_reach_approve_payout(clean_state):
    result = agent.dispatch_tool(
        "approve_payout", {"approval_id": "APR-X", "approver": "alice"}, "cid"
    )
    assert result["ok"] is False
    assert result["error"] == "TOOL_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Source labelling
# ---------------------------------------------------------------------------


def test_reserved_event_type_rejected_for_agent_source(clean_state):
    result = tools.log_audit_event(
        "TOOL_CALL_STARTED", {"tool": "x"}, "cid", source="agent"
    )
    assert result["ok"] is False
    assert result["error"] == "RESERVED_EVENT_TYPE"


def test_reserved_event_type_allowed_for_tool_source(clean_state):
    result = tools.log_audit_event(
        "TOOL_CALL_STARTED", {"tool": "x"}, "cid", source="tool"
    )
    assert result["ok"] is True

    entry = tools._load_audit_from_file()[-1]
    assert entry["event_type"] == "TOOL_CALL_STARTED"
    assert entry["source"] == "tool"


def test_agent_note_persists_agent_source(clean_state):
    result = tools.log_audit_event("AGENT_NOTE", {"x": 1}, "cid", source="agent")
    assert result["ok"] is True

    entry = tools._load_audit_from_file()[-1]
    assert entry["source"] == "agent"


def test_control_plane_events_stamped_tool_source(clean_state):
    assert tools.run_three_way_match("INV-2001", "PO-1001", "cid")["ok"] is True

    entry = tools._load_audit_from_file()[-1]
    assert entry["event_type"] == "THREE_WAY_MATCH"
    assert entry["source"] == "tool"


def test_apply_audit_note_cannot_forge_reserved_type(clean_state):
    result = tools._apply_audit_note(
        {"event_type": "PAYOUT_EXECUTED", "payload": {"approval_id": "APR-X"}},
        "cid",
    )
    assert result["ok"] is False
    assert result["error"] == "RESERVED_EVENT_TYPE"


def test_apply_audit_note_stamps_agent_source(clean_state):
    result = tools._apply_audit_note(
        {"event_type": "AGENT_NOTE", "payload": {"x": 1}}, "cid"
    )
    assert result["ok"] is True

    entry = tools._load_audit_from_file()[-1]
    assert entry["source"] == "agent"


# ---------------------------------------------------------------------------
# Principal threading
# ---------------------------------------------------------------------------


def test_build_request_carries_principal(clean_state):
    principal = tools.Principal(sub="alice", roles=frozenset({"operator"}))
    request = sandbox._build_request(
        "get_invoice", {"invoice_id": "INV-2001"}, principal, "cid-42", "run_x"
    )

    assert request["principal"]["sub"] == "alice"
    assert request["principal"]["roles"] == ["operator"]
    assert request["correlation_id"] == "cid-42"


def test_build_request_ignores_principal_in_arguments(clean_state):
    """The envelope's principal comes from the authenticated arg, not the model."""
    principal = tools.Principal(sub="alice", roles=frozenset({"operator"}))
    request = sandbox._build_request(
        "get_invoice",
        {"invoice_id": "INV-2001", "principal": "attacker"},
        principal,
        "cid",
        "run_x",
    )

    assert request["principal"]["sub"] == "alice"


def test_sandbox_dispatch_forwards_principal(clean_state, monkeypatch):
    captured = {}

    def fake_build(tool, arguments, principal, correlation_id, run_id):
        captured["principal"] = principal
        captured["correlation_id"] = correlation_id
        return {
            "contract_version": "x",
            "run_id": run_id,
            "correlation_id": correlation_id,
            "tool": tool,
            "arguments": arguments,
            "principal": {},
            "snapshot": {},
            "limits": {},
        }

    monkeypatch.setattr(sandbox, "_build_request", fake_build)
    monkeypatch.setattr(
        sandbox, "_spawn", lambda request: sandbox._SpawnResult(error="STOP")
    )

    principal = tools.Principal(sub="alice", roles=frozenset({"operator"}))
    result = sandbox.dispatch(
        "get_invoice", {"invoice_id": "INV-2001"}, principal, "cid-42"
    )

    assert result["ok"] is False
    assert result["error"] == "STOP"
    assert captured["principal"] is principal
    assert captured["correlation_id"] == "cid-42"


def test_dispatch_tool_strips_model_supplied_principal(clean_state, monkeypatch):
    captured = {}

    def fake_dispatch(tool, arguments, principal=None, correlation_id=""):
        captured["principal"] = principal
        captured["arguments"] = arguments
        captured["correlation_id"] = correlation_id
        return {"ok": True}

    monkeypatch.setattr(sandbox, "dispatch", fake_dispatch)

    principal = tools.Principal(sub="agent", roles=frozenset({"agent"}))
    agent.dispatch_tool(
        "get_invoice",
        {
            "invoice_id": "INV-2001",
            "principal": "attacker",
            "correlation_id": "attacker-cid",
        },
        "cid-real",
        principal,
    )

    assert captured["principal"] is principal
    assert captured["correlation_id"] == "cid-real"
    assert captured["arguments"] == {"invoice_id": "INV-2001"}


# ---------------------------------------------------------------------------
# Auth preflight
# ---------------------------------------------------------------------------


def _set_tokens(
    monkeypatch,
    agent_t="a-token",
    operator_t="o-token",
    executor_t="e-token",
):
    monkeypatch.setattr(config, "PL_AGENT_TOKEN", agent_t)
    monkeypatch.setattr(config, "PL_OPERATOR_TOKEN", operator_t)
    monkeypatch.setattr(config, "PL_EXECUTOR_TOKEN", executor_t)
    monkeypatch.setattr(config, "PL_OPERATOR_NAME", "operator")
    monkeypatch.setattr(config, "PL_EXECUTOR_NAME", "executor")


def test_preflight_passes_with_distinct_tokens(monkeypatch):
    _set_tokens(monkeypatch)
    app.preflight_auth()  # must not raise


def test_preflight_rejects_missing_token(monkeypatch):
    _set_tokens(monkeypatch, operator_t="")
    with pytest.raises(RuntimeError) as excinfo:
        app.preflight_auth()
    assert "PL_OPERATOR_TOKEN" in str(excinfo.value)


def test_preflight_rejects_whitespace_only_token(monkeypatch):
    _set_tokens(monkeypatch, executor_t="   ")
    with pytest.raises(RuntimeError) as excinfo:
        app.preflight_auth()
    assert "PL_EXECUTOR_TOKEN" in str(excinfo.value)


def test_preflight_rejects_equal_tokens(monkeypatch):
    _set_tokens(monkeypatch, agent_t="same", operator_t="same")
    with pytest.raises(RuntimeError) as excinfo:
        app.preflight_auth()
    assert "must differ" in str(excinfo.value)


def test_principals_assign_distinct_roles(monkeypatch):
    _set_tokens(monkeypatch)
    table = app._principals()

    assert table["a-token"].has_role(app.ROLE_AGENT)
    assert table["o-token"].has_role(app.ROLE_OPERATOR)
    assert table["e-token"].has_role(app.ROLE_EXECUTOR)
    assert not table["a-token"].has_role(app.ROLE_OPERATOR)


def test_authenticate_resolves_operator(monkeypatch):
    _set_tokens(monkeypatch)
    principal = app._authenticate("Bearer o-token")

    assert principal.sub == "operator"
    assert principal.has_role(app.ROLE_OPERATOR)


def test_authenticate_rejects_unknown_token(monkeypatch):
    _set_tokens(monkeypatch)
    with pytest.raises(app.AuthError) as excinfo:
        app._authenticate("Bearer nope")
    assert excinfo.value.code == "AUTH_INVALID"
