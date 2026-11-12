"""Synthetic clinic data. No real patients, clinics, or people."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import audit
from app.models import (
    Appointment,
    ApprovalRequest,
    Clinic,
    Patient,
    Payment,
    Service,
    Slot,
    StaffUser,
    utcnow,
)

# price_unit "visit" is one posted total. "unit" is per unit; the patient must enter a quantity.
# Medspa Botox uses an illustrative sandbox price of USD 13.00 per unit for this demo.
# It is not a treatment total and does not represent any real clinic's pricing.
SERVICES = [
    (
        "UC-VISIT",
        "Urgent care visit",
        "urgent",
        12500,
        "exam by a licensed clinician, vitals, one rapid test if needed (strep, flu or COVID)",
        "X-rays, stitches, prescriptions filled at the pharmacy",
        30,
        "urgent sick fever cough sore throat flu cold injury sprain rash",
        "visit",
    ),
    (
        "LAB-CMP",
        "Lab panel (CBC + CMP)",
        "lab",
        5900,
        "blood draw, complete blood count, comprehensive metabolic panel, results in 24-48 hours",
        "a clinician visit to discuss results",
        15,
        "lab blood test panel cbc cmp bloodwork draw",
        "visit",
    ),
    (
        "XR-CHEST",
        "Chest X-ray (2 views)",
        "imaging",
        8500,
        "two X-ray views and a radiologist read within one business day",
        "a follow-up visit",
        20,
        "xray x-ray chest imaging lungs",
        "visit",
    ),
    (
        "MRI-KNEE",
        "MRI knee without contrast",
        "imaging",
        45000,
        "the scan, a radiologist report, and images you can download",
        "contrast dye, sedation, and a specialist consult",
        45,
        "mri knee scan imaging",
        "visit",
    ),
    (
        "TELE-VISIT",
        "Telehealth visit (15 min)",
        "telehealth",
        4900,
        "a video visit with a licensed clinician and an e-prescription if appropriate",
        "lab work or in-person exams",
        15,
        "telehealth video online virtual telemedicine",
        "visit",
    ),
    (
        "MEDSPA-BOTOX",
        "Medspa Botox",
        "medspa",
        1300,
        "the per-unit sandbox price. Enter the quantity your clinician or staff authorized; "
        "the assistant does not choose one",
        "a clinician visit fee, other products, and units you did not enter",
        30,
        "botox medspa cosmetic",
        "unit",
    ),
]

PATIENTS = [
    ("Lily Bennett", "lily.bennett@patient.example"),
    ("Marcus Lee", "marcus.lee@patient.example"),
    ("Elena Torres", "elena.torres@patient.example"),
]
STAFF = [
    ("Jordan Kim", "jordan.kim@clinic.example", "staff"),
    ("Avery Patel", "avery.patel@clinic.example", "billing_lead"),
]
BOTOX_CODE = "MEDSPA-BOTOX"
BOTOX_UNIT_CENTS = 1300
BOTOX_INCLUDES = next(row[4] for row in SERVICES if row[0] == BOTOX_CODE)
_OLD_BOTOX_INCLUDES = (
    "the per-unit sandbox price for cosmetic botox. You choose how many units; nothing is added for you"
)


def _colnames(db: Session, table: str) -> set[str]:
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        rows = db.execute(text(f"PRAGMA table_info({table})")).all()
        return {row[1] for row in rows}
    rows = db.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    ).all()
    return {row[0] for row in rows}


def ensure_service_columns(db: Session) -> bool:
    """Add per-unit columns on databases created before this seed. No data reset."""
    changed = False
    if "price_unit" not in _colnames(db, "services"):
        db.execute(text("ALTER TABLE services ADD COLUMN price_unit VARCHAR(20) NOT NULL DEFAULT 'visit'"))
        changed = True
    if "quantity" not in _colnames(db, "appointments"):
        db.execute(text("ALTER TABLE appointments ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"))
        changed = True
    if changed:
        db.commit()
    return changed


def _service(row: tuple) -> Service:
    code, name, cat, price, inc, exc, duration, keywords, price_unit = row
    return Service(
        code=code,
        name=name,
        category=cat,
        price_cents=price,
        includes=inc,
        excludes=exc,
        duration_min=duration,
        keywords=keywords,
        price_unit=price_unit,
    )


def _add_slots(db: Session, clinics: list[Clinic], services: list[Service], start: datetime) -> int:
    n = 0
    for day in range(5):
        for i, svc in enumerate(services):
            for hour in (0, 2, 4):
                db.add(
                    Slot(
                        clinic_id=clinics[(day + i) % len(clinics)].id,
                        service_id=svc.id,
                        starts_at=start + timedelta(days=day, hours=hour + (i % 2)),
                        status="OPEN",
                    )
                )
                n += 1
    return n


# Rolling demo calendar: keep open times ahead so a judge never sees "No open times".
SLOT_HORIZON_DAYS = 36
SLOT_HOURS_UTC = (14, 16, 18)  # same shape as _add_slots: 14:00/16:00/18:00 (+1h for every other service)


def ensure_future_slots(db: Session, now: datetime | None = None, horizon_days: int = SLOT_HORIZON_DAYS) -> int:
    """Add OPEN slots for each service on future days (Mon-Sat) that have no slot yet.

    Only inserts rows. Never edits or deletes an existing slot, hold, booking, payment, or approval.
    Starts tomorrow, so today's list and the seeded first days stay as they are.
    """
    now = now or utcnow()
    clinics = list(db.execute(select(Clinic).order_by(Clinic.id)).scalars())
    services = list(db.execute(select(Service).order_by(Service.id)).scalars())
    if not clinics or not services:
        return 0
    first = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    last = first + timedelta(days=horizon_days)
    taken = {
        (sid, ts.date())
        for sid, ts in db.execute(
            select(Slot.service_id, Slot.starts_at).where(Slot.starts_at >= first, Slot.starts_at < last)
        )
    }
    added = 0
    for day in range(horizon_days):
        date = first + timedelta(days=day)
        if date.weekday() == 6:  # closed Sunday
            continue
        for i, svc in enumerate(services):
            if (svc.id, date.date()) in taken:
                continue
            for hour in SLOT_HOURS_UTC:
                db.add(
                    Slot(
                        clinic_id=clinics[(date.toordinal() + i) % len(clinics)].id,
                        service_id=svc.id,
                        starts_at=date + timedelta(hours=hour + (i % 2)),
                        status="OPEN",
                    )
                )
                added += 1
    if added:
        db.flush()
    return added


def _upgrade_catalog(db: Session, now: datetime) -> bool:
    """Insert the per-unit medspa service onto an existing demo database. Does not delete rows."""
    changed = False
    svc = db.execute(select(Service).where(Service.code == BOTOX_CODE)).scalar_one_or_none()
    if svc is None:
        svc = _service(next(row for row in SERVICES if row[0] == BOTOX_CODE))
        db.add(svc)
        db.flush()
        changed = True
    else:
        if svc.price_unit != "unit":
            svc.price_unit = "unit"
            changed = True
        if svc.includes == _OLD_BOTOX_INCLUDES:
            svc.includes = BOTOX_INCLUDES
            changed = True
        if svc.price_cents != BOTOX_UNIT_CENTS:
            used = db.execute(select(Appointment.id).where(Appointment.service_id == svc.id).limit(1)).first()
            if not used:
                svc.price_cents = BOTOX_UNIT_CENTS
                changed = True
    has_open = db.execute(
        select(Slot.id).where(Slot.service_id == svc.id, Slot.status == "OPEN", Slot.starts_at > now).limit(1)
    ).first()
    if not has_open:
        clinics = list(db.execute(select(Clinic).order_by(Clinic.id)).scalars())
        if clinics:
            start = (now + timedelta(days=1)).replace(hour=14)
            _add_slots(db, clinics, [svc], start)
            changed = True
    if changed:
        audit.record(db, "system", "seed_upgrade", "OK", "synthetic_data", {"service": BOTOX_CODE})
    return changed


def seed(db: Session, now: datetime | None = None) -> None:
    now = (now or utcnow()).replace(minute=0, second=0, microsecond=0)
    ensure_service_columns(db)
    if db.execute(select(Clinic).limit(1)).scalar_one_or_none():
        changed = _upgrade_catalog(db, now)
        if ensure_future_slots(db, now):
            changed = True
        if changed:
            db.commit()
        return
    clinics = [
        Clinic(name="Bayou City Family Clinic", location="6624 Fannin St, Houston, TX 77030"),
        Clinic(name="Bayou City Family Clinic", location="7200 Cambridge St, Houston, TX 77030"),
    ]
    db.add_all(clinics)
    services = [_service(row) for row in SERVICES]
    db.add_all(services)
    patients = [Patient(name=n, email=e) for n, e in PATIENTS]
    db.add_all(patients)
    db.add_all([StaffUser(name=n, email=e, role=r) for n, e, r in STAFF])
    db.flush()
    start = (now + timedelta(days=1)).replace(hour=14)
    slot_count = _add_slots(db, clinics, services, start)
    db.flush()
    # History so the staff console is not empty: one paid visit, one deposit, one pending refund ask.
    past = Slot(clinic_id=clinics[0].id, service_id=services[0].id, starts_at=now - timedelta(days=3), status="BOOKED")
    past2 = Slot(clinic_id=clinics[1].id, service_id=services[2].id, starts_at=now - timedelta(days=1), status="BOOKED")
    db.add_all([past, past2])
    db.flush()
    a1 = Appointment(
        patient_id=patients[0].id,
        slot_id=past.id,
        service_id=services[0].id,
        status="CONFIRMED",
        quote_cents=services[0].price_cents,
        quantity=1,
        small_actions={},
    )
    a2 = Appointment(
        patient_id=patients[1].id,
        slot_id=past2.id,
        service_id=services[2].id,
        status="CONFIRMED",
        quote_cents=services[2].price_cents,
        quantity=1,
        small_actions={},
    )
    db.add_all([a1, a2])
    db.flush()
    p1 = Payment(
        appointment_id=a1.id,
        kind="FULL",
        intent="CAPTURE",
        amount_cents=a1.quote_cents,
        status="CAPTURED",
        request_id="seed-pay-1",
        paypal_order_id="SEEDORDER000001",
        paypal_capture_id="SEEDCAP000001",
    )
    p2 = Payment(
        appointment_id=a2.id,
        kind="FULL",
        intent="CAPTURE",
        amount_cents=a2.quote_cents,
        status="CAPTURED",
        funding="venmo",
        request_id="seed-pay-2",
        paypal_order_id="SEEDORDER000002",
        paypal_capture_id="SEEDCAP000002",
    )
    db.add_all([p1, p2])
    db.flush()
    db.add(
        ApprovalRequest(
            kind="REFUND",
            appointment_id=a2.id,
            payment_id=p2.id,
            amount_cents=2500,
            reason="Patient reports a long wait; asks for a partial refund.",
            requested_by="agent",
            required_role="staff",
        )
    )
    slot_count += ensure_future_slots(db, now)
    audit.record(db, "system", "seed", "OK", "synthetic_data", {"services": len(services), "slots": slot_count})
    db.commit()
