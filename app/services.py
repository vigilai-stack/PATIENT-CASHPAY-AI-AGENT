"""Core flows: book, quote, pay (full / deposit / plan / invoice), approvals, webhooks, reconcile.

Every agent-reachable money step asks the guard first. Refunds and discounts only ever create an
approval request; executing them needs an APPROVED record and a staff role.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, fhir
from app.guard import Guard, GuardDenied
from app.models import (
    Appointment,
    ApprovalRequest,
    IdempotencyRecord,
    Patient,
    Payment,
    Service,
    Slot,
    WebhookEvent,
    utcnow,
)
from app.payments.adapter import OrderNotFound, PaymentAdapter, PaymentDeclined, PayPalError, PayPalTimeout
from app.pricing import deposit_cents, is_per_unit, line_total_cents, money, plan_schedule, quantity_message

log = logging.getLogger(__name__)

RANK = {
    "CREATED": 0,
    "APPROVED": 1,
    "AUTHORIZED": 2,
    "PLAN_PENDING": 1,
    "INVOICE_SENT": 2,
    "PLAN_ACTIVE": 3,
    "CAPTURED": 3,
    "PARTIALLY_REFUNDED": 4,
    "REFUNDED": 5,
}
TERMINAL = {"DECLINED", "VOIDED", "EXPIRED"}
MAX_ATTEMPTS = 3


class FlowError(Exception):
    """A patient-facing problem with a plain-language message."""

    def __init__(self, message: str, code: str = "flow_error"):
        super().__init__(message)
        self.message, self.code = message, code


def _full_note(text: str) -> str:
    """Keep a staff or patient note exactly. Refuse one that is over the accept limit."""
    from app.conversation import MAX_ACCEPTED_MESSAGE_CHARS

    note = text if isinstance(text, str) else ""
    if len(note) > MAX_ACCEPTED_MESSAGE_CHARS:
        raise FlowError("That note is too long to store.", "note_too_long")
    return note


class Clinic:
    def __init__(
        self,
        db: Session,
        adapter: PaymentAdapter,
        guard: Guard | None = None,
        fhir_base: str = "",
        fhir_client=None,
    ):
        self.db, self.pp, self.guard = db, adapter, guard or Guard()
        self.caps = self.guard.caps
        self.fhir_base, self.fhir_client = fhir_base, fhir_client

    # ------------------------------------------------------------------ helpers
    def _guard(self, actor: str, action: str, **ctx):
        try:
            d = self.guard.require(action, **ctx)
        except GuardDenied as e:
            audit.record(self.db, actor, action, "DENIED", e.decision.rule, _safe(ctx))
            self.db.commit()
            raise FlowError(e.decision.message, "guard_denied") from e
        audit.record(self.db, actor, action, "ALLOWED", d.rule, _safe(ctx))
        return d

    def idempotent(self, key: str | None, fn):
        """Return the stored response for a repeated client request (double-click, retry)."""
        if not key:
            return fn()
        rec = self.db.get(IdempotencyRecord, key)
        if rec:
            return rec.response
        resp = fn()
        self.db.add(IdempotencyRecord(key=key, response=resp))
        self.db.commit()
        return resp

    def services(self) -> list[Service]:
        return list(self.db.execute(select(Service).order_by(Service.price_cents)).scalars())

    def open_slots(self, service: Service, limit: int = 3) -> list[Slot]:
        now = utcnow()
        q = (
            select(Slot)
            .where(Slot.service_id == service.id, Slot.status == "OPEN", Slot.starts_at > now)
            .order_by(Slot.starts_at)
            .limit(limit)
        )
        found = list(self.db.execute(q).scalars())
        if len(found) < limit:
            # Rolling calendar: top up future days (insert-only), then read again.
            from app.seed import ensure_future_slots

            if ensure_future_slots(self.db, now):
                self.db.commit()
                found = list(self.db.execute(q).scalars())
        return found

    def latest_appointment(self, patient: Patient, statuses=("HELD", "CONFIRMED")) -> Appointment | None:
        q = (
            select(Appointment)
            .where(Appointment.patient_id == patient.id, Appointment.status.in_(statuses))
            .order_by(Appointment.id.desc())
            .limit(1)
        )
        return self.db.execute(q).scalar_one_or_none()

    def payments_for(self, appt: Appointment) -> list[Payment]:
        return list(
            self.db.execute(select(Payment).where(Payment.appointment_id == appt.id).order_by(Payment.id)).scalars()
        )

    # ------------------------------------------------------------------ booking
    def hold_slot(
        self, patient: Patient, slot_id: int, actor: str = "agent", quantity: int | None = None
    ) -> Appointment:
        slot = self.db.get(Slot, slot_id)
        if not slot or slot.status != "OPEN" or slot.starts_at <= utcnow():
            raise FlowError("That time is no longer available. Here are other open times.", "slot_taken")
        self._guard(actor, "hold_slot", slot_id=slot_id)
        if is_per_unit(slot.service):
            if quantity is None:
                raise FlowError(quantity_message("missing"), "quantity_required")
            try:
                quote = line_total_cents(slot.service.price_cents, quantity)
            except ValueError as e:
                raise FlowError(str(e), "bad_quantity") from e
            units = quantity
        else:
            quote = slot.service.price_cents
            units = 1
        slot.status = "HELD"
        appt = Appointment(
            patient_id=patient.id,
            slot_id=slot.id,
            service_id=slot.service_id,
            status="HELD",
            quote_cents=quote,
            quantity=units,
            small_actions={},
            hold_expires_at=utcnow() + timedelta(minutes=self.caps["hold_minutes"]),
        )
        self.db.add(appt)
        self.db.commit()
        return appt

    def expire_holds(self) -> int:
        now = utcnow()
        n = 0
        for appt in self.db.execute(select(Appointment).where(Appointment.status == "HELD")).scalars():
            if appt.hold_expires_at and appt.hold_expires_at < now:
                appt.status = "EXPIRED"
                appt.slot.status = "OPEN"
                for p in self.payments_for(appt):
                    if p.status in ("CREATED", "APPROVED"):
                        p.status = "EXPIRED"
                audit.record(self.db, "system", "expire_hold", "OK", "hold_minutes", {"appointment": appt.id})
                n += 1
        self.db.commit()
        return n

    # ------------------------------------------------------------------ payments
    def start_payment(
        self,
        appt: Appointment,
        kind: str,
        funding: str = "paypal",
        percent: int = 25,
        months: int = 3,
        actor: str = "agent",
    ) -> Payment:
        if appt.status != "HELD":
            raise FlowError("This booking isn't waiting for payment.", "not_held")
        if appt.hold_expires_at and appt.hold_expires_at < utcnow():
            self.expire_holds()
            raise FlowError(
                "Your hold expired, so the time was released. Pick a new time and I'll hold it.",
                "hold_expired",
            )
        if funding not in ("paypal", "venmo"):
            raise FlowError("Choose PayPal or Venmo.", "bad_funding")
        q = appt.quote_cents
        rid = f"cp-{appt.id}-{kind.lower()}-{uuid.uuid4().hex[:12]}"
        label = appt.service.name
        if is_per_unit(appt.service):
            label = f"{label} x {appt.quantity} units"
        desc = f"{label} (appointment {appt.id})"
        exp = utcnow() + timedelta(minutes=self.caps["order_expiry_minutes"])
        # cancel earlier unpaid attempts for this booking
        for p in self.payments_for(appt):
            if p.status in ("CREATED", "APPROVED"):
                p.status = "VOIDED"
        if kind == "FULL":
            self._guard(actor, "create_checkout", quote_cents=q, amount_cents=q, appointment=appt.id)
            o = self.pp.create_order("CAPTURE", q, rid, str(appt.id), desc)
            pay = Payment(
                appointment_id=appt.id,
                kind="FULL",
                intent="CAPTURE",
                amount_cents=q,
                funding=funding,
                request_id=rid,
                paypal_order_id=o["id"],
                status="CREATED",
                expires_at=exp,
            )
        elif kind == "DEPOSIT":
            amt = deposit_cents(q, percent)
            self._guard(
                actor,
                "create_deposit_auth",
                quote_cents=q,
                percent=percent,
                amount_cents=amt,
                appointment=appt.id,
            )
            o = self.pp.create_order("AUTHORIZE", amt, rid, str(appt.id), f"Deposit: {desc}")
            pay = Payment(
                appointment_id=appt.id,
                kind="DEPOSIT",
                intent="AUTHORIZE",
                amount_cents=amt,
                funding=funding,
                request_id=rid,
                paypal_order_id=o["id"],
                status="CREATED",
                expires_at=exp,
            )
        elif kind == "PLAN":
            self._guard(actor, "create_plan", quote_cents=q, months=months, appointment=appt.id)
            sched = plan_schedule(q, months)
            r = self.pp.create_plan(q, months, sched, rid, appt.service.name)
            pay = Payment(
                appointment_id=appt.id,
                kind="PLAN",
                intent="PLAN",
                amount_cents=q,
                funding=funding,
                request_id=rid,
                paypal_order_id=r["subscription_id"],
                plan_months=months,
                approve_url=r.get("approve_url"),
                status="PLAN_PENDING",
                expires_at=exp,
            )
        elif kind == "INVOICE":
            self._guard(actor, "create_invoice", quote_cents=q, amount_cents=q, appointment=appt.id)
            r = self.pp.create_invoice(q, appt.patient.email, desc, rid)
            pay = Payment(
                appointment_id=appt.id,
                kind="INVOICE",
                intent="INVOICE",
                amount_cents=q,
                request_id=rid,
                paypal_invoice_id=r["invoice_id"],
                status="INVOICE_SENT",
                approve_url=r.get("pay_url"),
            )
        else:
            raise FlowError("Unknown payment option.", "bad_kind")
        self.db.add(pay)
        self.db.flush()
        audit.record(
            self.db,
            actor,
            f"payment_created_{kind.lower()}",
            "OK",
            "",
            {
                "payment": pay.id,
                "amount_cents": pay.amount_cents,
                "paypal_id": pay.paypal_order_id or pay.paypal_invoice_id,
                "request_id": rid,
            },
        )
        if kind == "INVOICE":
            self._confirm(appt)
        self.db.commit()
        return pay

    def confirm_payment(self, pay: Payment, mock: str | None = None, actor: str = "patient") -> Payment:
        """Called after the buyer approves in the PayPal/Venmo popup (or the local test button)."""
        if pay.status in ("CAPTURED", "AUTHORIZED", "PLAN_ACTIVE", "PARTIALLY_REFUNDED", "REFUNDED"):
            return pay  # duplicate confirm: no second capture
        if pay.status in TERMINAL - {"DECLINED"}:
            raise FlowError("This payment link has expired. Ask me for a new one.", "expired")
        if pay.expires_at and pay.expires_at < utcnow():
            pay.status = "EXPIRED"
            audit.record(self.db, actor, "confirm_payment", "DENIED", "order_expiry_minutes", {"payment": pay.id})
            self.db.commit()
            raise FlowError("This payment link has expired. Ask me for a new one.", "expired")
        if pay.kind == "PLAN":
            pay.status = "PLAN_ACTIVE"
            self._confirm(pay.appointment)
            audit.record(self.db, actor, "plan_activated", "OK", "", {"payment": pay.id})
            self.db.commit()
            return pay
        op = self.pp.capture_order if pay.intent == "CAPTURE" else self.pp.authorize_order
        try:
            r = self._with_retry(lambda: op(pay.paypal_order_id, f"{pay.request_id}-confirm", mock))
        except PaymentDeclined as e:
            pay.status, pay.failure_reason = "DECLINED", e.code
            audit.record(self.db, actor, "confirm_payment", "DECLINED", "paypal", {"payment": pay.id, "code": e.code})
            self.db.commit()
            raise FlowError(
                "Your payment didn't go through, and you have not been charged. Your time is still held. "
                "You can try again, switch to Venmo or PayPal, or get an invoice by email.",
                "declined",
            ) from e
        except PayPalTimeout as e:
            pay.status, pay.failure_reason = "UNKNOWN", "timeout"
            audit.record(
                self.db,
                actor,
                "confirm_payment",
                "UNKNOWN",
                "timeout_after_retries",
                {"payment": pay.id, "attempts": MAX_ATTEMPTS},
            )
            self.db.commit()
            raise FlowError(
                "PayPal is slow to answer. We'll check the result automatically; you will not be charged twice.",
                "unknown",
            ) from e
        if pay.intent == "CAPTURE":
            pay.status, pay.paypal_capture_id = "CAPTURED", r["capture_id"]
        else:
            pay.status, pay.paypal_authorization_id = "AUTHORIZED", r["authorization_id"]
        self._confirm(pay.appointment)
        audit.record(self.db, actor, "confirm_payment", "OK", "", {"payment": pay.id, "status": pay.status})
        self.db.commit()
        return pay

    def _with_retry(self, fn):
        last = None
        for _ in range(MAX_ATTEMPTS):
            try:
                return fn()
            except PayPalTimeout as e:
                last = e
        raise last

    def _confirm(self, appt: Appointment):
        if appt.status == "CONFIRMED":
            return
        appt.status = "CONFIRMED"
        appt.slot.status = "BOOKED"
        appt.hold_expires_at = None
        try:
            fid = fhir.write_appointment(self.fhir_base, appt, self.fhir_client)
            if fid:
                appt.fhir_appointment_id = fid
                audit.record(self.db, "system", "fhir_appointment_write", "OK", "", {"fhir_id": fid})
        except Exception as e:  # FHIR is a side channel; never blocks the patient
            audit.record(self.db, "system", "fhir_appointment_write", "FAILED", "", {"error": str(e)[:120]})

    def capture_deposit(self, pay: Payment, staff_role: str, actor: str) -> Payment:
        """Front desk captures the authorized deposit at check-in (staff only)."""
        if staff_role not in ("staff", "billing_lead"):
            raise FlowError("Staff only.", "forbidden")
        if pay.status != "AUTHORIZED":
            raise FlowError("Only an authorized deposit can be captured.", "bad_state")
        r = self._with_retry(
            lambda: self.pp.capture_authorization(
                pay.paypal_authorization_id, pay.amount_cents, f"{pay.request_id}-checkin"
            )
        )
        pay.status, pay.paypal_capture_id = "CAPTURED", r["capture_id"]
        audit.record(self.db, actor, "capture_deposit", "OK", "staff_checkin", {"payment": pay.id})
        self.db.commit()
        return pay

    # ------------------------------------------------------------------ small agentic actions
    def _bump(self, appt: Appointment, key: str):
        sa = dict(appt.small_actions or {})
        sa[key] = sa.get(key, 0) + 1
        appt.small_actions = sa

    def extend_hold(self, appt: Appointment, actor: str = "agent") -> Appointment:
        if appt.status != "HELD":
            raise FlowError("There's no active hold to extend.", "not_held")
        self._guard(actor, "extend_hold", used=(appt.small_actions or {}).get("extend_hold", 0), appointment=appt.id)
        appt.hold_expires_at = max(appt.hold_expires_at or utcnow(), utcnow()) + timedelta(
            minutes=self.caps["small_actions"]["extend_hold_minutes"]
        )
        self._bump(appt, "extend_hold")
        self.db.commit()
        return appt

    def resend_link(self, appt: Appointment, actor: str = "agent") -> Payment | None:
        self._guard(
            actor,
            "resend_payment_link",
            used=(appt.small_actions or {}).get("resend_link", 0),
            appointment=appt.id,
        )
        self._bump(appt, "resend_link")
        self.db.commit()
        pays = [p for p in self.payments_for(appt) if p.status in ("CREATED", "APPROVED", "PLAN_PENDING")]
        return pays[-1] if pays else None

    def switch_method(self, appt: Appointment, funding: str, actor: str = "agent") -> Payment:
        self._guard(
            actor,
            "switch_payment_method",
            used=(appt.small_actions or {}).get("switch_method", 0),
            appointment=appt.id,
        )
        last = [p for p in self.payments_for(appt) if p.kind in ("FULL", "DEPOSIT")]
        kind = last[-1].kind if last else "FULL"
        self._bump(appt, "switch_method")
        return self.start_payment(appt, kind, funding=funding, actor=actor)

    # ------------------------------------------------------------------ approvals
    def request_refund(
        self,
        appt: Appointment,
        amount_cents: int | None,
        reason: str,
        requested_by: str,
        injection_flag: bool = False,
    ) -> ApprovalRequest:
        paid = [
            p for p in self.payments_for(appt) if p.paypal_capture_id and p.status in ("CAPTURED", "PARTIALLY_REFUNDED")
        ]
        if not paid:
            raise FlowError("I can't find a completed payment on this visit to refund.", "nothing_paid")
        pay = paid[-1]
        amt = amount_cents if amount_cents is not None else pay.amount_cents - pay.refunded_cents
        d = self._guard(
            requested_by,
            "request_refund",
            captured_cents=pay.amount_cents,
            refunded_cents=pay.refunded_cents,
            amount_cents=amt,
            appointment=appt.id,
        )
        ar = ApprovalRequest(
            kind="REFUND",
            appointment_id=appt.id,
            payment_id=pay.id,
            amount_cents=amt,
            reason=_full_note(reason),
            requested_by=requested_by,
            required_role=d.required_role,
            injection_flag=injection_flag,
        )
        self.db.add(ar)
        self.db.flush()
        audit.record(
            self.db,
            requested_by,
            "approval_queued",
            "PENDING",
            d.rule,
            {"approval": ar.id, "amount_cents": amt, "injection_flag": injection_flag},
        )
        self.db.commit()
        return ar

    def request_discount(
        self, appt: Appointment, percent: int, reason: str, requested_by: str, injection_flag: bool = False
    ) -> ApprovalRequest:
        d = self._guard(requested_by, "request_discount", percent=percent, appointment=appt.id)
        amt = round(appt.quote_cents * percent / 100)
        ar = ApprovalRequest(
            kind="DISCOUNT",
            appointment_id=appt.id,
            amount_cents=amt,
            percent=percent,
            reason=_full_note(reason),
            requested_by=requested_by,
            required_role=d.required_role,
            injection_flag=injection_flag,
        )
        self.db.add(ar)
        self.db.flush()
        audit.record(
            self.db,
            requested_by,
            "approval_queued",
            "PENDING",
            d.rule,
            {"approval": ar.id, "percent": percent, "injection_flag": injection_flag},
        )
        self.db.commit()
        return ar

    def decide(
        self, ar: ApprovalRequest, approve: bool, actor: str, actor_role: str, note: str = ""
    ) -> ApprovalRequest:
        if ar.status != "PENDING":
            return ar  # duplicate click
        if not self.guard.can_decide(ar.required_role, actor_role):
            audit.record(self.db, actor, "approval_decide", "DENIED", "role_threshold", {"approval": ar.id})
            self.db.commit()
            raise FlowError(f"This needs a {ar.required_role.replace('_', ' ')}.", "forbidden")
        ar.decided_by, ar.decided_at, ar.decision_note = actor, utcnow(), _full_note(note)
        if not approve:
            ar.status = "DENIED"
            audit.record(self.db, actor, "approval_decide", "DENIED", "staff_decision", {"approval": ar.id})
            self.db.commit()
            return ar
        ar.status = "APPROVED"
        audit.record(self.db, actor, "approval_decide", "APPROVED", "staff_decision", {"approval": ar.id})
        self.db.flush()
        self._execute(ar, actor, actor_role)
        self.db.commit()
        return ar

    def _execute(self, ar: ApprovalRequest, actor: str, actor_role: str):
        d = self.guard.check_execute(ar.status, ar.required_role, actor_role)
        if not d.allowed:
            raise FlowError(d.message, "forbidden")
        if ar.kind == "REFUND":
            pay = ar.payment
            if ar.amount_cents > pay.amount_cents - pay.refunded_cents:
                ar.status = "FAILED"
                audit.record(
                    self.db,
                    actor,
                    "refund_execute",
                    "DENIED",
                    "refund_le_captured_minus_refunded",
                    {"approval": ar.id},
                )
                return
            try:
                r = self.pp.refund_capture(pay.paypal_capture_id, ar.amount_cents, f"refund-{ar.id}")
            except PayPalError as e:
                ar.status = "FAILED"
                audit.record(
                    self.db,
                    actor,
                    "refund_execute",
                    "FAILED",
                    "paypal",
                    {"approval": ar.id, "error": str(e)[:120]},
                )
                return
            pay.refunded_cents += ar.amount_cents
            pay.status = "REFUNDED" if pay.refunded_cents >= pay.amount_cents else "PARTIALLY_REFUNDED"
            ar.status = "EXECUTED"
            audit.record(
                self.db,
                actor,
                "refund_execute",
                "OK",
                d.rule,
                {"approval": ar.id, "amount_cents": ar.amount_cents, "refund_id": r.get("refund_id")},
            )
        else:
            appt = ar.appointment
            paid = [p for p in self.payments_for(appt) if p.status in ("CAPTURED", "AUTHORIZED", "PLAN_ACTIVE")]
            if paid:
                ar.status, ar.decision_note = "FAILED", "Already paid: issue a refund instead."
                return
            appt.quote_cents -= ar.amount_cents
            ar.status = "EXECUTED"
            audit.record(
                self.db,
                actor,
                "discount_execute",
                "OK",
                d.rule,
                {"approval": ar.id, "new_quote": appt.quote_cents},
            )

    # ------------------------------------------------------------------ webhooks
    def handle_webhook(self, event: dict) -> str:
        eid, etype = event.get("id"), event.get("event_type", "")
        if not eid:
            raise FlowError("missing id", "bad_event")
        existing = self.db.execute(select(WebhookEvent).where(WebhookEvent.event_id == eid)).scalar_one_or_none()
        if existing and existing.status == "PROCESSED":
            return "duplicate"
        status = "PROCESSED" if self._apply_event(etype, event.get("resource") or {}) else "PARKED"
        if existing:
            existing.status = status
        else:
            self.db.add(WebhookEvent(event_id=eid, event_type=etype, status=status, raw=event))
        audit.record(self.db, "paypal", "webhook", status, etype, {"event_id": eid})
        self.db.commit()
        return status.lower()

    def retry_parked(self) -> int:
        n = 0
        for ev in self.db.execute(select(WebhookEvent).where(WebhookEvent.status == "PARKED")).scalars().all():
            if self._apply_event(ev.event_type, ev.raw.get("resource") or {}):
                ev.status = "PROCESSED"
                n += 1
        self.db.commit()
        return n

    def _find(self, resource: dict) -> Payment | None:
        rel = (resource.get("supplementary_data") or {}).get("related_ids") or {}
        ids = [resource.get("id"), rel.get("order_id"), rel.get("capture_id"), rel.get("authorization_id")]
        for link in resource.get("links") or []:
            if link.get("rel") == "up":
                ids.append(link["href"].rstrip("/").split("/")[-1])
        ids = [i for i in ids if i]
        if not ids:
            return None
        q = select(Payment).where(
            (Payment.paypal_order_id.in_(ids))
            | (Payment.paypal_capture_id.in_(ids))
            | (Payment.paypal_authorization_id.in_(ids))
            | (Payment.paypal_invoice_id.in_(ids))
        )
        return self.db.execute(q).scalars().first()

    def _advance(self, pay: Payment, new: str) -> None:
        if pay.status in ("VOIDED", "EXPIRED"):
            return
        if pay.status in RANK and RANK.get(new, -1) <= RANK[pay.status]:
            return  # never regress
        pay.status = new

    def _apply_event(self, etype: str, res: dict) -> bool:
        pay = self._find(res)
        if not pay:
            return False
        if etype == "CHECKOUT.ORDER.APPROVED":
            self._advance(pay, "APPROVED")
        elif etype == "PAYMENT.AUTHORIZATION.CREATED":
            pay.paypal_authorization_id = pay.paypal_authorization_id or res.get("id")
            self._advance(pay, "AUTHORIZED")
            self._confirm(pay.appointment)
        elif etype == "PAYMENT.CAPTURE.COMPLETED":
            pay.paypal_capture_id = pay.paypal_capture_id or res.get("id")
            self._advance(pay, "CAPTURED")
            self._confirm(pay.appointment)
        elif etype == "PAYMENT.CAPTURE.DENIED":
            if pay.status not in ("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED"):
                pay.status = "DECLINED"
        elif etype == "PAYMENT.CAPTURE.REFUNDED":
            if not pay.paypal_capture_id:
                return False  # refund before capture: park until the capture shows up
            if pay.refunded_cents == 0:
                pay.refunded_cents = pay.amount_cents
            self._advance(pay, "REFUNDED" if pay.refunded_cents >= pay.amount_cents else "PARTIALLY_REFUNDED")
        elif etype == "PAYMENT.AUTHORIZATION.VOIDED":
            pay.status = "VOIDED"
        elif etype == "BILLING.SUBSCRIPTION.ACTIVATED":
            self._advance(pay, "PLAN_ACTIVE")
            self._confirm(pay.appointment)
        elif etype == "INVOICING.INVOICE.PAID":
            self._advance(pay, "CAPTURED")
        return True

    # ------------------------------------------------------------------ reconcile
    def reconcile(self) -> dict:
        healed = 0
        for pay in self.db.execute(
            select(Payment).where(
                Payment.status.in_(("UNKNOWN", "CREATED", "APPROVED")),
                Payment.intent.in_(("CAPTURE", "AUTHORIZE")),
            )
        ).scalars():
            try:
                o = self.pp.get_order(pay.paypal_order_id)
            except OrderNotFound:
                continue
            except PayPalError:
                continue
            if o.get("capture_id"):
                pay.status, pay.paypal_capture_id = "CAPTURED", o["capture_id"]
                self._confirm(pay.appointment)
                healed += 1
            elif o.get("authorization_id"):
                pay.status, pay.paypal_authorization_id = "AUTHORIZED", o["authorization_id"]
                self._confirm(pay.appointment)
                healed += 1
            elif pay.status == "UNKNOWN":
                pay.status = "APPROVED" if o.get("status") == "APPROVED" else "CREATED"
                healed += 1
        parked = self.retry_parked()
        audit.record(self.db, "system", "reconcile", "OK", "", {"healed": healed, "parked_processed": parked})
        self.db.commit()
        return {"healed": healed, "parked_processed": parked, "report": self.recon_report()}

    def recon_report(self) -> list[dict]:
        rows = []
        for appt in self.db.execute(
            select(Appointment).where(Appointment.status == "CONFIRMED").order_by(Appointment.id)
        ).scalars():
            pays = self.payments_for(appt)
            collected = sum(
                p.amount_cents - p.refunded_cents
                for p in pays
                if p.status in ("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED")
            )
            pending = sum(p.amount_cents for p in pays if p.status in ("AUTHORIZED", "PLAN_ACTIVE", "INVOICE_SENT"))
            refunded = sum(p.refunded_cents for p in pays)
            expected = appt.quote_cents - refunded
            if collected == expected:
                status = "MATCHED"
            elif collected + pending >= expected and pending:
                status = "PENDING"
            elif collected > expected:
                status = "OVERPAID"
            elif not pays:
                status = "NO_PAYMENT"
            elif any(p.kind == "DEPOSIT" and p.status == "CAPTURED" for p in pays):
                status = "BALANCE_DUE"
            else:
                status = "UNDERPAID"
            rows.append(
                {
                    "appointment_id": appt.id,
                    "patient": appt.patient.name,
                    "service": appt.service.name,
                    "expected": money(expected),
                    "collected": money(collected),
                    "pending": money(pending),
                    "status": status,
                }
            )
        return rows


def _safe(ctx: dict) -> dict:
    return {k: v for k, v in ctx.items() if isinstance(v, (int, str, float, bool))}
