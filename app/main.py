"""FastAPI app: landing, patient chat, checkout, staff console, webhooks."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import audit, conversation
from app import db as dbm
from app.agent import Agent
from app.config import Settings, load_settings
from app.conversation import MAX_ACCEPTED_MESSAGE_CHARS, MessageRejected, accepted_message
from app.guard import Guard
from app.llm import OpenAIInterpreter
from app.models import ApprovalRequest, AuditEvent, ConversationMessage, Patient, Payment, StaffUser
from app.payments.fake import FakePayPalAdapter
from app.payments.sandbox import SandboxPayPalAdapter
from app.pricing import is_per_unit, money
from app.services import Clinic, FlowError

WEB = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=WEB / "templates")
MOCK_CODES = {"INSTRUMENT_DECLINED", "INSUFFICIENT_FUNDS", "TRANSACTION_REFUSED"}


def _keep_exact_message(v: str) -> str:
    try:
        return accepted_message(v)
    except MessageRejected as exc:
        raise ValueError(exc.code) from exc


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_ACCEPTED_MESSAGE_CHARS)
    client_message_id: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_-]{8,80}$")

    @field_validator("message")
    @classmethod
    def keep_exact(cls, v: str) -> str:
        return _keep_exact_message(v)


class RetryIn(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]{8,80}$")


class StaffReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_ACCEPTED_MESSAGE_CHARS)

    @field_validator("message")
    @classmethod
    def keep_exact(cls, v: str) -> str:
        return _keep_exact_message(v)


class ConfirmIn(BaseModel):
    order_id: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9\-]+$")
    test_outcome: str | None = Field(default=None, pattern=r"^(approve|decline)$")


class DecideIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=MAX_ACCEPTED_MESSAGE_CHARS)


class ResetIn(BaseModel):
    confirm: str = Field(max_length=20)


class AnalystIn(BaseModel):
    question: str = Field(min_length=2, max_length=300)


def build_adapter(s: Settings):
    if s.payment_mode == "sandbox":
        return SandboxPayPalAdapter(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url, s.paypal_webhook_id)
    return FakePayPalAdapter(s.fake_webhook_key)


def create_app(
    settings: Settings | None = None,
    adapter=None,
    create_schema: bool = True,
    fhir_client=None,
    llm_transport=None,
) -> FastAPI:
    s = settings or load_settings()
    engine = dbm.init_engine(s.database_url)
    pp = adapter or build_adapter(s)
    guard = Guard()
    interpreter = OpenAIInterpreter(s, transport=llm_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if create_schema:
            dbm.Base.metadata.create_all(engine)
        from app.seed import seed

        with dbm.SessionLocal() as session:
            seed(session)
        yield

    app = FastAPI(title="Patient-cashpay_AI-agent", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=s.session_secret, same_site="lax", max_age=8 * 3600)
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    app.state.settings, app.state.adapter = s, pp

    def clinic(session: Session = Depends(dbm.get_session)) -> Clinic:
        return Clinic(session, pp, guard, s.fhir_base_url, fhir_client)

    def patient(request: Request, session: Session = Depends(dbm.get_session)) -> Patient:
        pid = request.session.get("patient_id")
        p = session.get(Patient, pid) if pid else None
        if not p:
            raise HTTPException(401, "Please start from the home page (click Patient in the role bar).")
        return p

    def staff(request: Request, session: Session = Depends(dbm.get_session)) -> StaffUser:
        sid = request.session.get("staff_id")
        u = session.get(StaffUser, sid) if sid else None
        if not u:
            raise HTTPException(401, "Please start from the home page (click Staff in the role bar).")
        return u

    def ctx(request: Request, **kw):
        return {
            "request": request,
            "mode": s.payment_mode,
            "llm_mode": s.llm_mode,
            "openai_model": s.openai_model,
            "max_accepted_chars": MAX_ACCEPTED_MESSAGE_CHARS,
            "prompt_char_budget": conversation.prompt_budget(),
            "ag_grid_version": AG_GRID_VERSION,
            "ag_charts_version": AG_CHARTS_VERSION,
            "demo_reset": demo_reset_allowed(s),
            **kw,
        }

    # ---------------------------------------------------------------- pages
    @app.get("/health")
    def health(session: Session = Depends(dbm.get_session)):
        session.execute(text("select 1"))
        body = {
            "ok": True,
            "payment_mode": s.payment_mode,
            "llm_mode": s.llm_mode,
            "fhir": bool(s.fhir_base_url),
            "openai_configured": s.openai_configured,
        }
        if s.llm_mode == "openai":
            body["openai_model"] = s.openai_model
        return body

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        threshold = money(guard.caps["billing_lead_refund_threshold_cents"])
        return templates.TemplateResponse(request, "landing.html", ctx(request, refund_threshold=threshold))

    @app.post("/demo/login/{who}")
    def demo_login(who: str, request: Request, session: Session = Depends(dbm.get_session)):
        request.session.clear()
        if who == "patient":
            p = session.execute(select(Patient).order_by(Patient.id).limit(1)).scalar_one()
            request.session["patient_id"] = p.id
            return RedirectResponse("/patient", status_code=303)
        if who in ("staff", "billing_lead"):
            u = session.execute(select(StaffUser).where(StaffUser.role == who)).scalars().first()
            request.session["staff_id"] = u.id
            return RedirectResponse("/staff", status_code=303)
        raise HTTPException(404)

    @app.post("/demo/login/patient/{patient_id}")
    def demo_login_patient(patient_id: int, request: Request, session: Session = Depends(dbm.get_session)):
        person = session.get(Patient, patient_id)
        if person is None:
            raise HTTPException(404, "Unknown synthetic patient.")
        request.session.clear()
        request.session["patient_id"] = person.id
        return RedirectResponse("/patient", status_code=303)

    @app.get("/patient", response_class=HTMLResponse)
    def patient_page(request: Request, p: Patient = Depends(patient)):
        return templates.TemplateResponse(request, "chat.html", ctx(request, patient=p))

    @app.get("/pay/{payment_id}", response_class=HTMLResponse)
    def pay_page(payment_id: int, request: Request, p: Patient = Depends(patient), c: Clinic = Depends(clinic)):
        pay = _own_payment(c, p, payment_id)
        return templates.TemplateResponse(
            request,
            "pay.html",
            ctx(
                request,
                pay=pay,
                amount=money(pay.amount_cents),
                service=(
                    f"{pay.appointment.service.name} x {pay.appointment.quantity} units"
                    if is_per_unit(pay.appointment.service)
                    else pay.appointment.service.name
                ),
                client_id=s.paypal_client_id if s.payment_mode == "sandbox" else "",
            ),
        )

    @app.get("/staff", response_class=HTMLResponse)
    def staff_page(request: Request, u: StaffUser = Depends(staff)):
        threshold = money(guard.caps["billing_lead_refund_threshold_cents"])
        return templates.TemplateResponse(request, "staff.html", ctx(request, user=u, refund_threshold=threshold))

    # ---------------------------------------------------------------- patient API

    @app.get("/api/chat/transcript")
    def chat_transcript(p: Patient = Depends(patient), c: Clinic = Depends(clinic)):
        return conversation.transcript(c.db, p)

    @app.post("/api/chat")
    def chat(body: ChatIn, request: Request, p: Patient = Depends(patient), c: Clinic = Depends(clinic)):
        c.expire_holds()
        try:
            outcome = conversation.exchange(
                c.db,
                Agent(c, settings=s, interpreter=interpreter),
                p,
                body.message,
                body.client_message_id,
                request.session.get("unit_quote"),
            )
        except MessageRejected as exc:
            raise HTTPException(422, exc.code) from exc
        return _apply_quote(request, outcome)

    @app.post("/api/chat/retry")
    def chat_retry(body: RetryIn, request: Request, p: Patient = Depends(patient), c: Clinic = Depends(clinic)):
        c.expire_holds()
        try:
            outcome = conversation.retry_saved(
                c.db,
                Agent(c, settings=s, interpreter=interpreter),
                p,
                body.client_message_id,
                request.session.get("unit_quote"),
            )
        except KeyError:
            raise HTTPException(404, "That saved message is not in this chat.") from None
        return _apply_quote(request, outcome)

    @app.get("/api/payments/{payment_id}")
    def payment_status(payment_id: int, p: Patient = Depends(patient), c: Clinic = Depends(clinic)):
        return _pay_json(_own_payment(c, p, payment_id))

    @app.post("/api/payments/{payment_id}/confirm")
    def confirm(
        payment_id: int,
        body: ConfirmIn,
        p: Patient = Depends(patient),
        c: Clinic = Depends(clinic),
        idempotency_key: str | None = Header(default=None, max_length=120),
    ):
        pay = _own_payment(c, p, payment_id)
        if body.order_id and body.order_id != pay.paypal_order_id:
            raise HTTPException(400, "Order does not match this payment.")
        mock = None
        if s.payment_mode == "fake":
            if pay.paypal_order_id and pay.intent in ("CAPTURE", "AUTHORIZE"):
                try:
                    pp.approve(pay.paypal_order_id)
                except Exception:
                    pass
            if body.test_outcome == "decline":
                mock = "INSTRUMENT_DECLINED"

        def run():
            try:
                return {"ok": True, **_pay_json(c.confirm_payment(pay, mock=mock))}
            except FlowError as e:
                return {"ok": False, "code": e.code, "message": e.message, **_pay_json(pay)}

        key = f"confirm:{pay.id}:{idempotency_key}" if idempotency_key else None
        return c.idempotent(key, run)

    # ---------------------------------------------------------------- staff API
    @app.get("/api/staff/conversations")
    def staff_conversations(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        return conversation.staff_queue(c.db)

    @app.get("/api/staff/conversations/{conversation_id}")
    def staff_conversation(conversation_id: int, u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        payload = conversation.staff_transcript(c.db, conversation_id)
        if payload is None:
            raise HTTPException(404, "No handed-off conversation with that id.")
        return payload

    @app.post("/api/staff/conversations/{conversation_id}/reply")
    def staff_conversation_reply(
        conversation_id: int,
        body: StaffReplyIn,
        u: StaffUser = Depends(staff),
        c: Clinic = Depends(clinic),
    ):
        try:
            return conversation.staff_say(c.db, c, u, conversation_id, body.message)
        except KeyError:
            raise HTTPException(404, "No handed-off conversation with that id.") from None
        except ValueError as exc:
            code = str(exc)
            if code == "amount":
                raise HTTPException(400, "That amount is not on the clinic price list.") from exc
            if code == "too_long":
                raise HTTPException(422, "too_long") from exc
            if code == "control":
                raise HTTPException(422, "control") from exc
            raise HTTPException(400, "Please type a message.") from exc

    @app.get("/api/staff/me")
    def me(u: StaffUser = Depends(staff)):
        return {"name": u.name, "role": u.role}

    @app.get("/api/staff/approvals")
    def approvals(collapse: bool = False, u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        rows = c.db.execute(select(ApprovalRequest).order_by(ApprovalRequest.id.desc())).scalars()
        out = [
            {
                "id": a.id,
                "kind": a.kind,
                "patient": a.appointment.patient.name,
                "service": a.appointment.service.name,
                "amount": money(a.amount_cents),
                "amount_cents": a.amount_cents,
                "percent": a.percent,
                "reason": a.reason,
                "summary": _approval_summary(a),
                "quote": _patient_quote(c.db, a),
                "status": a.status,
                "required_role": a.required_role,
                "injection_flag": a.injection_flag,
                "requested_by": a.requested_by,
                "decided_by": a.decided_by,
                "created_at": a.created_at.isoformat(),
                "_key": (a.kind, a.appointment_id, a.amount_cents, a.percent, a.reason, a.required_role),
            }
            for a in rows
        ]
        return collapse_duplicates(out) if collapse else [_strip_key(r) for r in out]

    @app.post("/api/staff/demo/reset")
    def demo_reset(body: ResetIn, u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        """Staff-only. Wipes the synthetic demo rows and seeds them again. Sandbox or local demo only."""
        if body.confirm != "RESET":
            raise HTTPException(400, "Type RESET to confirm.")
        if not demo_reset_allowed(s):
            raise HTTPException(403, "Reset demo is only available with PayPal sandbox or local demo data.")
        from app.seed import seed

        who, role = u.name, u.role
        reset_demo_data(c.db)
        seed(c.db)
        audit.record(c.db, who, "demo_reset", "OK", "staff_reset_synthetic_data", {"role": role})
        c.db.commit()
        return {"ok": True, "kpis": _kpis(c)}

    @app.post("/api/staff/approvals/{approval_id}/decide")
    def decide(approval_id: int, body: DecideIn, u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        ar = c.db.get(ApprovalRequest, approval_id)
        if not ar:
            raise HTTPException(404)
        try:
            ar = c.decide(ar, body.approve, u.name, u.role, body.note)
        except FlowError as e:
            raise HTTPException(403, e.message) from e
        return {"id": ar.id, "status": ar.status, "note": ar.decision_note}

    @app.get("/api/staff/payments")
    def payments(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        rows = c.db.execute(select(Payment).order_by(Payment.id.desc())).scalars()
        return [
            {
                **_pay_json(p),
                "patient": p.appointment.patient.name,
                "service": p.appointment.service.name,
                "location": p.appointment.slot.clinic.location,
                "created_at": p.created_at.isoformat(),
            }
            for p in rows
        ]

    @app.post("/api/staff/payments/{payment_id}/capture-deposit")
    def capture_deposit(payment_id: int, u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        pay = c.db.get(Payment, payment_id)
        if not pay:
            raise HTTPException(404)
        try:
            return _pay_json(c.capture_deposit(pay, u.role, u.name))
        except FlowError as e:
            raise HTTPException(400, e.message) from e

    @app.post("/api/staff/reconcile")
    def reconcile(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        return c.reconcile()

    @app.get("/api/staff/recon")
    def recon(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        return c.recon_report()

    @app.get("/api/staff/audit")
    def audit_rows(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        ok = audit.row_status(c.db)
        rows = c.db.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(300)).scalars()
        return [
            {
                "id": e.id,
                "ts": e.ts.isoformat() + "Z",
                "actor": e.actor,
                "action": e.action,
                "decision": e.decision,
                "rule": e.rule,
                "details": json.dumps(e.details),
                "hash": e.hash[:12],
                "chain_ok": ok.get(e.id, False),
            }
            for e in rows
        ]

    @app.get("/api/staff/audit/verify")
    def audit_verify(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        return audit.verify_chain(c.db)

    @app.get("/api/staff/kpis")
    def kpis(u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        return _kpis(c)

    @app.post("/api/staff/analyst")
    def analyst(body: AnalystIn, u: StaffUser = Depends(staff), c: Clinic = Depends(clinic)):
        return billing_analyst(c, body.question)

    # ---------------------------------------------------------------- webhooks
    @app.post("/webhooks/paypal")
    async def webhook(request: Request, c: Clinic = Depends(clinic)):
        body = await request.body()
        if not pp.verify_webhook(dict(request.headers), body):
            audit.record(c.db, "paypal", "webhook_signature", "DENIED", "bad_signature", {"bytes": len(body)})
            c.db.commit()
            return JSONResponse({"error": "bad signature"}, status_code=400)
        try:
            event = json.loads(body)
            return {"result": c.handle_webhook(event)}
        except (ValueError, FlowError):
            return JSONResponse({"error": "bad event"}, status_code=400)

    return app


AG_GRID_VERSION = "36.2.0"  # app/web/static/ag-grid-community.min.js (MIT)
AG_CHARTS_VERSION = "14.2.0"  # app/web/static/ag-charts-community.min.js (MIT)


def demo_reset_allowed(s: Settings) -> bool:
    """Reset only touches synthetic data: local demo mode, or PayPal sandbox (never a live PayPal host)."""
    import os

    if os.getenv("DEMO_RESET", "1").strip() == "0":
        return False
    if s.payment_mode == "fake":
        return True
    return s.payment_mode == "sandbox" and "sandbox.paypal.com" in (s.paypal_base_url or "")


def reset_demo_data(db: Session) -> None:
    """Delete every app row (schema stays). Seed runs again afterwards."""
    tables = [t for t in reversed(dbm.Base.metadata.sorted_tables)]
    if db.get_bind().dialect.name == "postgresql":
        names = ", ".join(f'"{t.name}"' for t in tables)
        db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    else:
        for t in tables:
            db.execute(t.delete())
        has_seq = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
        ).first()
        if has_seq:
            db.execute(text("DELETE FROM sqlite_sequence"))
    db.commit()
    db.expire_all()


def _patient_quote(db: Session, a: ApprovalRequest) -> str:
    """The patient's exact chat line behind this request, if it is in the saved transcript."""
    reason = a.reason or ""
    said = reason[len("[flagged] ") :] if reason.startswith("[flagged] ") else reason
    if not said:
        return ""
    hit = db.execute(
        select(ConversationMessage.id)
        .where(
            ConversationMessage.speaker == "patient",
            ConversationMessage.patient_id == a.appointment.patient_id,
            ConversationMessage.body == said,
        )
        .limit(1)
    ).scalar_one_or_none()
    return said if hit is not None else ""


def _approval_summary(a: ApprovalRequest) -> str:
    what = "Refund" if a.kind == "REFUND" else f"{a.percent}% discount"
    if a.injection_flag:
        return f"{what} asked in a message that tried to override the rules"
    return f"{what} requested"


def _strip_key(row: dict) -> dict:
    row = dict(row)
    row.pop("_key", None)
    return row


def collapse_duplicates(rows: list[dict]) -> list[dict]:
    """Show one row per identical pending request (same patient visit, amount, reason, role), with a count.

    Display only: each request keeps its own id and is still decided one at a time on the server.
    The visible row is the oldest pending one, so it is decided first.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[object] = []
    for r in rows:
        if r["status"] != "PENDING":
            order.append(r)
            continue
        key = r["_key"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for item in order:
        if isinstance(item, dict):
            out.append({**_strip_key(item), "count": 1, "duplicate_ids": []})
            continue
        members = sorted(groups[item], key=lambda r: r["id"])
        head = _strip_key(members[0])
        head["count"] = len(members)
        head["duplicate_ids"] = [m["id"] for m in members[1:]]
        out.append(head)
    return out


def _apply_quote(request: Request, outcome: dict) -> dict:
    quote = outcome.pop("_quote", None)
    if isinstance(quote, dict) and quote.get("code") and not quote.get("clear", True):
        request.session["unit_quote"] = {"code": quote["code"], "quantity": quote["quantity"]}
    elif not isinstance(quote, dict) or quote.get("clear", True):
        request.session.pop("unit_quote", None)
    return outcome


def _own_payment(c: Clinic, p: Patient, payment_id: int) -> Payment:
    pay = c.db.get(Payment, payment_id)
    if not pay or pay.appointment.patient_id != p.id:
        raise HTTPException(404, "Payment not found")
    return pay


def _pay_json(p: Payment) -> dict:
    return {
        "id": p.id,
        "kind": p.kind,
        "intent": p.intent,
        "amount": money(p.amount_cents),
        "amount_cents": p.amount_cents,
        "refunded": money(p.refunded_cents),
        "status": p.status,
        "funding": p.funding,
        "paypal_order_id": p.paypal_order_id,
        "paypal_capture_id": p.paypal_capture_id,
        "paypal_authorization_id": p.paypal_authorization_id,
        "paypal_invoice_id": p.paypal_invoice_id,
        "failure_reason": p.failure_reason,
        "approve_url": p.approve_url,
        "appointment_id": p.appointment_id,
        "appointment_status": p.appointment.status,
    }


def _kpis(c: Clinic) -> dict:
    pays = list(c.db.execute(select(Payment)).scalars())
    collected = sum(
        p.amount_cents - p.refunded_cents for p in pays if p.status in ("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED")
    )
    authorized = sum(p.amount_cents for p in pays if p.status == "AUTHORIZED")
    pending = c.db.execute(
        select(func.count()).select_from(ApprovalRequest).where(ApprovalRequest.status == "PENDING")
    ).scalar()
    blocks = c.db.execute(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.decision.in_(("DENIED", "FLAGGED")))
    ).scalar()
    by_service: dict[str, int] = {}
    for p in pays:
        if p.status in ("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED"):
            n = p.appointment.service.name
            by_service[n] = by_service.get(n, 0) + p.amount_cents - p.refunded_cents
    return {
        "collected": money(collected),
        "authorized_deposits": money(authorized),
        "pending_approvals": pending,
        "guard_blocks": blocks,
        "payments": len(pays),
        "by_service": [{"service": k, "dollars": v / 100} for k, v in sorted(by_service.items())],
    }


# ------------------------------------------------------------------- read-only billing analyst
def billing_analyst(c: Clinic, question: str) -> dict:
    """Read-only. Maps a plain-English question to a chart spec over server data. Cannot move money."""
    q = question.lower()
    if re.search(r"\b(refund|approve|capture|void|pay|charge)\b", q) and re.search(
        r"\b(do|issue|execute|run|make|send)\b", q
    ):
        return {
            "title": "Read-only",
            "answer": "I can only answer questions. Money actions happen in the approvals grid.",
            "chart": None,
        }
    pays = list(c.db.execute(select(Payment)).scalars())
    if "refund" in q:
        rows = [
            {"label": p.appointment.service.name, "value": p.refunded_cents / 100} for p in pays if p.refunded_cents
        ]
        agg = _sum(rows)
        return {
            "title": "Refunds by service ($)",
            "answer": f"{len(rows)} payment(s) with refunds.",
            "chart": {"type": "bar", "data": agg},
        }
    if "location" in q or "site" in q:
        rows = [
            {"label": p.appointment.slot.clinic.location, "value": (p.amount_cents - p.refunded_cents) / 100}
            for p in pays
            if p.status in ("CAPTURED", "PARTIALLY_REFUNDED")
        ]
        return {
            "title": "Collected by location ($)",
            "answer": "Net collected per location.",
            "chart": {"type": "bar", "data": _sum(rows)},
        }
    if "method" in q or "venmo" in q or "funding" in q:
        rows = [{"label": p.funding.title(), "value": 1} for p in pays if p.kind in ("FULL", "DEPOSIT")]
        return {
            "title": "Payments by method (count)",
            "answer": "PayPal vs Venmo checkouts.",
            "chart": {"type": "pie", "data": _sum(rows)},
        }
    if "status" in q or "fail" in q or "declin" in q:
        rows = [{"label": p.status.title().replace("_", " "), "value": 1} for p in pays]
        return {
            "title": "Payments by status (count)",
            "answer": "Where each payment stands.",
            "chart": {"type": "bar", "data": _sum(rows)},
        }
    rows = [
        {"label": p.appointment.service.name, "value": (p.amount_cents - p.refunded_cents) / 100}
        for p in pays
        if p.status in ("CAPTURED", "PARTIALLY_REFUNDED")
    ]
    return {
        "title": "Collected by service ($)",
        "answer": "Net collected per service. Try: refunds, location, method, or status.",
        "chart": {"type": "bar", "data": _sum(rows)},
    }


def _sum(rows):
    out: dict[str, float] = {}
    for r in rows:
        out[r["label"]] = round(out.get(r["label"], 0) + r["value"], 2)
    return [{"label": k, "value": v} for k, v in out.items()]


app = None


def get_app() -> FastAPI:
    """Factory for uvicorn (`uvicorn app.main:get_app --factory`). Compose runs Alembic first."""
    import os

    global app
    if app is None:
        app = create_app(create_schema=os.getenv("AUTO_CREATE_SCHEMA", "1") == "1")
    return app
