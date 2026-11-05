"""
worker.py
=========
Sandboxed decision worker for ProcureLedger.

Runs under ``bwrap`` (see ``sandbox._bwrap_command``) with no network, a
read-only root, and a fresh tmpfs workspace. It reads one request envelope from
stdin, re-runs ``decisions.decide`` against the snapshot carried in that
envelope, and writes one response envelope to stdout.

Contract
--------
Request  (built by ``sandbox._build_request``):
    {"contract_version", "run_id", "correlation_id", "tool", "arguments",
     "principal", "snapshot", "limits"}

Response (validated by ``sandbox._validate``):
    {"contract_version", "run_id", "ok": bool, "proposal"?: {...},
     "error"?: str, "detail"?: str}

The worker always exits 0. ``sandbox._spawn`` treats a non-zero exit as
``SANDBOX_VIOLATION`` and discards stdout, so a structured error must be
reported in the envelope, not via the exit code. Tracebacks are never emitted:
they would leak host paths across the sandbox boundary.
"""

from __future__ import annotations

import json
import sys

import decisions


def _emit(envelope: dict) -> None:
    sys.stdout.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _fail(run_id: str, code: str, detail: str = "") -> dict:
    return {
        "contract_version": decisions.CONTRACT_VERSION,
        "run_id": run_id,
        "ok": False,
        "error": code,
        "detail": detail,
    }


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        _emit(_fail("", "EMPTY_REQUEST", "no request envelope on stdin"))
        return 0

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        _emit(_fail("", "MALFORMED_REQUEST", str(exc)))
        return 0

    if not isinstance(request, dict):
        _emit(_fail("", "MALFORMED_REQUEST", "request is not an object"))
        return 0

    run_id = request.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _emit(_fail("", "MALFORMED_REQUEST", "missing run_id"))
        return 0

    if request.get("contract_version") != decisions.CONTRACT_VERSION:
        _emit(
            _fail(
                run_id,
                "CONTRACT_VERSION_MISMATCH",
                f"worker speaks {decisions.CONTRACT_VERSION}",
            )
        )
        return 0

    tool = request.get("tool")
    if not isinstance(tool, str) or not tool:
        _emit(_fail(run_id, "MALFORMED_REQUEST", "missing tool"))
        return 0

    try:
        decision = decisions.decide(
            tool, request.get("arguments"), request.get("snapshot")
        )
    except Exception:
        # Never leak a traceback across the boundary. The control plane
        # re-decides on live state, so a crash here fails closed.
        _emit(_fail(run_id, "SANDBOX_INTERNAL_ERROR", "decision raised"))
        return 0

    envelope = {
        "contract_version": decisions.CONTRACT_VERSION,
        "run_id": run_id,
        "ok": bool(decision.get("ok")),
    }
    if envelope["ok"]:
        envelope["proposal"] = decision.get("proposal")
    else:
        envelope["error"] = decision.get("error")
        envelope["detail"] = decision.get("detail", "")

    _emit(envelope)
    return 0


if __name__ == "__main__":
    sys.exit(main())
