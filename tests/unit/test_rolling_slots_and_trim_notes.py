"""Rolling demo calendar and hidden excerpt notes."""

from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.conversation import ASSISTANT_GAP_TEXT, CURRENT_GAP_TEXT, GAP_TEXT, strip_trim_notes
from app.models import Appointment, ApprovalRequest, ConversationMessage, Payment, Service, Slot, utcnow
from app.seed import SLOT_HORIZON_DAYS, ensure_future_slots
from tests.conftest import chat

CHAT_HTML = Path(__file__).resolve().parents[2] / "app" / "web" / "templates" / "chat.html"


def _future_open(db, svc_id, now):
    return db.scalar(
        select(func.count())
        .select_from(Slot)
        .where(Slot.service_id == svc_id, Slot.status == "OPEN", Slot.starts_at > now)
    )


def test_every_service_has_open_times_through_the_horizon(env, db_session):
    now = utcnow()
    end = now + timedelta(days=SLOT_HORIZON_DAYS - 2)
    for svc in db_session.execute(select(Service)).scalars():
        last = db_session.scalar(select(func.max(Slot.starts_at)).where(Slot.service_id == svc.id))
        assert last >= end, svc.code
        assert _future_open(db_session, svc.id, now) >= 3


def test_generator_is_insert_only_and_idempotent(env, db_session):
    before = {s.id: (s.status, s.starts_at) for s in db_session.execute(select(Slot)).scalars()}
    counts = [db_session.scalar(select(func.count()).select_from(m)) for m in (Appointment, Payment, ApprovalRequest)]
    assert ensure_future_slots(db_session) == 0  # seed already filled the horizon
    later = utcnow() + timedelta(days=20)
    added = ensure_future_slots(db_session, later)
    db_session.commit()
    assert added > 0
    after = {s.id: (s.status, s.starts_at) for s in db_session.execute(select(Slot)).scalars()}
    assert all(after[k] == v for k, v in before.items())
    models = (Appointment, Payment, ApprovalRequest)
    assert [db_session.scalar(select(func.count()).select_from(m)) for m in models] == counts
    assert ensure_future_slots(db_session, later) == 0
    sundays = db_session.scalar(
        select(func.count())
        .select_from(Slot)
        .where(Slot.id.notin_(list(before)), func.strftime("%w", Slot.starts_at) == "0")
    )
    assert sundays == 0


def test_stale_calendar_tops_up_when_a_patient_asks(env, patient, db_session):
    now = utcnow()
    svc = db_session.execute(select(Service).where(Service.code == "LAB-CMP")).scalar_one_or_none()
    svc = svc or db_session.execute(select(Service).where(Service.name.like("Lab panel%"))).scalar_one()
    stale = list(db_session.execute(select(Slot).where(Slot.service_id == svc.id, Slot.starts_at > now)).scalars())
    for s in stale:  # simulate a demo database whose times have all passed
        s.starts_at = s.starts_at - timedelta(days=60)
    db_session.commit()
    assert _future_open(db_session, svc.id, now) == 0
    reply = chat(patient, "book a lab panel")
    assert "No open times" not in reply["text"]
    db_session.expire_all()
    assert _future_open(db_session, svc.id, now) >= 3


def test_strip_trim_notes_removes_every_excerpt_note():
    for note in (GAP_TEXT, ASSISTANT_GAP_TEXT, CURRENT_GAP_TEXT):
        assert strip_trim_notes("Price is $59.00.\n" + note) == "Price is $59.00."
    assert strip_trim_notes("Plain reply.") == "Plain reply."


def test_restored_transcript_hides_old_excerpt_notes(env, patient, db_session):
    chat(patient, "lab price")
    q = select(ConversationMessage).where(ConversationMessage.speaker == "assistant")
    row = db_session.execute(q.order_by(ConversationMessage.id.desc())).scalars().first()
    row.body = row.body + "\n" + ASSISTANT_GAP_TEXT
    db_session.commit()
    bodies = [m["body"] for m in patient.get("/api/chat/transcript").json()["messages"]]
    assert not any("model excerpt" in b for b in bodies)
    db_session.expire_all()
    assert ASSISTANT_GAP_TEXT in db_session.get(ConversationMessage, row.id).body  # stored text is untouched


def test_chat_page_shows_no_excerpt_notes():
    html = CHAT_HTML.read_text()
    for phrase in ("model excerpt", "left out of the", "Restored ", "excerpt limit"):
        assert phrase not in html
