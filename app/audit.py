"""Append-only, hash-chained audit log. Tampering with any row breaks the chain."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, utcnow

GENESIS = "0" * 64


def _digest(prev_hash: str, ts: str, actor: str, action: str, decision: str, rule: str, details: dict) -> str:
    payload = json.dumps(
        {
            "prev": prev_hash,
            "ts": ts,
            "actor": actor,
            "action": action,
            "decision": decision,
            "rule": rule,
            "details": details,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _ts(dt) -> str:
    return dt.replace(microsecond=0).isoformat()


def record(db: Session, actor: str, action: str, decision: str, rule: str = "", details: dict | None = None):
    details = details or {}
    last = db.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)).scalar_one_or_none()
    prev = last.hash if last else GENESIS
    ts = utcnow().replace(microsecond=0)
    ev = AuditEvent(
        ts=ts,
        actor=actor,
        action=action,
        decision=decision,
        rule=rule,
        details=details,
        prev_hash=prev,
        hash=_digest(prev, _ts(ts), actor, action, decision, rule, details),
    )
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session) -> dict:
    prev = GENESIS
    rows = db.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
    bad: list[int] = []
    for ev in rows:
        expected = _digest(prev, _ts(ev.ts), ev.actor, ev.action, ev.decision, ev.rule, ev.details or {})
        if ev.prev_hash != prev or ev.hash != expected:
            bad.append(ev.id)
        prev = ev.hash
    return {"ok": not bad, "rows": len(rows), "broken_ids": bad}


def row_status(db: Session) -> dict[int, bool]:
    prev = GENESIS
    out: dict[int, bool] = {}
    for ev in db.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars():
        expected = _digest(prev, _ts(ev.ts), ev.actor, ev.action, ev.decision, ev.rule, ev.details or {})
        out[ev.id] = ev.prev_hash == prev and ev.hash == expected
        prev = ev.hash
    return out
