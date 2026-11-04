"""
app.py
======
ProcureLedger - HTTP surface.

The agent is reachable only through ``/agent/run``. Human approval and payout
settlement are deliberately separate endpoints so a model can never settle a
payout on its own. ``/audit`` and ``/audit/verify`` read the on-disk chain.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import agent
import config
import tools

app = FastAPI(title="ProcureLedger")


class RunRequest(BaseModel):
    message: str
    correlation_id: str = ""


class IngestRequest(BaseModel):
    invoice_id: str
    vendor_id: str
    po_id: str
    amount: float
    currency: str = "USD"
    correlation_id: str = ""


class ApproveRequest(BaseModel):
    approver: str
    correlation_id: str = ""


class ExecuteRequest(BaseModel):
    correlation_id: str = ""


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.post("/agent/run")
def run_agent(req: RunRequest) -> Dict[str, Any]:
    try:
        return agent.run(req.message, correlation_id=req.correlation_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/audit")
def read_audit() -> Dict[str, Any]:
    path = config.AUDIT_LOG_PATH
    if not path or not os.path.exists(path):
        return {"ok": True, "entries": []}
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return {"ok": True, "entries": entries}


@app.get("/audit/verify")
def verify_audit(from_file: bool = True) -> Dict[str, Any]:
    return tools.verify_audit_chain(from_file=from_file)


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> Dict[str, Any]:
    return tools.get_invoice(invoice_id)


@app.post("/invoices")
def ingest_invoice(req: IngestRequest) -> Dict[str, Any]:
    return tools.ingest_invoice(
        req.invoice_id,
        req.vendor_id,
        req.po_id,
        req.amount,
        currency=req.currency,
        correlation_id=req.correlation_id,
    )


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: str) -> Dict[str, Any]:
    return tools.get_approval(approval_id)


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, req: ApproveRequest) -> Dict[str, Any]:
    return tools.approve_payout(
        approval_id, req.approver, correlation_id=req.correlation_id
    )


@app.post("/approvals/{approval_id}/execute")
def execute(approval_id: str, req: ExecuteRequest) -> Dict[str, Any]:
    return tools._execute_payout(approval_id, correlation_id=req.correlation_id)
