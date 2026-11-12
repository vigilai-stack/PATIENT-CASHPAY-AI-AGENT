"""Patients read clinic time (CT) and never see an internal slot id."""

import re
from datetime import datetime

from app import timefmt
from app.timefmt import to_clinic, when_ct
from tests.conftest import chat

SLOT_ID_CMD = re.compile(r"\bhold \d+")
CT_TIME = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} (AM|PM) CT\b")


def test_when_ct_converts_utc_to_houston_time():
    assert when_ct(datetime(2026, 11, 11, 14, 0)) == "Wed Nov 11, 8:00 AM CT"  # CST, UTC-6
    assert when_ct(datetime(2026, 11, 11, 19, 30)) == "Wed Nov 11, 1:30 PM CT"
    assert when_ct(datetime(2026, 7, 1, 17, 0)) == "Wed Jul 1, 12:00 PM CT"  # CDT, UTC-5
    assert when_ct(datetime(2026, 11, 12, 5, 0)) == "Wed Nov 11, 11:00 PM CT"  # date rolls back


def test_fallback_rule_matches_the_tz_database(monkeypatch):
    samples = [datetime(2026, 1, 15, 12), datetime(2026, 3, 8, 7, 59), datetime(2026, 3, 8, 8, 0)]
    samples += [datetime(2026, 11, 1, 6, 59), datetime(2026, 11, 1, 7, 0), datetime(2026, 12, 15, 20)]
    expected = [to_clinic(s).utcoffset() for s in samples]
    monkeypatch.setattr(timefmt, "_TZ", None)
    assert [to_clinic(s).utcoffset() for s in samples] == expected


def _booking_reply(patient):
    reply = chat(patient, "book mri")
    if not any(SLOT_ID_CMD.fullmatch(b.get("send", "")) for b in reply.get("buttons") or []):
        reply = chat(patient, "Book MRI knee without contrast")
    return reply


def test_open_times_are_shown_in_ct(env, patient):
    reply = _booking_reply(patient)
    labels = [b["label"] for b in reply["buttons"] if SLOT_ID_CMD.fullmatch(b.get("send", ""))]
    assert labels
    for label in labels:
        assert CT_TIME.search(label), label
        assert not re.search(r"\b\d{2}:\d{2}\b(?! (AM|PM))", label)


def test_picked_time_is_shown_in_plain_words_after_reload(env, patient):
    reply = _booking_reply(patient)
    pick = next(b for b in reply["buttons"] if SLOT_ID_CMD.fullmatch(b.get("send", "")))
    held = chat(patient, pick["send"])
    assert "holding" in held["text"].lower()
    assert CT_TIME.search(held["text"])
    assert not SLOT_ID_CMD.search(held["text"])
    rows = patient.get("/api/chat/transcript").json()["messages"]
    shown = [r["body"] for r in rows if r["speaker"] == "patient"]
    assert pick["label"] in shown
    assert not any(SLOT_ID_CMD.search(b) for b in shown)


def test_unknown_hold_command_never_shows_the_number(env, patient, db_session):
    from app.conversation import patient_display

    assert patient_display(db_session, "hold 999999", {}) == "Picked a time"
    assert patient_display(db_session, "lab price", {}) == "lab price"
    assert patient_display(db_session, "book lab", {"book lab": "Book Lab panel"}) == "Book Lab panel"


def test_status_reply_uses_ct(env, patient):
    reply = _booking_reply(patient)
    pick = next(b for b in reply["buttons"] if SLOT_ID_CMD.fullmatch(b.get("send", "")))
    chat(patient, pick["send"])
    status = chat(patient, "status")
    assert CT_TIME.search(status["text"]), status["text"]


def test_old_saved_label_for_a_pick_is_rebuilt_in_ct(env, patient, db_session):
    from app.conversation import patient_display
    from app.models import Slot

    slot = db_session.query(Slot).filter(Slot.status == "OPEN").first()
    old = {f"hold {slot.id}": "Tue Nov 10, 16:00 - somewhere"}
    shown = patient_display(db_session, f"hold {slot.id}", old)
    assert shown == f"{when_ct(slot.starts_at)} - {slot.clinic.location}"
    assert CT_TIME.search(shown)
