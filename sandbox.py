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
* Read-only root: ``--ro-bind`` for the resolved system paths (see
   ``_bind_paths``) and the app directory. The only writable paths are ``/tmp``
   and ``/workspace``, both tmpfs.
* Resource caps: ``prlimit --as`` for address space, ``--nofile`` and
   ``--nproc`` for file and process counts, ``--core=0`` to suppress dumps.
   Wall clock is enforced by ``subprocess.run(timeout=...)``.
* Lifecycle: bwrap creates a fresh tmpfs ``/workspace`` per run and
   ``--die-with-parent`` tears it down with the process. No reuse, no warm
   pool, and nothing to clean up on the host.

Error detail
------------
A non-zero exit from the sandbox is classified from its stderr into a specific
code - ``SANDBOX_NO_USERNS``, ``SANDBOX_NO_PYTHON``, ``SANDBOX_OOM`` - falling
back to ``SANDBOX_VIOLATION`` when nothing matches. The stderr text itself is
logged server-side at ``WARNING`` and never crosses the boundary: it can
contain host paths, and ``detail`` flows back through ``dispatch`` to the agent
and the model. What the caller gets instead is the code plus a static,
path-free hint naming the fix.

Availability
------------
``preflight`` is called from the app's startup hook. It checks that ``bwrap``
and ``prlimit`` exist *and* runs one real invocation, so a host that has the
binaries but cannot actually sandbox fails at boot rather than on the first
tool call. That distinction matters: ``--unshare-all`` is
``--unshare-user-try``, so a container without ``CAP_SYS_ADMIN`` does not fail
at the unshare - it falls back and dies at the first ``mount()``, which is
indistinguishable from a working host until a tool is actually called.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import config
import decisions
import tools

_LOG = logging.getLogger(__name__)

# Binaries the sandbox cannot run without. Checked by ``preflight``.
_REQUIRED_BINARIES = ("bwrap", "prlimit")

# Argument keys that select records for the sandbox snapshot. Only these are
# forwarded to ``tools._snapshot_for``; everything else in ``arguments``
# (amounts, notes, ...) is business data, not a selector. ``as_of`` is not an
# ID but is forwarded so the snapshot is a pure function of the arguments.
# ``sku`` is a selector too, and the one that widens rather than narrows: a
# vendor comparison needs every quote for the SKU, not one. It is still
# bounded by the SKU, so the snapshot never becomes the whole store.
_SNAPSHOT_ARG_KEYS = (
    "invoice_id",
    "vendor_id",
    "po_id",
    "requisition_id",
    "receipt_id",
    "approval_id",
    "rfq_id",
    "quote_id",
    "item_id",
    "sku",
    "as_of",
)

# Paths bound read-only into the sandbox, before resolution.
#
# ``/usr/local`` is listed explicitly because the runtime image installs the
# interpreter there and symlinks ``/usr/bin/python3`` to it; on bookworm it is
# a plain subdirectory of ``/usr``, so ``_bind_paths`` normally folds it into
# the ``/usr`` bind. It stays in the list so the intent is visible and so a
# host where it *is* a separate mount still gets it.
_RAW_BIND_PATHS = ("/usr", "/usr/local", "/lib", "/lib64")

# stderr fragments -> specific sandbox error code. Matched in order, first hit
# wins, so the most specific signature must come first: a missing interpreter
# also prints "No such file or directory", and that must not be read as a
# namespace failure.
#
# The fragments are deliberately short and lower-cased. They are matched
# against bwrap's and glibc's own messages, which are stable across the
# versions this image ships.
_STDERR_SIGNATURES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("execvp python3", "execvp: no such file"), "SANDBOX_NO_PYTHON"),
    (
        ("memoryerror", "cannot allocate memory", "out of memory"),
        "SANDBOX_OOM",
    ),
    (
        (
            # bwrap's own message when unshare(CLONE_NEWUSER) is refused.
            # This is the most common failure on a container that is not
            # privileged, and it contains none of the fragments below, so it
            # needs its own entry. Without it the failure classifies as
            # SANDBOX_VIOLATION and the operator-facing hint is lost.
            "no permissions to create new namespace",
            "non-privileged user namespaces",
            "operation not permitted",
            "can't mount",
            "unshare",
            "permission denied",
        ),
        "SANDBOX_NO_USERNS",
    ),
)

# Operator-facing hint per classified code. These are static strings on
# purpose: ``detail`` reaches the agent and the model, so it must not carry
# anything derived from the host. The hint names the fix, not the path.
_CODE_HINTS = {
    "SANDBOX_NO_USERNS": (
        "bwrap could not create a namespace; the container needs "
        "privileged: true (or CAP_SYS_ADMIN)"
    ),
    "SANDBOX_NO_PYTHON": (
        "python3 is not on the sandbox PATH; check the /usr/bin/python3 "
        "symlink in the image"
    ),
    "SANDBOX_OOM": (
        "the sandbox hit its memory cap; raise SANDBOX_MEMORY_MB"
    ),
}


def _bind_paths() -> list:
    """Return the read-only bind targets, resolved and de-duplicated.

    ``/lib`` and ``/lib64`` are symlinks into ``/usr`` on bookworm, so binding
    them as written makes bwrap self-bind ``/usr/lib`` onto ``/usr/lib``. That
    is harmless, but it means the command does not say what it does - and the
    "read-only root" claim in the module docstring is only honest if the bind
    set is the set of paths actually mounted.

    Resolving first, then dropping any path already covered by a shorter one,
    keeps the bind set minimal. On a host where ``/lib`` is a real directory it
    is not covered by ``/usr`` and is kept.
    """
    resolved: list = []
    for path in _RAW_BIND_PATHS:
        if not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        if real not in resolved:
            resolved.append(real)

    minimal: list = []
    for path in sorted(resolved, key=len):
        if any(
            path == kept or path.startswith(kept.rstrip("/") + "/")
            for kept in minimal
        ):
            continue
        minimal.append(path)
    return minimal


def _classify_stderr(stderr: str) -> str:
    """Map a bwrap/prlimit stderr line to a specific sandbox error code.

    Returns ``SANDBOX_VIOLATION`` when nothing matches, so an unrecognised
    failure is still reported rather than swallowed. The stderr text itself
    never leaves this function: it can contain host paths, and ``detail``
    flows back through ``dispatch`` to the agent and the model.
    """
    lowered = stderr.lower()
    for fragments, code in _STDERR_SIGNATURES:
        if any(fragment in lowered for fragment in fragments):
            return code
    return "SANDBOX_VIOLATION"


def _probe_command() -> list:
    """The boot-time smoke test: a real bwrap invocation, not ``--version``.

    It mirrors ``_bwrap_command``'s namespace and mount flags and then runs
    ``python3 -c pass``, so it exercises the two things that actually break:
    the mount set (needs ``CAP_SYS_ADMIN`` or user namespaces) and the
    ``python3`` lookup on the sandbox ``PATH``. A ``--version`` check would
    pass on a host where every real call fails.
    """
    cmd = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
    ]
    for path in _bind_paths():
        cmd.extend(["--ro-bind", path, path])
    cmd.extend([
        "--proc", "/proc",
        "--dev", "/dev",
        "--chdir", "/",
        "python3", "-c", "pass",
    ])
    return cmd


def preflight() -> None:
    """Fail fast if this host cannot actually run the sandbox.

    Checking that the binaries exist is not enough. A container without
    ``CAP_SYS_ADMIN`` has ``bwrap`` and ``prlimit`` on ``PATH`` and still
    fails every ``mount()``; ``--unshare-all`` is ``--unshare-user-try``, so
    such a host does not fail at the unshare either - it falls back and dies
    at the first mount. The only way to tell a working host from a broken one
    is to run a real invocation, which is what the probe below does.

    Without this, a broken host boots cleanly and then returns
    ``SANDBOX_VIOLATION`` on every tool call. That reads as a broken tool
    rather than a broken deployment, and the model sees a transcript full of
    failures with no way to say why.
    """
    missing = [name for name in _REQUIRED_BINARIES if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "sandbox unavailable: missing "
            + ", ".join(missing)
            + " (install bubblewrap and util-linux)"
        )

    try:
        proc = subprocess.run(
            _probe_command(), capture_output=True, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("sandbox unavailable: bwrap probe timed out")
    except OSError as exc:
        raise RuntimeError(
            f"sandbox unavailable: bwrap probe could not run: {exc}"
        )

    if proc.returncode != 0:
        # This message goes to the boot log, not to an API client, so the raw
        # stderr is safe here - and it is the whole point of the probe.
        stderr = proc.stderr.decode("utf-8", "replace")[:200].strip()
        raise RuntimeError(
            "sandbox unavailable: bwrap probe failed "
            f"({_classify_stderr(stderr)}): {stderr}"
        )


@dataclass
class _SpawnResult:
    error: Optional[str] = None
    detail: str = ""
    envelope: Optional[Dict[str, Any]] = None


def _snapshot(
    tool: str,
    arguments: Dict[str, Any],
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the tool-scoped snapshot from the call's selector arguments.

    Both ``_build_request`` and ``_redeci`` go through here so the sandbox and
    the control plane see identical snapshots; otherwise every read would be
    reported as ``stale``.

    ``as_of`` is the reference date frozen into the request envelope. It is
    only used when the caller did not pass an explicit ``as_of`` argument, so
    a call that straddles UTC midnight still compares equal on both sides.
    """
    ids = {key: arguments[key] for key in _SNAPSHOT_ARG_KEYS if key in arguments}
    if "as_of" not in ids and as_of is not None:
        ids["as_of"] = as_of
    return tools._snapshot_for(tool, **ids)


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
        "snapshot": _snapshot(tool, arguments),
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
    # ``/usr/local`` is load-bearing, not cosmetic. The runtime image is
    # ``python:3.11-slim-bookworm``, which installs the interpreter at
    # ``/usr/local/bin/python3``; the Dockerfile adds
    # ``/usr/bin/python3 -> /usr/local/bin/python3`` so the ``PATH`` above can
    # find it. If that symlink is missing, ``execvp`` fails with ENOENT and
    # bwrap's ``die()`` exits 1 - which surfaces as ``SANDBOX_NO_PYTHON`` on
    # every single tool call. The stderr line to look for in the server log is
    # ``bwrap: execvp python3: No such file or directory``.
    for path in _bind_paths():
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
        # Log stderr server-side only. It can contain host paths, and
        # ``detail`` flows back through ``dispatch`` to the agent and model.
        # The *code* is classified from it, so the caller learns which class
        # of failure this is without being handed the path.
        stderr = proc.stderr.decode("utf-8", "replace")[:512]
        code = _classify_stderr(stderr)
        _LOG.warning(
            "sandbox violation: run_id=%s exit=%s code=%s stderr=%r",
            request.get("run_id"),
            proc.returncode,
            code,
            stderr,
        )
        detail = f"exit {proc.returncode}"
        hint = _CODE_HINTS.get(code)
        if hint:
            detail = f"{detail}: {hint}"
        return _SpawnResult(error=code, detail=detail)

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
    # Reuse the reference date frozen into the request so a call that straddles
    # UTC midnight does not spuriously diverge from the sandbox's snapshot.
    frozen_as_of = (request.get("snapshot") or {}).get("as_of")
    try:
        live = _snapshot(tool, arguments, as_of=frozen_as_of)
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
        data = proposal.get("data", {})
        if not isinstance(data, dict):
            return {"ok": False, "error": "MALFORMED_PROPOSAL", "kind": kind}
        # ``ok`` is written last so a ``data`` key named ``ok`` (or ``error``)
        # cannot turn a read into a success the control plane did not decide.
        result = dict(data)
        result["ok"] = True
        return result
    if kind == "transition":
        return tools._apply_transition(proposal, correlation_id)
    if kind == "create_approval":
        return tools._apply_create_approval(proposal, correlation_id)
    if kind == "create_requisition":
        return tools._apply_create_requisition(proposal, correlation_id)
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
