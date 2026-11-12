"""Database tables. Money is stored as integer cents."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Clinic(Base):
    __tablename__ = "clinics"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(120))


class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    price_cents: Mapped[int] = mapped_column(Integer)
    includes: Mapped[str] = mapped_column(Text)
    excludes: Mapped[str] = mapped_column(Text)
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    keywords: Mapped[str] = mapped_column(Text, default="")
    # "visit" = one posted total. "unit" = price_cents is per unit; quantity is entered, never assumed.
    price_unit: Mapped[str] = mapped_column(String(20), default="visit", server_default="visit")


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True)


class StaffUser(Base):
    __tablename__ = "staff_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[str] = mapped_column(String(30))  # staff | billing_lead


class Slot(Base):
    __tablename__ = "slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | HELD | BOOKED
    clinic: Mapped[Clinic] = relationship()
    service: Mapped[Service] = relationship()


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    status: Mapped[str] = mapped_column(String(20), default="HELD")  # HELD|CONFIRMED|CANCELLED|EXPIRED
    quote_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    small_actions: Mapped[dict] = mapped_column(JSON, default=dict)
    fhir_appointment_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    patient: Mapped[Patient] = relationship()
    slot: Mapped[Slot] = relationship()
    service: Mapped[Service] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"))
    kind: Mapped[str] = mapped_column(String(20))  # FULL | DEPOSIT | PLAN | INVOICE
    intent: Mapped[str] = mapped_column(String(20))  # CAPTURE | AUTHORIZE | PLAN | INVOICE
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="CREATED")
    funding: Mapped[str] = mapped_column(String(20), default="paypal")  # paypal | venmo
    request_id: Mapped[str] = mapped_column(String(80), unique=True)
    paypal_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    paypal_capture_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    paypal_authorization_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    paypal_invoice_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    approve_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    plan_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    appointment: Mapped[Appointment] = relationship()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # REFUND | DISCOUNT
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING|APPROVED|DENIED|EXECUTED|FAILED
    required_role: Mapped[str] = mapped_column(String(30), default="staff")
    injection_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_by: Mapped[str] = mapped_column(String(80))
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    appointment: Mapped[Appointment] = relationship()
    payment: Mapped[Payment | None] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60))
    decision: Mapped[str] = mapped_column(String(30))
    rule: Mapped[str] = mapped_column(String(120), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_webhook_event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="PROCESSED")  # PROCESSED | PARKED
    raw: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyRecord(Base):
    """Duplicate-request guard for client POSTs (double-clicks, retries)."""

    __tablename__ = "idempotency_records"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    """One saved chat for one synthetic patient. Identity comes from the server session."""

    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | handed_off
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    patient: Mapped[Patient] = relationship()


class ConversationMessage(Base):
    """Exact stored line. seq is the order. The model prompt may omit lines; this table does not."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_conversation_message_seq"),
        UniqueConstraint("conversation_id", "client_key", name="uq_conversation_client_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20))  # patient | assistant | staff
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff_users.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    client_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stored")  # stored | answered | unavailable
    reply_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    conversation: Mapped[Conversation] = relationship()


class ConversationHandoff(Base):
    """Staff review queue. Creating one does not send email, SMS, or a payment."""

    __tablename__ = "conversation_handoffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), unique=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    reason: Mapped[str] = mapped_column(String(20))  # clinical | requested
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | reviewed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    conversation: Mapped[Conversation] = relationship()
    patient: Mapped[Patient] = relationship()
