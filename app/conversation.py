"""Saved chat transcript and the bounded excerpt sent to the model.

The transcript keeps every accepted line, in order, with the server session's
speaker. The model sees only whole lines that fit a character budget. It does
not receive a generated summary, and a line is never cut in half to make it fit.
Patient text cannot set a staff role or approve a payment.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.models import Conversation, ConversationHandoff, ConversationMessage, Patient, Slot, StaffUser, utcnow
from app.timefmt import when_ct

CARE_PREFIX = (
    "I'm sorry this is weighing on you. A person at the clinic can talk with you, "
    "and I will not turn this into a sales pitch."
)
CLINICAL_TEXT = (
    "I can't interpret symptoms or recommend a dose or a diagnosis. "
    "I saved your exact words for clinic staff to review. "
    "I will not send them outside this demo, and I will not guess at medical advice."
)
PERSON_TEXT = (
    "I saved this conversation for clinic staff to review. "
    "A person can read the exact messages in order. "
    "I will not send them outside this demo, and I cannot move a payment from this chat."
)
GAP_TEXT = (
    "Some earlier messages were not sent to the language model. "
    "The full transcript is saved, and this reply uses that saved text rather than a summary."
)
ASSISTANT_GAP_TEXT = (
    "An earlier reply in this chat was left out of the model excerpt so the saved patient and staff "
    "lines could fit. The full transcript is still saved. I did not replace it with a summary."
)
CURRENT_GAP_TEXT = (
    "Your latest message is saved in full. It was not sent to the language model "
    "because it is longer than the excerpt limit. This reply uses the saved message, not a shortened copy."
)
TRIM_NOTES = (GAP_TEXT, ASSISTANT_GAP_TEXT, CURRENT_GAP_TEXT)


def strip_trim_notes(text: str) -> str:
    """Remove excerpt/trimming notes from patient-facing text (older saved replies may contain them)."""
    if not text:
        return text
    for note in TRIM_NOTES:
        text = text.replace("\n" + note, "").replace(note, "")
    return text.rstrip("\n")


# Hard accept limit. Over this, the route rejects the request and stores nothing.
# The model excerpt is a separate, smaller budget and never shortens an accepted line.
MAX_ACCEPTED_MESSAGE_CHARS = 8000
_ALLOWED_CONTROLS = frozenset("\n\t\r")
_DEFAULT_BUDGET = 900
_DISTRESS = re.compile(r"\b(scared|frightened|afraid|exhausted|anxious|terrified|crying|overwhelmed)\b", re.I)
_CLINICAL = re.compile(
    r"\b(diagnos\w*|cancer|tumor|tumour|dosage|\bdose\b|prescription|should i take|am i dying|chest pain|suicid)\b",
    re.I,
)
_PERSON = re.compile(r"\b(talk|speak|see|review)\b.{0,40}\b(person|human|someone|staff)\b", re.I)
_PRONOUN = re.compile(
    r"\b(how much is that|how much is this|how much is it|what about that|what about it|"
    r"that one|the same|does that|is that|book that)\b",
    re.I,
)
# A negation covers the following words until a clause boundary. This is not a list of sentences.
_NEGATION = re.compile(r"\b(?:not|no|without|never|ignore|skip)\b|n't\b|\brather than\b|\binstead of\b", re.I)
_NEGATION_END = re.compile(r"[.;!?,]|\b(?:but|however|and)\b", re.I)
_GENERIC_KEYS = {"scan", "imaging", "test", "blood", "video", "online", "visit"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


class MessageRejected(ValueError):
    """The message was not accepted, so nothing was stored."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def accepted_message(raw: str) -> str:
    """Return the caller's text unchanged, or refuse it before any save.

    Whitespace, line breaks, and Unicode stay as sent. This does not trim or slice.
    """
    if not isinstance(raw, str):
        raise MessageRejected("blank")
    if len(raw) > MAX_ACCEPTED_MESSAGE_CHARS:
        raise MessageRejected("too_long")
    if any(unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROLS for ch in raw):
        raise MessageRejected("control")
    if not raw.strip():
        raise MessageRejected("blank")
    return raw


def prompt_budget() -> int:
    raw = os.environ.get("CONVERSATION_PROMPT_CHARS", "")
    if raw.isdigit():
        return max(200, min(4000, int(raw)))
    return _DEFAULT_BUDGET


def distress_concern(text: str) -> bool:
    return bool(_DISTRESS.search(text or ""))


def clinical_concern(text: str) -> bool:
    return bool(_CLINICAL.search(text or ""))


def wants_person(text: str) -> bool:
    return bool(_PERSON.search(text or ""))


def is_pronoun_followup(text: str) -> bool:
    return bool(_PRONOUN.search(text or ""))


def named_service(text: str, services):
    """The one catalog service named outside a negation. None when none or several match."""
    matches = service_matches(text, services)
    if len(matches) == 1:
        return matches[0]
    return None


def service_matches(text: str, services) -> list:
    """Every catalog service with a non-negated name in this string. Order is not a guess."""
    low = (text or "").lower()
    blocked = _negation_ranges(low)
    found = []
    for service in services:
        if _service_named(low, service, blocked):
            found.append(service)
    return found


def _negation_ranges(text: str) -> list[tuple[int, int]]:
    ranges = []
    for match in _NEGATION.finditer(text):
        start = match.end()
        end_at = _NEGATION_END.search(text, start)
        ranges.append((start, end_at.start() if end_at else len(text)))
    return ranges


def last_named_service(history: list[dict], services):
    found = None
    for msg in history:
        if msg.get("speaker") != "patient":
            continue
        hit = named_service(str(msg.get("body") or ""), services)
        if hit is not None:
            found = hit
    return found


def _service_named(text: str, service, blocked: list[tuple[int, int]]) -> bool:
    """A name needs two catalog words, or one distinctive word. Generic and negated words do not count."""
    positions = []
    distinctive = 0
    for key in service.keywords.split():
        if len(key) < 3 or key in _GENERIC_KEYS:
            continue
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
            if any(start <= match.start() < end for start, end in blocked):
                continue
            positions.append(match.start())
            if len(key) >= 4:
                distinctive += 1
    return len(positions) >= 2 or distinctive >= 1


@dataclass
class PromptWindow:
    lines: list[dict]
    omitted_count: int
    prior_count: int
    anchor_included: bool
    omitted_human: int = 0

    def public(self) -> dict:
        return {
            "prior_messages": self.prior_count,
            "prompt_messages": len(self.lines),
            "omitted_count": self.omitted_count,
            "omitted_human": self.omitted_human,
            "anchor_included": self.anchor_included,
            "prompt_char_budget": prompt_budget(),
        }


def select_window(prior: list[dict], services) -> PromptWindow:
    """Patient and staff lines first. Assistant replies use only the room that remains."""
    budget = prompt_budget()
    anchor = None
    staff_anchor = None
    human: list[dict] = []
    assistant: list[dict] = []
    for msg in prior:
        if msg.get("speaker") == "assistant":
            assistant.append(msg)
            continue
        human.append(msg)
        if msg.get("speaker") == "patient" and named_service(str(msg.get("body") or ""), services) is not None:
            anchor = msg
        if msg.get("speaker") == "staff":
            staff_anchor = msg
    selected = _fit_lines(human, budget)
    anchor_included = _force_line(selected, anchor, budget)
    _force_line(selected, staff_anchor, budget)
    used = sum(len(str(item.get("body") or "")) for item in selected)
    for msg in _fit_lines(assistant, budget - used):
        selected.append(msg)
    selected.sort(key=lambda item: item["seq"])
    kept = {item["seq"] for item in selected}
    omitted_human = sum(1 for msg in human if msg["seq"] not in kept)
    return PromptWindow(
        lines=selected,
        omitted_count=len(prior) - len(kept),
        prior_count=len(prior),
        anchor_included=anchor_included,
        omitted_human=omitted_human,
    )


def _fit_lines(rows: list[dict], budget: int) -> list[dict]:
    """Newest whole lines that fit. A long line is skipped, not sliced, and does not block a shorter one."""
    selected: list[dict] = []
    used = 0
    if budget <= 0:
        return selected
    for msg in reversed(rows):
        cost = len(str(msg.get("body") or ""))
        if cost > budget or used + cost > budget:
            continue
        selected.append(msg)
        used += cost
    selected.reverse()
    return selected


def _force_line(selected: list[dict], anchor: dict | None, budget: int) -> bool:
    """Keep one whole line. Drop older selected lines until it fits. Never slice it."""
    if anchor is None:
        return True
    if anchor in selected:
        return True
    cost = len(str(anchor.get("body") or ""))
    if cost > budget:
        return False
    used = sum(len(str(item.get("body") or "")) for item in selected)
    while selected and used + cost > budget:
        used -= len(str(selected.pop(0).get("body") or ""))
    selected.append(anchor)
    selected.sort(key=lambda item: item["seq"])
    return True


def _lock(patient_id: int) -> threading.Lock:
    key = f"patient:{patient_id}"
    with _LOCK_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _messages(db: Session, conversation_id: int) -> list[ConversationMessage]:
    return list(
        db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.seq, ConversationMessage.id)
        ).scalars()
    )


def _as_dict(db: Session, row: ConversationMessage) -> dict:
    staff_name = None
    if row.staff_id:
        staff = db.get(StaffUser, row.staff_id)
        staff_name = staff.name if staff else None
    return {
        "id": row.id,
        "seq": row.seq,
        "speaker": row.speaker,
        "body": row.body,
        "status": row.status,
        "patient_id": row.patient_id,
        "staff_id": row.staff_id,
        "staff_name": staff_name,
        "client_key": row.client_key,
    }


def ensure_conversation(db: Session, patient: Patient) -> Conversation:
    row = (
        db.execute(select(Conversation).where(Conversation.patient_id == patient.id).order_by(Conversation.id.desc()))
        .scalars()
        .first()
    )
    if row:
        return row
    row = Conversation(patient_id=patient.id, status="open", created_at=utcnow(), updated_at=utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _next_seq(db: Session, conversation_id: int) -> int:
    current = db.execute(
        select(func.max(ConversationMessage.seq)).where(ConversationMessage.conversation_id == conversation_id)
    ).scalar()
    return int(current or 0) + 1


def _assistant_after(rows: list[ConversationMessage], patient_row: ConversationMessage) -> ConversationMessage | None:
    later = [row for row in rows if row.seq > patient_row.seq and row.speaker == "assistant"]
    return later[-1] if later else None


def _public(
    conv: Conversation,
    patient_row: ConversationMessage,
    assistant_row: ConversationMessage,
    window,
    duplicate: bool,
):
    payload = dict(assistant_row.reply_json or {})
    payload["text"] = assistant_row.body
    payload.setdefault("buttons", [])
    payload.setdefault("flags", [])
    payload.setdefault("pay_url", None)
    payload.setdefault("assistant", "unavailable" if assistant_row.status == "unavailable" else "deterministic")
    payload.setdefault("handoff", None)
    payload["conversation_id"] = conv.id
    payload["message_id"] = patient_row.id
    payload["client_message_id"] = patient_row.client_key
    payload["duplicate"] = duplicate
    payload["saved_message"] = patient_row.body
    context = window.public() if window is not None else {}
    context["current_included"] = len(patient_row.body) <= prompt_budget()
    context["max_accepted_chars"] = MAX_ACCEPTED_MESSAGE_CHARS
    context["truncates_accepted_text"] = False
    payload["context"] = context
    return payload


def _open_handoff(db: Session, conv: Conversation, patient: Patient, reason: str) -> None:
    existing = db.execute(
        select(ConversationHandoff).where(ConversationHandoff.conversation_id == conv.id)
    ).scalar_one_or_none()
    if existing:
        return
    db.add(
        ConversationHandoff(
            conversation_id=conv.id,
            patient_id=patient.id,
            reason=reason,
            status="open",
            created_at=utcnow(),
        )
    )
    conv.status = "handed_off"
    conv.updated_at = utcnow()
    audit.record(db, "agent", "conversation_handoff", "OK", reason, {"conversation_id": conv.id})


def exchange(db: Session, agent, patient: Patient, raw: str, client_key: str | None, unit_quote: dict | None) -> dict:
    from app.agent import Reply
    from app.llm import UNAVAILABLE_TEXT

    text = accepted_message(raw)
    with _lock(patient.id):
        conv = ensure_conversation(db, patient)
        rows = _messages(db, conv.id)
        if client_key:
            existing = next((row for row in rows if row.client_key == client_key and row.speaker == "patient"), None)
            if existing is not None:
                assistant = _assistant_after(rows, existing)
                prior = [_as_dict(db, row) for row in rows if row.seq < existing.seq]
                window = select_window(prior, agent.c.services())
                if assistant is None:
                    reply = _run_agent(agent, patient, existing.body, unit_quote, prior, window)
                    assistant = _store_assistant(db, conv, existing, reply)
                    if reply.handoff:
                        _open_handoff(db, conv, patient, reply.handoff)
                    db.commit()
                return _public(conv, existing, assistant, window, True)
        patient_row = ConversationMessage(
            conversation_id=conv.id,
            seq=_next_seq(db, conv.id),
            speaker="patient",
            patient_id=patient.id,
            staff_id=None,
            body=text,
            client_key=client_key,
            status="stored",
            created_at=utcnow(),
        )
        db.add(patient_row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            rows = _messages(db, conv.id)
            existing = next((row for row in rows if row.client_key == client_key and row.speaker == "patient"), None)
            if existing is None:
                raise
            assistant = _assistant_after(rows, existing)
            prior = [_as_dict(db, row) for row in rows if row.seq < existing.seq]
            window = select_window(prior, agent.c.services())
            if assistant is None:
                reply = _run_agent(agent, patient, existing.body, unit_quote, prior, window)
                assistant = _store_assistant(db, conv, existing, reply)
                db.commit()
            return _public(conv, existing, assistant, window, True)
        db.refresh(patient_row)
        prior = [_as_dict(db, row) for row in _messages(db, conv.id) if row.seq < patient_row.seq]
        window = select_window(prior, agent.c.services())
        try:
            reply = _run_agent(agent, patient, text, unit_quote, prior, window)
        except Exception:
            reply = Reply(UNAVAILABLE_TEXT, assistant="unavailable")
        assistant = _store_assistant(db, conv, patient_row, reply)
        if reply.handoff:
            _open_handoff(db, conv, patient, reply.handoff)
        db.commit()
        return _public(conv, patient_row, assistant, window, False)


def retry_saved(db: Session, agent, patient: Patient, client_key: str, unit_quote: dict | None) -> dict:
    """One more model attempt on text already stored. Does not insert another patient line."""
    from app.agent import Reply
    from app.llm import UNAVAILABLE_TEXT

    with _lock(patient.id):
        conv = ensure_conversation(db, patient)
        rows = _messages(db, conv.id)
        existing = next((row for row in rows if row.client_key == client_key and row.speaker == "patient"), None)
        if existing is None or existing.patient_id != patient.id:
            raise KeyError(client_key)
        assistant = _assistant_after(rows, existing)
        prior = [_as_dict(db, row) for row in rows if row.seq < existing.seq]
        window = select_window(prior, agent.c.services())
        if assistant is not None and assistant.status == "answered":
            return _public(conv, existing, assistant, window, True)
        try:
            reply = _run_agent(agent, patient, existing.body, unit_quote, prior, window)
        except Exception:
            reply = Reply(UNAVAILABLE_TEXT, assistant="unavailable")
        stored = _store_assistant(db, conv, existing, reply)
        if reply.handoff:
            _open_handoff(db, conv, patient, reply.handoff)
        db.commit()
        return _public(conv, existing, stored, window, False)


def _run_agent(agent, patient, text, unit_quote, prior, window):
    return agent.handle(patient, text, unit_quote=unit_quote, history=prior, window=window)


def _store_assistant(db: Session, conv: Conversation, patient_row: ConversationMessage, reply) -> ConversationMessage:
    status = "unavailable" if reply.assistant == "unavailable" else "answered"
    payload = reply.as_dict()
    payload["_quote"] = {
        "code": reply.unit_code,
        "quantity": reply.unit_quantity,
        "clear": reply.clear_unit_quote,
    }
    row = ConversationMessage(
        conversation_id=conv.id,
        seq=_next_seq(db, conv.id),
        speaker="assistant",
        patient_id=None,
        staff_id=None,
        body=reply.text,
        client_key=None,
        status=status,
        reply_json=payload,
        created_at=utcnow(),
    )
    patient_row.status = status
    conv.updated_at = utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def transcript(db: Session, patient: Patient) -> dict:
    conv = (
        db.execute(select(Conversation).where(Conversation.patient_id == patient.id).order_by(Conversation.id.desc()))
        .scalars()
        .first()
    )
    if conv is None:
        return {
            "conversation_id": None,
            "status": None,
            "messages": [],
            "limits": _limits(0),
        }
    rows = _messages(db, conv.id)
    lines = []
    labels: dict[str, str] = {}
    for row in rows:
        line = _patient_line(db, row, labels)
        if row.speaker == "assistant":
            labels = {
                str(b.get("send")): str(b.get("label"))
                for b in (line.get("buttons") or [])
                if isinstance(b, dict) and b.get("send") and b.get("label")
            }
        lines.append(line)
    return {
        "conversation_id": conv.id,
        "status": conv.status,
        "messages": lines,
        "limits": _limits(len(rows)),
    }


def _limits(count: int) -> dict:
    return {
        "stored_messages": count,
        "max_accepted_chars": MAX_ACCEPTED_MESSAGE_CHARS,
        "truncates_accepted_text": False,
        "prompt_char_budget": prompt_budget(),
        "model_context": "bounded",
        "full_transcript_stored": True,
    }


_HOLD_CMD = re.compile(r"hold (\d+)(?: \d+)?")


def _patient_line(db: Session, row: ConversationMessage, labels: dict[str, str] | None = None) -> dict:
    line = _public_line(db, row)
    if row.speaker == "assistant":
        line["body"] = strip_trim_notes(line["body"])
    elif row.speaker == "patient":
        line["body"] = patient_display(db, line["body"], labels or {})
    return line


def patient_display(db: Session, body: str, labels: dict[str, str]) -> str:
    """Show a tapped button the way the patient saw it, never an internal command or slot id."""
    key = (body or "").strip()
    m = _HOLD_CMD.fullmatch(key)
    if not m and key in labels:
        return labels[key]
    if m:  # always rebuilt from the slot, so older saved labels also read in clinic time
        slot = db.get(Slot, int(m.group(1)))
        if slot is None:
            return "Picked a time"
        place = slot.clinic.location if slot.clinic else ""
        return f"{when_ct(slot.starts_at)} - {place}".rstrip(" -")
    return body


def _public_line(db: Session, row: ConversationMessage) -> dict:
    staff_name = None
    if row.staff_id:
        staff = db.get(StaffUser, row.staff_id)
        staff_name = staff.name if staff else None
    line = {
        "id": row.id,
        "seq": row.seq,
        "speaker": row.speaker,
        "body": row.body,
        "status": row.status,
        "staff_name": staff_name,
    }
    if row.speaker == "assistant" and isinstance(row.reply_json, dict):
        # The saved card and chips let a reloaded chat look the same. They were built from server values.
        line["card"] = row.reply_json.get("card")
        line["chips"] = row.reply_json.get("chips") or []
        line["buttons"] = row.reply_json.get("buttons") or []
    return line


def staff_queue(db: Session) -> list[dict]:
    rows = db.execute(select(ConversationHandoff).order_by(ConversationHandoff.id)).scalars()
    out = []
    for row in rows:
        patient = db.get(Patient, row.patient_id)
        out.append(
            {
                "conversation_id": row.conversation_id,
                "patient": patient.name if patient else "",
                "reason": row.reason,
                "status": row.status,
            }
        )
    return out


def staff_transcript(db: Session, conversation_id: int) -> dict | None:
    handoff = db.execute(
        select(ConversationHandoff).where(ConversationHandoff.conversation_id == conversation_id)
    ).scalar_one_or_none()
    if handoff is None:
        return None
    conv = db.get(Conversation, conversation_id)
    patient = db.get(Patient, handoff.patient_id)
    if conv is None or patient is None:
        return None
    return {
        "conversation_id": conv.id,
        "patient": patient.name,
        "reason": handoff.reason,
        "status": handoff.status,
        "messages": [_public_line(db, row) for row in _messages(db, conv.id)],
    }


def staff_say(db: Session, clinic, staff: StaffUser, conversation_id: int, text: str) -> dict:
    from app.agent import Agent
    from app.pricing import invalid_amounts

    try:
        cleaned = accepted_message(text)
    except MessageRejected as exc:
        raise ValueError(exc.code) from exc
    handoff = db.execute(
        select(ConversationHandoff).where(ConversationHandoff.conversation_id == conversation_id)
    ).scalar_one_or_none()
    conv = db.get(Conversation, conversation_id) if handoff else None
    patient = db.get(Patient, handoff.patient_id) if handoff else None
    if conv is None or patient is None:
        raise KeyError(conversation_id)
    with _lock(patient.id):
        bad = invalid_amounts(cleaned, Agent(clinic)._allowed_amounts(patient))
        if bad:
            raise ValueError("amount")
        row = ConversationMessage(
            conversation_id=conv.id,
            seq=_next_seq(db, conv.id),
            speaker="staff",
            patient_id=None,
            staff_id=staff.id,
            body=cleaned,
            client_key=None,
            status="answered",
            created_at=utcnow(),
        )
        handoff.status = "reviewed"
        conv.updated_at = utcnow()
        db.add(row)
        audit.record(
            db,
            staff.email,
            "conversation_staff_reply",
            "OK",
            "staff_review",
            {"conversation_id": conv.id, "staff_id": staff.id},
        )
        db.commit()
        db.refresh(row)
        return _public_line(db, row)
