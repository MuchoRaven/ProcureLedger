"""
sandbox.py
==========
Control-plane side of the sandbox boundary.

``dispatch`` is the only entry point. It:

1. Builds a request envelope with a tool-scoped, read-only snapshot.
2. Spawns ``worker.py`` under ``bwrap`` with no network, a read-only root,
   a fresh tmpfs workspace, and ``prlimit`` caps.
3. Validates the response envelope.
4. Re-runs ``decisions.decide`` on live state and rejects any proposal that
   does not match.
5. Applies the *live* proposal via ``tools`` and returns the result.

The sandbox's proposal is only used for the comparison. What gets applied is
always the control plane's own re-decision, so a compromised sandbox can only
propose what an honest sandbox would have proposed.

Containment properties
----------------------
* No network: ``bwrap --unshare-all`` includes ``--unshare-net``.
* No secrets: ``--clearenv`` then a minimal ``--setenv`` allowlist. The
  request envelope carries business data only.
* Read-only root: ``--ro-bind`` for ``/usr``, ``/lib``, ``/lib64``, and the
  app directory. The only writable paths are ``/tmp`` and ``/workspace``,
  both tmpfs.
* Resource caps: ``prlimit --as`` for address space, ``--nofile`` and
  ``--nproc`` for file and process counts, ``--core=0`` to suppress dumps.
  Wall clock is enforced by ``subprocess.run(timeout=...)``.
* Lifecycle: bwrap creates a fresh tmpfs ``/workspace`` per run and
  ``--die-with-parent`` tears it down with the process. No reuse, no warm
  pool, and nothing to clean up on the host.

Availability
------------
``preflight`` is called from the app's startup hook and raises if ``bwrap`` or
``prlimit`` is missing, so a host that cannot sandbox fails at boot rather than
on the first tool call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import config
import decisions
import tools

# Binaries the sandbox cannot run without. Checked by ``preflight``.
_REQUIRED_BINARIES = ("bwrap", "prlimit")


def preflight() -> None:
    """Fail fast if this host cannot run the sandbox.

    Without this, a host missing ``bwrap`` or ``prlimit`` produces
    ``SANDBOX_SPAWN_FAILED`` on every tool call, the model sees errors in its
    transcript, and ``agent.run`` still returns ``ok: True`` with a useless
    reply - a broken deployment reported as a successful run.
    """
    missing = [name for name in _REQUIRED_BINARIES if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "sandbox unavailable: missing "
            + ", ".join(missing)
            + " (install bubblewrap and util-linux)"
        )


@dataclass
class _SpawnResult:
    error: Optional[str] = None
    detail: str = ""
    envelope: Optional[Dict[str, Any]] = None


def _build_request(
    tool: str,
    arguments: Dict[str, Any],
    principal: Any,
    correlation_id: str,
    run_id: str,
) -> Dict[str, Any]:
    return {
        "contract_version": decisions.CONTRACT_VERSION,
        "run_id": run_id,
        "correlation_id": correlation_id or "",
        "tool": tool,
        "arguments": arguments,
        "principal": {
            "sub": getattr(principal, "sub", "agent"),
            "roles": sorted(getattr(principal, "roles", [])),
            "auth_method": getattr(principal, "auth_method", "service"),
        },
        "snapshot": tools._snapshot_for(tool),
        "limits": dict(config.SANDBOX_LIMITS),
    }


def _bwrap_command() -> list:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    limits = config.SANDBOX_LIMITS
    cmd = [
        "prlimit",
        f"--as={limits['memory_mb'] * 1024 * 1024}",
        "--nofile=256",
        "--nproc=64",
        "--core=0",
        "--",
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONPATH", app_dir,
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PL_WORKSPACE", "/workspace",
        "--setenv", "PL_SANDBOX_PROFILE", "default",
    ]
    for path in ("/usr", "/lib", "/lib64"):
        if os.path.exists(path):
            cmd.extend(["--ro-bind", path, path])
    cmd.extend([
        "--ro-bind", app_dir, app_dir,
        "--tmpfs", "/tmp",
        "--tmpfs", "/workspace",
        "--proc", "/proc",
        "--dev", "/dev",
        "--chdir", "/workspace",
        "python3", os.path.join(app_dir, "worker.py"),
    ])
    return cmd


def _spawn(request: Dict[str, Any]) -> _SpawnResult:
    cmd = _bwrap_command()
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
    try:
        proc = subprocess.run(
            cmd,
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=request["limits"]["wall_clock_s"],
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _SpawnResult(error="SANDBOX_TIMEOUT", detail="wall clock exceeded")
    except OSError as exc:
        return _SpawnResult(error="SANDBOX_SPAWN_FAILED", detail=str(exc))

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")[:512]
        return _SpawnResult(
            error="SANDBOX_VIOLATION",
            detail=f"exit {proc.returncode}: {stderr}",
        )

    try:
        envelope = json.loads(proc.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _SpawnResult(error="MALFORMED_RESPONSE", detail=str(exc))

    if not isinstance(envelope, dict):
        return _SpawnResult(error="MALFORMED_RESPONSE", detail="not an object")

    sandbox_meta = envelope.get("sandbox")
    if not isinstance(sandbox_meta, dict):
        sandbox_meta = {}
    sandbox_meta["exit_code"] = proc.returncode
    envelope["sandbox"] = sandbox_meta
    return _SpawnResult(envelope=envelope)


def _validate(request: Dict[str, Any], envelope: Dict[str, Any]) -> str:
    if envelope.get("contract_version") != decisions.CONTRACT_VERSION:
        return "CONTRACT_VERSION_MISMATCH"
    if envelope.get("run_id") != request.get("run_id"):
        return "RUN_ID_MISMATCH"
    if envelope.get("ok") is not True:
        return ""
    proposal = envelope.get("proposal")
    if not isinstance(proposal, dict):
        return "MALFORMED_PROPOSAL"
    kind = proposal.get("kind")
    allowed = decisions.PROPOSAL_KINDS.get(request.get("tool"), ())
    if kind not in allowed:
        return "PROPOSAL_KIND_NOT_ALLOWED"
    return ""


def _redeci(
    request: Dict[str, Any], envelope: Dict[str, Any]
) -> Tuple[str, Optional[Dict[str, Any]], bool]:
    """Re-run the decision on live state.

    Returns ``(error, outcome, stale)``. ``outcome`` is either
    ``{"proposal": ...}`` (apply this) or ``{"error": ..., "detail": ...}``
    (return this to the caller). ``stale`` is true only for reads whose live
    data diverged from the snapshot.
    """
    tool = request["tool"]
    arguments = request["arguments"]
    try:
        live = tools._snapshot_for(tool)
        expected = decisions.decide(tool, arguments, live)
    except Exception:
        # Mirror the worker's guard: a raise here must not escape as a 500.
        return "DECISION_FAILED", None, False

    received_ok = envelope.get("ok") is True

    if not expected.get("ok"):
        if received_ok:
            return "PROPOSAL_REJECTED", None, False
        return "", {
            "error": expected.get("error"),
            "detail": expected.get("detail", ""),
        }, False

    if not received_ok:
        return "PROPOSAL_REJECTED", None, False

    expected_proposal = expected["proposal"]
    received_proposal = envelope.get("proposal")

    if expected_proposal.get("kind") == "read":
        stale = expected_proposal != received_proposal
        return "", {"proposal": expected_proposal}, stale

    if expected_proposal != received_proposal:
        return "PROPOSAL_REJECTED", None, False

    return "", {"proposal": expected_proposal}, False


def _apply(
    tool: str, proposal: Dict[str, Any], correlation_id: str
) -> Dict[str, Any]:
    kind = proposal.get("kind")
    if kind == "read":
        return {"ok": True, **proposal.get("data", {})}
    if kind == "transition":
        return tools._apply_transition(proposal, correlation_id)
    if kind == "create_approval":
        return tools._apply_create_approval(proposal, correlation_id)
    if kind == "audit_note":
        return tools._apply_audit_note(proposal, correlation_id)
    return {"ok": False, "error": "UNKNOWN_PROPOSAL_KIND", "kind": kind}


def dispatch(
    tool: str,
    arguments: Dict[str, Any],
    principal: Any,
    correlation_id: str = "",
) -> Dict[str, Any]:
    """Run one tool through the sandbox and apply the verified proposal."""
    if tool not in decisions.SNAPSHOT_SCOPES:
        return {"ok": False, "error": "TOOL_NOT_ALLOWED", "tool": tool}

    run_id = "run_" + uuid.uuid4().hex[:12]
    request = _build_request(tool, arguments, principal, correlation_id, run_id)

    spawn = _spawn(request)
    if spawn.error:
        return {
            "ok": False,
            "error": spawn.error,
            "detail": spawn.detail,
            "tool": tool,
        }

    envelope = spawn.envelope
    error = _validate(request, envelope)
    if error:
        return {"ok": False, "error": error, "tool": tool}

    error, outcome, stale = _redeci(request, envelope)
    if error:
        return {"ok": False, "error": error, "tool": tool}

    if not isinstance(outcome, dict):
        return {"ok": False, "error": "PROPOSAL_REJECTED", "tool": tool}

    if "error" in outcome:
        return {
            "ok": False,
            "error": outcome.get("error") or "DECISION_FAILED",
            "detail": outcome.get("detail", ""),
            "tool": tool,
        }

    proposal = outcome.get("proposal")
    if not isinstance(proposal, dict):
        return {"ok": False, "error": "MALFORMED_PROPOSAL", "tool": tool}

    result = _apply(tool, proposal, correlation_id)

    if stale:
        result = dict(result)
        result["stale"] = True

    return result
