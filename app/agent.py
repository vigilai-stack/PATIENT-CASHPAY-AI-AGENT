"""Patient chat agent.

LLM_MODE=mock (default): a deterministic intent engine, so every run gets identical behaviour
with no keys. LLM_MODE=openai asks a hosted model for one allowlisted structured intent, then
runs that intent through the same Clinic methods. The model never supplies a price, a dose, or
a payment authorization. If the model is unavailable, the reply says so and does not pretend the
offline router was a model answer.

Whatever produces the intent, the agent can only act through `Clinic` methods, and those ask the
guard first. Patient text is treated as data: it is never used as an instruction, and amounts
or IDs never come from it except a refund amount, which the guard then caps.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select

from app import audit
from app.config import Settings, load_settings
from app.conversation import (
    CARE_PREFIX,
    CLINICAL_TEXT,
    PERSON_TEXT,
    clinical_concern,
    distress_concern,
    is_pronoun_followup,
    last_named_service,
    named_service,
    prompt_budget,
    service_matches,
    strip_trim_notes,
    wants_person,
)
from app.guard import detect_injection
from app.llm import UNAVAILABLE_TEXT, LLMUnavailable, OpenAIInterpreter, patient_note
from app.models import Appointment, ApprovalRequest, Patient, Payment, Service
from app.pricing import (
    UNIT_QUANTITY_MAX,
    amounts_in,
    derived_amounts,
    explain,
    invalid_amounts,
    is_per_unit,
    line_total_cents,
    money,
    parse_unit_quantity,
    plan_schedule,
    quantity_followup,
    quantity_message,
)
from app.services import Clinic, FlowError
from app.timefmt import when_ct

_log = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str
    buttons: list[dict] = field(default_factory=list)  # {"label", "send"} or {"label", "href"}
    flags: list[str] = field(default_factory=list)
    pay_url: str | None = None
    unit_code: str | None = None
    unit_quantity: int | None = None
    clear_unit_quote: bool = True
    allowed_extra: set[int] = field(default_factory=set)
    assistant: str = "deterministic"  # deterministic | openai | unavailable
    handoff: str | None = None  # clinical | requested | None
    card: dict | None = None  # price card or approval (human handoff) card, built from server values
    chips: list[dict] = field(default_factory=list)  # compact status chips, set only when true for this reply
    amount_verified: bool = False  # a payment was created after the guard checked its amount

    def as_dict(self):
        return {
            "text": self.text,
            "buttons": self.buttons,
            "flags": self.flags,
            "pay_url": self.pay_url,
            "assistant": self.assistant,
            "handoff": self.handoff,
            "card": self.card,
            "chips": self.chips,
        }


class Agent:
    def __init__(self, clinic: Clinic, settings: Settings | None = None, interpreter: OpenAIInterpreter | None = None):
        self.c = clinic
        self.settings = settings or load_settings()
        self.interpreter = interpreter

    def _service_for(self, text: str):
        words = set(re.findall(r"[a-z\-]+", text.lower()))
        best, score = None, 0
        for s in self.c.services():
            k = len(words & set(s.keywords.split()))
            if k > score:
                best, score = s, k
        return best

    def _allowed_amounts(self, patient: Patient) -> set[int]:
        vals = set()
        caps = self.c.caps
        for s in self.c.services():
            vals.add(s.price_cents)
            if is_per_unit(s):
                continue
            for pct in range(caps["deposit_percent_min"], caps["deposit_percent_max"] + 1):
                vals.add(round(s.price_cents * pct / 100))
        appts = list(self.c.db.execute(select(Appointment).where(Appointment.patient_id == patient.id)).scalars())
        for a in appts:
            for m in range(caps["plan_months_min"], caps["plan_months_max"] + 1):
                vals.update(plan_schedule(a.quote_cents, m))
            for p in self.c.payments_for(a):
                vals.update({p.amount_cents, p.amount_cents - p.refunded_cents})
        for s in self.c.services():
            if is_per_unit(s):
                continue
            for m in range(caps["plan_months_min"], caps["plan_months_max"] + 1):
                vals.update(plan_schedule(s.price_cents, m))
        vals.add(caps["plan_min_total_cents"])
        q = select(ApprovalRequest.amount_cents).join(Appointment).where(Appointment.patient_id == patient.id)
        vals.update(self.c.db.execute(q).scalars())
        q2 = select(Appointment.quote_cents).where(Appointment.patient_id == patient.id)
        vals.update(self.c.db.execute(q2).scalars())
        return vals

    def handle(
        self,
        patient: Patient,
        raw: str,
        unit_quote: dict | None = None,
        history: list[dict] | None = None,
        window=None,
    ) -> Reply:
        text = raw if isinstance(raw, str) else ""
        if not text.strip():
            return Reply('Please type a question, for example "How much is a knee MRI?"')
        current_omitted = len(text) > prompt_budget()
        flags = detect_injection(text)
        quote = unit_quote or {}
        history = list(history or [])
        services = self.c.services()
        matches = service_matches(text, services)
        named = matches[0] if len(matches) == 1 else None
        carried = None
        if named is None and is_pronoun_followup(text) and not _money_ask(text):
            carried = last_named_service(history, services)
        clinical = clinical_concern(text)
        try:
            if clinical and not flags:
                reply = Reply(CLINICAL_TEXT, assistant="deterministic", handoff="clinical")
            elif wants_person(text) and not flags:
                reply = Reply(PERSON_TEXT, assistant="deterministic", handoff="requested")
            elif flags or self._offline_route(text, quote) or self.settings.llm_mode != "openai":
                reply = self._route(patient, text, flags, quote)
                reply = self._apply_carried(patient, reply, text, carried, quote)
            else:
                reply = self._model_turn(
                    patient, text, quote, window, named, matches, carried, current_omitted, history
                )
        except FlowError as e:
            reply = Reply(e.message, buttons=[{"label": "Show open times", "send": "book"}])
        reply.flags = flags
        if distress_concern(text) and not flags and not clinical and not reply.text.startswith(CARE_PREFIX):
            reply.text = CARE_PREFIX + "\n" + reply.text
        # Excerpt trimming stays internal: it is logged, never shown to the patient.
        if current_omitted or (window is not None and getattr(window, "omitted_count", 0)):
            _log.info(
                "model excerpt trimmed: omitted=%s omitted_human=%s current_omitted=%s",
                getattr(window, "omitted_count", 0),
                getattr(window, "omitted_human", 0),
                bool(current_omitted),
            )
        reply.text = strip_trim_notes(reply.text)
        shown = reply.text + " " + " ".join(b.get("label", "") for b in reply.buttons) + " " + _card_text(reply.card)
        bad = invalid_amounts(shown, self._allowed_amounts(patient) | reply.allowed_extra)
        if bad:  # the price validator: no amount reaches the patient unless it is in the database
            audit.record(self.c.db, "agent", "reply_blocked", "DENIED", "amount_not_from_db", {"amounts": bad})
            self.c.db.commit()
            reply = Reply(
                "Let me get you the exact price from the clinic price list. Which service do you need?",
                assistant=reply.assistant,
            )
        priced = reply.text + " " + " ".join(b.get("label", "") for b in reply.buttons)
        if reply.card and reply.card.get("type") == "price":
            priced += " " + _card_text(reply.card)
        reply.chips = _status_chips(reply, bool(amounts_in(priced)))
        return reply

    # ------------------------------------------------------------------
    def _route(self, patient: Patient, text: str, flags: list[str], unit_quote: dict) -> Reply:
        t = text.lower()
        appt = self.c.latest_appointment(patient)
        held = appt if appt and appt.status == "HELD" else None

        if flags:
            audit.record(
                self.c.db,
                "agent",
                "injection_detected",
                "FLAGGED",
                "injection_detector",
                {"reasons": flags[:3]},
            )
            self.c.db.commit()
            msg = (
                "I can't change clinic rules or move money on my own. Refunds and discounts are always "
                "reviewed by clinic staff."
            )
            if "refund" in t:
                last = self.c.latest_appointment(patient, ("CONFIRMED",))
                if last:
                    try:
                        ar = self.c.request_refund(last, None, f"[flagged] {text}", "agent", injection_flag=True)
                        msg += (
                            " I've passed your refund request to the billing team; they'll reply within one "
                            "business day."
                        )
                        return Reply(msg, card=_approval_card(ar))
                    except FlowError as e:
                        msg += " " + e.message
            return Reply(msg)

        m = re.match(r"hold (\d+)(?:\s+(\d+))?$", t)
        if m:
            qty = int(m.group(2)) if m.group(2) else None
            if qty is not None and not 1 <= qty <= UNIT_QUANTITY_MAX:
                return Reply(quantity_message("range"), unit_code=_pending_code(unit_quote), clear_unit_quote=False)
            a = self.c.hold_slot(patient, int(m.group(1)), quantity=qty)
            extra: set[int] = set()
            shown_qty = None
            if is_per_unit(a.service):
                shown_qty = a.quantity
                extra = derived_amounts(a.service.price_cents, a.quantity, self.c.caps)
            return Reply(
                f"Done. I'm holding {a.service.name} on {_when(a.slot.starts_at)} at {a.slot.clinic.location} "
                f"for {self.c.caps['hold_minutes']} minutes.\n"
                + explain(
                    a.service,
                    a.quote_cents,
                    plan_allowed=self._plan_ok(a),
                    quantity=shown_qty,
                ),
                buttons=self._pay_buttons(a),
                card=_price_card(a.service, a.quote_cents, self._plan_ok(a), shown_qty, held=True),
                unit_code=a.service.code if shown_qty else None,
                unit_quantity=shown_qty,
                clear_unit_quote=shown_qty is None,
                allowed_extra=extra,
            )

        if t.startswith("pay ") or t in ("pay", "deposit", "plan", "invoice"):
            if not held:
                return Reply(
                    "First pick a time so I can hold it for you.",
                    [{"label": "Show open times", "send": "book"}],
                )
            funding = "venmo" if "venmo" in t else "paypal"
            if "deposit" in t:
                p = self.c.start_payment(held, "DEPOSIT", funding=funding)
                return self._pay_reply(
                    p,
                    f"Your deposit is {money(p.amount_cents)}. We only authorize it now and collect it at check-in.",
                )
            if "plan" in t:
                months = int(re.search(r"(\d+)", t).group(1)) if re.search(r"(\d+)", t) else 3
                p = self.c.start_payment(held, "PLAN", months=months)
                sched = plan_schedule(p.amount_cents, months)
                return self._pay_reply(
                    p,
                    f"{months} monthly payments: first {money(sched[0])}, then "
                    f"{money(sched[-1])} a month, {money(p.amount_cents)} in total.",
                )
            if "invoice" in t:
                p = self.c.start_payment(held, "INVOICE")
                return Reply(
                    f"I've emailed an invoice for {money(p.amount_cents)} to {patient.email}. Your visit is booked.",
                    amount_verified=True,
                )
            p = self.c.start_payment(held, "FULL", funding=funding)
            return self._pay_reply(p, f"Your total is {money(p.amount_cents)}.")

        if any(w in t for w in ("more time", "extend", "longer")):
            if not held:
                return Reply("There's no hold to extend right now.")
            a = self.c.extend_hold(held)
            return Reply(
                f"I've added {self.c.caps['small_actions']['extend_hold_minutes']} more minutes to your "
                "hold (one extension per booking).",
                self._pay_buttons(a),
            )
        if "resend" in t or "link" in t:
            if not held:
                return Reply("There's no open payment link.")
            p = self.c.resend_link(held)
            if not p:
                return Reply("You haven't picked a payment option yet.", self._pay_buttons(held))
            return self._pay_reply(p, "Here's your payment link again.")
        if "switch" in t:
            if not held:
                return Reply("There's no open payment to switch.")
            funding = "venmo" if "venmo" in t else "paypal"
            p = self.c.switch_method(held, funding)
            return self._pay_reply(p, f"Switched to {funding.title()}.")

        if "refund" in t:
            last = self.c.latest_appointment(patient, ("CONFIRMED",))
            if not last:
                return Reply("I can't find a paid visit to refund.")
            amt = _amount(t)
            ar = self.c.request_refund(last, amt, text, "agent")
            who = "the billing lead" if ar.required_role == "billing_lead" else "clinic staff"
            return Reply(
                f"I've sent a refund request for {money(ar.amount_cents)} to {who}. I can't approve "
                "refunds myself; you'll hear back within one business day.",
                card=_approval_card(ar),
            )
        if any(w in t for w in ("discount", "afford", "cheaper", "expensive")):
            if not appt:
                return Reply("Pick a service first and I'll show you every way to pay.")
            pct = int(re.search(r"(\d+)\s*%", t).group(1)) if re.search(r"(\d+)\s*%", t) else 10
            ar = self.c.request_discount(appt, pct, text, "agent")
            msg = (
                f"I've asked staff to review a {ar.percent}% discount. Meanwhile, a monthly plan spreads the "
                f"{money(appt.quote_cents)} over a few months with no interest."
            )
            return Reply(msg, self._pay_buttons(appt) if appt.status == "HELD" else [], card=_approval_card(ar))

        if "status" in t or "my booking" in t:
            if not appt:
                return Reply("You don't have a booking yet.", [{"label": "Show services", "send": "services"}])
            pays = self.c.payments_for(appt)
            ps = ", ".join(f"{p.kind.lower()} {p.status.lower().replace('_', ' ')}" for p in pays) or "no payment yet"
            return Reply(f"{appt.service.name} on {_when(appt.slot.starts_at)}: booking {appt.status.lower()}; {ps}.")

        unit_reply = self._maybe_unit(patient, t, unit_quote, held)
        if unit_reply is not None:
            return unit_reply

        svc = self._service_for(t)
        if svc and is_per_unit(svc):
            return self._unit_reply(patient, t, svc, unit_quote, held)
        if any(w in t for w in ("book", "slot", "available", "appointment", "time", "schedule")) or (
            svc and "book" in t
        ):
            svc = svc or (appt.service if appt else None)
            if not svc:
                return Reply("Which service would you like to book?", self._service_buttons())
            slots = self.c.open_slots(svc)
            if not slots:
                return Reply(f"No open times for {svc.name} in the next few days. Try another service.")
            return Reply(
                f"Open times for {svc.name} ({money(svc.price_cents)}):",
                [{"label": f"{_when(s.starts_at)} - {s.clinic.location}", "send": f"hold {s.id}"} for s in slots],
            )
        if svc:
            plan_ok = svc.price_cents >= self.c.caps["plan_min_total_cents"]
            return Reply(
                explain(svc, plan_allowed=plan_ok),
                [{"label": f"Book {svc.name}", "send": f"book {svc.code.lower()} {svc.keywords.split()[0]}"}],
                card=_price_card(svc, svc.price_cents, plan_ok),
            )
        if any(w in t for w in ("price", "cost", "how much", "services", "menu", "list")):
            return Reply("Here are our cash prices. Tap one for details.", self._service_buttons())
        return Reply(
            "Hi! I can tell you what a visit costs, book a time, and take payment by PayPal or Venmo. "
            "What do you need today?",
            self._service_buttons(),
        )

    # ------------------------------------------------------------------
    def _offline_route(self, text: str, unit_quote: dict) -> bool:
        """Button commands and unit quantities stay on the parser. The model does not choose a dose."""
        t = text.lower().strip()
        if quantity_followup(t):
            return True
        if unit_quote.get("code") and re.fullmatch(r"\d{1,3}", t):
            return True
        if re.fullmatch(r"hold \d+(?: \d+)?", t):
            return True
        if t in {"pay", "deposit", "plan", "invoice", "status", "services", "book"}:
            return True
        if re.fullmatch(r"pay(?: full| deposit| invoice)?(?: venmo)?", t):
            return True
        if re.fullmatch(r"pay plan \d{1,2}", t):
            return True
        if t in {"more time", "extend", "longer", "resend", "resend link", "switch", "switch venmo", "switch paypal"}:
            return True
        if re.fullmatch(r"[a-z\-]+ price", t):
            return True
        return bool(t.startswith("book ") and len(t) <= 80)

    def _model_turn(
        self,
        patient,
        text,
        unit_quote,
        window,
        named,
        matches,
        carried,
        current_omitted: bool,
        history: list[dict],
    ) -> Reply:
        if self.interpreter is None:
            return self._unavailable("not_configured")
        catalog = [
            {
                "code": s.code,
                "name": s.name,
                "price_unit": getattr(s, "price_unit", "visit"),
                "includes": s.includes,
                "excludes": s.excludes,
                "keywords": s.keywords,
            }
            for s in self.c.services()
        ]
        lines = list(getattr(window, "lines", []) or [])
        omitted = int(getattr(window, "omitted_count", 0) or 0)
        try:
            turn = self.interpreter.interpret(
                "" if current_omitted else text,
                catalog,
                lines,
                omitted,
                current_omitted,
            )
        except LLMUnavailable as exc:
            return self._unavailable(exc.reason)
        resolved = self._resolve_model_turn(text, history, turn, self.c.services(), named, matches, carried)
        audit.record(
            self.c.db,
            "agent",
            "llm_interpreted",
            "OK",
            resolved["selected_intent"],
            {
                "service_code": resolved["selected_service"],
                "proposed_intent": resolved["proposed_intent"],
                "proposed_service": resolved["proposed_service"],
                "proposed_focus": resolved["proposed_focus"],
                "selected_intent": resolved["selected_intent"],
                "selected_service": resolved["selected_service"],
                "selected_focus": resolved["selected_focus"],
                "service_source": resolved["service_source"],
                "proposed_concern": resolved["proposed_concern"],
                "selected_concern": resolved["selected_concern"],
            },
        )
        self.c.db.commit()
        turn.service_code = resolved["selected_service"] or None
        turn.intent = resolved["selected_intent"]
        turn.focus = resolved["selected_focus"]
        if resolved["drop_note"] or resolved["service_source"] in ("rejected_ungrounded", "unknown_code"):
            turn.explanation = ""
        if resolved["service_source"] == "unknown_code":
            return Reply(
                "I can only quote services on the clinic price list.",
                self._service_buttons(),
                assistant="openai",
            )
        if resolved["service_source"] == "ambiguous":
            return Reply(resolved["ambiguous_text"], self._service_buttons(), assistant="openai")
        return self._apply_turn(patient, text, turn, unit_quote, history)

    def _resolve_model_turn(self, text, history, turn, services, named, matches, carried) -> dict:
        """Keep a model service only when the saved chat or the current words support it."""
        proposed_service = turn.service_code or ""
        proposed_known = self._by_code(proposed_service) if proposed_service else None
        saved_codes = set()
        staff_service = None
        for msg in history:
            hit = named_service(str(msg.get("body") or ""), services)
            if hit is not None:
                saved_codes.add(hit.code)
                if msg.get("speaker") == "staff":
                    staff_service = hit
        saved_service = staff_service or last_named_service(history, services) or carried
        refers_back = is_pronoun_followup(text) or turn.focus in ("includes", "excludes")
        selected = None
        source = "none"
        ambiguous_text = ""
        if len(matches) > 1:
            source = "ambiguous"
            names = "; ".join(item.name for item in matches)
            ambiguous_text = (
                "Those words match more than one service on the clinic price list: "
                f"{names}. I will not guess which one, and I will not give medical advice."
            )
        elif named is not None:
            selected = named
            source = "current_text"
        elif proposed_known is not None and proposed_known.code in saved_codes:
            selected = proposed_known
            source = "model_grounded"
        elif refers_back and saved_service is not None:
            selected = saved_service
            source = "saved_patient_line"
        elif proposed_known is not None:
            source = "rejected_ungrounded"
        elif proposed_service:
            source = "unknown_code"
        selected_focus = turn.focus
        selected_intent = turn.intent
        drop_note = False
        model_admin = turn.focus in ("price", "includes", "excludes") and turn.intent in ("price", "book")
        if selected is not None and source == "current_text" and not model_admin:
            selected_focus = "excludes" if turn.focus == "excludes" else "includes"
            selected_intent = "price"
            drop_note = True
        elif (
            selected is not None
            and selected_focus == "price"
            and selected_intent in ("clarify", "refuse", "list_services")
        ):
            selected_intent = "price"
            drop_note = True
        if source == "ambiguous":
            selected_intent = "clarify"
            selected_focus = "other"
            drop_note = True
        selected_concern = "none"
        if clinical_concern(text):
            selected_concern = "clinical"
        elif distress_concern(text):
            selected_concern = "distress"
        return {
            "proposed_intent": turn.intent,
            "proposed_service": proposed_service,
            "proposed_focus": turn.focus,
            "proposed_concern": turn.concern,
            "selected_intent": selected_intent,
            "selected_service": selected.code if selected is not None else "",
            "selected_focus": selected_focus,
            "selected_concern": selected_concern,
            "service_source": source,
            "drop_note": drop_note,
            "ambiguous_text": ambiguous_text,
        }

    def _catalog_fact(self, svc: Service, focus: str, history: list[dict]) -> Reply:
        label = "included" if focus == "includes" else "not included"
        fact = svc.includes if focus == "includes" else svc.excludes
        lines = [
            f"From the clinic price list for {svc.name}, what is {label}: {fact}.",
            f"The posted cash price is {money(svc.price_cents)}. I am not adding a fee that is not on that list.",
        ]
        staff = _latest_staff(history)
        if staff is not None:
            lines.append(f"{staff['name']} wrote in this chat: {staff['body']}")
        return Reply(
            "\n".join(lines),
            [{"label": f"Book {svc.name}", "send": f"book {svc.code.lower()} {svc.keywords.split()[0]}"}],
        )

    def _staff_or_unknown(self, history: list[dict]) -> str:
        staff = _latest_staff(history)
        if staff is None:
            return (
                "That detail is not in the saved chat or the clinic price list. "
                "I will not guess. Clinic staff can read the transcript in this demo."
            )
        return (
            f"{staff['name']} wrote in this chat: {staff['body']}\nI am quoting that saved note and not adding to it."
        )

    def _apply_carried(self, patient: Patient, reply: Reply, text: str, carried, unit_quote: dict) -> Reply:
        if carried is None or not _is_unresolved(reply.text):
            return reply
        appt = self.c.latest_appointment(patient)
        held = appt if appt and appt.status == "HELD" else None
        if is_per_unit(carried):
            unit = self._unit_reply(patient, text.lower(), carried, unit_quote, held)
            unit.assistant = reply.assistant
            return unit
        if any(w in text.lower() for w in ("book", "slot", "available", "appointment", "time", "schedule")):
            booked = self._book_reply(carried)
            booked.assistant = reply.assistant
            return booked
        plan_ok = carried.price_cents >= self.c.caps["plan_min_total_cents"]
        priced = Reply(
            explain(carried, plan_allowed=plan_ok),
            [{"label": f"Book {carried.name}", "send": f"book {carried.code.lower()} {carried.keywords.split()[0]}"}],
            assistant=reply.assistant,
            card=_price_card(carried, carried.price_cents, plan_ok),
        )
        return priced

    def _unavailable(self, reason: str) -> Reply:
        audit.record(self.c.db, "agent", "llm_unavailable", "DENIED", reason, {})
        self.c.db.commit()
        return Reply(UNAVAILABLE_TEXT, self._service_buttons(), assistant="unavailable")

    def _apply_turn(
        self, patient: Patient, text: str, turn, unit_quote: dict, history: list[dict] | None = None
    ) -> Reply:
        """Execute a validated intent. Prices, quantities, and money amounts come from the server."""
        t = text.lower()
        note = patient_note(turn.explanation)
        svc = self._by_code(turn.service_code)
        if turn.service_code and svc is None:
            return Reply(
                "I can only quote services on the clinic price list.",
                self._service_buttons(),
                assistant="openai",
            )
        appt = self.c.latest_appointment(patient)
        held = appt if appt and appt.status == "HELD" else None
        if turn.focus in ("includes", "excludes") and svc is not None:
            return self._prefix_note(self._catalog_fact(svc, turn.focus, history or []), note)
        if turn.focus == "other":
            return Reply(self._staff_or_unknown(history or []), assistant="openai")
        if turn.intent in ("clarify", "refuse"):
            return Reply(note or _FALLBACK, self._service_buttons(), assistant="openai")
        if turn.intent == "list_services":
            return Reply(
                note or "Here are our cash prices. Tap one for details.",
                self._service_buttons(),
                assistant="openai",
            )
        if turn.intent == "status":
            reply = self._status_reply(appt)
            reply.assistant = "openai"
            return reply
        if turn.intent in ("price", "book"):
            if svc is None:
                return Reply(note or "Which service do you mean?", self._service_buttons(), assistant="openai")
            if is_per_unit(svc):
                reply = self._unit_reply(patient, t, svc, unit_quote, held)
            elif turn.intent == "book":
                reply = self._book_reply(svc)
            else:
                plan_ok = svc.price_cents >= self.c.caps["plan_min_total_cents"]
                reply = Reply(
                    explain(svc, plan_allowed=plan_ok),
                    [{"label": f"Book {svc.name}", "send": f"book {svc.code.lower()} {svc.keywords.split()[0]}"}],
                    card=_price_card(svc, svc.price_cents, plan_ok),
                )
            return self._prefix_note(reply, note)
        if turn.intent == "refund_request":
            reply = self._refund_reply(patient, t, text)
            reply.assistant = "openai"
            return reply
        if turn.intent == "discount_request":
            reply = self._discount_reply(t, text, appt)
            reply.assistant = "openai"
            return reply
        if turn.intent == "extend_hold":
            if not held:
                return Reply("There's no hold to extend right now.", assistant="openai")
            a = self.c.extend_hold(held)
            return Reply(
                f"I've added {self.c.caps['small_actions']['extend_hold_minutes']} more minutes to your "
                "hold (one extension per booking).",
                self._pay_buttons(a),
                assistant="openai",
            )
        if turn.intent == "resend_link":
            if not held:
                return Reply("There's no open payment link.", assistant="openai")
            p = self.c.resend_link(held)
            if not p:
                return Reply("You haven't picked a payment option yet.", self._pay_buttons(held), assistant="openai")
            reply = self._pay_reply(p, "Here's your payment link again.")
            reply.assistant = "openai"
            return reply
        if turn.intent == "switch_method":
            if not held:
                return Reply("There's no open payment to switch.", assistant="openai")
            reply = self._pay_reply(self.c.switch_method(held, _funding(t)), f"Switched to {_funding(t).title()}.")
            reply.assistant = "openai"
            return reply
        if turn.intent in ("pay_full", "pay_deposit", "pay_plan", "pay_invoice"):
            if not held:
                return Reply(
                    "First pick a time so I can hold it for you.",
                    [{"label": "Show open times", "send": "book"}],
                    assistant="openai",
                )
            return self._pay_intent(patient, held, turn.intent, t, turn.months)
        return Reply(_FALLBACK, self._service_buttons(), assistant="openai")

    def _prefix_note(self, reply: Reply, note: str | None) -> Reply:
        reply.assistant = "openai"
        if note:
            reply.text = f"{note}\n{reply.text}"
        return reply

    def _status_reply(self, appt) -> Reply:
        if not appt:
            return Reply("You don't have a booking yet.", [{"label": "Show services", "send": "services"}])
        pays = self.c.payments_for(appt)
        ps = ", ".join(f"{p.kind.lower()} {p.status.lower().replace('_', ' ')}" for p in pays) or "no payment yet"
        return Reply(f"{appt.service.name} on {_when(appt.slot.starts_at)}: booking {appt.status.lower()}; {ps}.")

    def _book_reply(self, svc: Service) -> Reply:
        slots = self.c.open_slots(svc)
        if not slots:
            return Reply(f"No open times for {svc.name} in the next few days. Try another service.")
        return Reply(
            f"Open times for {svc.name} ({money(svc.price_cents)}):",
            [{"label": f"{_when(s.starts_at)} - {s.clinic.location}", "send": f"hold {s.id}"} for s in slots],
        )

    def _refund_reply(self, patient: Patient, lowered: str, original: str) -> Reply:
        last = self.c.latest_appointment(patient, ("CONFIRMED",))
        if not last:
            return Reply("I can't find a paid visit to refund.")
        ar = self.c.request_refund(last, _amount(lowered), original, "agent")
        who = "the billing lead" if ar.required_role == "billing_lead" else "clinic staff"
        return Reply(
            f"I've sent a refund request for {money(ar.amount_cents)} to {who}. I can't approve "
            "refunds myself; you'll hear back within one business day.",
            card=_approval_card(ar),
        )

    def _discount_reply(self, lowered: str, original: str, appt) -> Reply:
        if not appt:
            return Reply("Pick a service first and I'll show you every way to pay.")
        match = re.search(r"(\d+)\s*%", lowered)
        percent = int(match.group(1)) if match else 10
        ar = self.c.request_discount(appt, percent, original, "agent")
        msg = (
            f"I've asked staff to review a {ar.percent}% discount. Meanwhile, a monthly plan spreads the "
            f"{money(appt.quote_cents)} over a few months with no interest."
        )
        return Reply(msg, self._pay_buttons(appt) if appt.status == "HELD" else [], card=_approval_card(ar))

    def _pay_intent(self, patient: Patient, held, intent: str, lowered: str, months_hint: int | None) -> Reply:
        funding = _funding(lowered)
        if intent == "pay_deposit":
            p = self.c.start_payment(held, "DEPOSIT", funding=funding)
            reply = self._pay_reply(
                p,
                f"Your deposit is {money(p.amount_cents)}. We only authorize it now and collect it at check-in.",
            )
        elif intent == "pay_plan":
            months = months_hint if months_hint and re.search(rf"\b{months_hint}\b", lowered) else 3
            p = self.c.start_payment(held, "PLAN", months=months)
            sched = plan_schedule(p.amount_cents, months)
            reply = self._pay_reply(
                p,
                f"{months} monthly payments: first {money(sched[0])}, then "
                f"{money(sched[-1])} a month, {money(p.amount_cents)} in total.",
            )
        elif intent == "pay_invoice":
            p = self.c.start_payment(held, "INVOICE")
            reply = Reply(
                f"I've emailed an invoice for {money(p.amount_cents)} to {patient.email}. Your visit is booked.",
                amount_verified=True,
            )
        else:
            p = self.c.start_payment(held, "FULL", funding=funding)
            reply = self._pay_reply(p, f"Your total is {money(p.amount_cents)}.")
        reply.assistant = "openai"
        return reply

    def _plan_ok(self, a) -> bool:
        return a.quote_cents >= self.c.caps["plan_min_total_cents"]

    def _by_code(self, code: str | None) -> Service | None:
        if not code:
            return None
        return self.c.db.execute(select(Service).where(Service.code == code)).scalar_one_or_none()

    def _maybe_unit(self, patient: Patient, text: str, unit_quote: dict, held) -> Reply | None:
        mentions = bool(re.search(r"\bbotox\b|\bmedspa\b", text))
        pending = unit_quote.get("code")
        follow = bool(pending) and quantity_followup(text)
        booking = any(w in text for w in ("book", "slot", "available", "appointment", "time", "schedule"))
        if not (mentions or follow or (pending and booking)):
            return None
        other = self._service_for(text)
        if other and not is_per_unit(other):
            return None
        svc = other if other and is_per_unit(other) else self._by_code(pending or "MEDSPA-BOTOX")
        if not svc or not is_per_unit(svc):
            return None
        return self._unit_reply(patient, text, svc, unit_quote, held)

    def _unit_reply(self, patient: Patient, text: str, svc: Service, unit_quote: dict, held) -> Reply:
        qty, err = parse_unit_quantity(text, allow_bare=True)
        if err:
            return Reply(err, unit_code=svc.code, clear_unit_quote=False)
        stored = unit_quote.get("quantity") if unit_quote.get("code") == svc.code else None
        if qty is None and isinstance(stored, int):
            qty = stored
        booking = any(w in text for w in ("book", "slot", "available", "appointment", "time", "schedule"))
        if qty is None:
            return Reply(explain(svc), unit_code=svc.code, clear_unit_quote=False)
        total = line_total_cents(svc.price_cents, qty)
        extra = derived_amounts(svc.price_cents, qty, self.c.caps)
        if held and held.service_id == svc.id and not booking:
            live = [p for p in self.c.payments_for(held) if p.status not in ("VOIDED", "DECLINED", "EXPIRED")]
            if live:
                extra = derived_amounts(svc.price_cents, held.quantity, self.c.caps)
                return Reply(
                    f"This hold is priced at {held.quantity} units, {money(held.quote_cents)} total. "
                    "Checkout uses that quantity.",
                    self._pay_buttons(held),
                    unit_code=svc.code,
                    unit_quantity=held.quantity,
                    clear_unit_quote=False,
                    allowed_extra=extra,
                )
            held.quantity = qty
            held.quote_cents = total
            self.c.db.commit()
            return Reply(
                explain(svc, total, quantity=qty, plan_allowed=total >= self.c.caps["plan_min_total_cents"]),
                self._pay_buttons(held),
                card=_price_card(svc, total, total >= self.c.caps["plan_min_total_cents"], qty, held=True),
                unit_code=svc.code,
                unit_quantity=qty,
                clear_unit_quote=False,
                allowed_extra=extra,
            )
        if booking:
            slots = self.c.open_slots(svc)
            if not slots:
                return Reply(
                    f"No open times for {svc.name} in the next few days. Try another service.",
                    unit_code=svc.code,
                    unit_quantity=qty,
                    clear_unit_quote=False,
                )
            return Reply(
                f"Open times for {svc.name}, {qty} units ({money(total)} total):",
                [{"label": f"{_when(s.starts_at)} - {s.clinic.location}", "send": f"hold {s.id} {qty}"} for s in slots],
                unit_code=svc.code,
                unit_quantity=qty,
                clear_unit_quote=False,
                allowed_extra=extra,
            )
        return Reply(
            explain(svc, total, quantity=qty, plan_allowed=total >= self.c.caps["plan_min_total_cents"]),
            [{"label": f"Book {qty} units", "send": f"book botox {qty} units"}],
            card=_price_card(svc, total, total >= self.c.caps["plan_min_total_cents"], qty),
            unit_code=svc.code,
            unit_quantity=qty,
            clear_unit_quote=False,
            allowed_extra=extra,
        )

    def _service_buttons(self):
        buttons = []
        for s in self.c.services():
            price = money(s.price_cents) + ("/unit" if is_per_unit(s) else "")
            buttons.append({"label": f"{s.name} - {price}", "send": f"{s.keywords.split()[0]} price"})
        return buttons

    def _pay_buttons(self, a):
        b = [
            {"label": f"Pay in full ({money(a.quote_cents)})", "send": "pay full"},
            {"label": "Pay a 25% deposit", "send": "pay deposit"},
        ]
        if self._plan_ok(a):
            b.append({"label": "Monthly plan (3 months)", "send": "pay plan 3"})
        b.append({"label": "Email me an invoice", "send": "pay invoice"})
        return b

    def _pay_reply(self, p: Payment, lead: str) -> Reply:
        return Reply(
            f"{lead} Tap the button to pay securely with PayPal or Venmo.",
            [{"label": "Open secure checkout", "href": f"/pay/{p.id}"}],
            pay_url=f"/pay/{p.id}",
            amount_verified=True,
        )


def _price_card(svc: Service, total_cents: int, plan_ok: bool, quantity: int | None = None, held: bool = False) -> dict:
    """Price card from server values only. Pay methods listed are the ones checkout really offers."""
    options = ["PayPal", "Venmo", "25% deposit"]
    if plan_ok:
        options.append("3-month plan")
    options.append("Invoice")
    return {
        "type": "price",
        "service": svc.name,
        "amount": money(total_cents),
        "detail": f"{quantity} units x {money(svc.price_cents)}" if quantity else "Cash price, total",
        "options": options,
        "held": held,
    }


def _approval_card(ar: ApprovalRequest) -> dict:
    """Action card for a refund or discount: it waits for a person, and no money moves."""
    role = "Billing lead" if ar.required_role == "billing_lead" else "Clinic staff"
    kind = "Refund" if ar.kind == "REFUND" else "Discount"
    return {
        "type": "approval",
        "kind": kind,
        "amount": money(ar.amount_cents),
        "needs": role,
        "status": "Human approval required",
        "moved": "$0 moved",
        "request_id": ar.id,
    }


def _card_text(card: dict | None) -> str:
    if not card:
        return ""
    return " ".join(str(card.get(k, "")) for k in ("amount", "detail"))


def _status_chips(reply: Reply, has_amount: bool) -> list[dict]:
    """Chips are claims about this reply, so each is added only when it is true for this reply."""
    chips = []
    if reply.flags:
        chips.append({"label": "Injection flagged", "tone": "bad"})
    if has_amount and reply.assistant != "unavailable":
        chips.append({"label": "Price from DB \u2713", "tone": "ok"})
    if reply.amount_verified:
        chips.append({"label": "Amount verified \u2713", "tone": "ok"})
    if reply.card and reply.card.get("type") == "approval":
        chips.append({"label": f"{reply.card['kind']} \u2192 human", "tone": "warn"})
    return chips


def _latest_staff(history: list[dict]) -> dict | None:
    found = None
    for msg in history:
        if msg.get("speaker") != "staff":
            continue
        body = str(msg.get("body") or "")
        if not body.strip():
            continue
        found = {"name": str(msg.get("staff_name") or "Clinic staff"), "body": body}
    return found


def _when(dt) -> str:
    return when_ct(dt)


def _pending_code(unit_quote: dict) -> str | None:
    code = unit_quote.get("code")
    return code if isinstance(code, str) else None


_FALLBACK = (
    "Hi! I can tell you what a visit costs, book a time, and take payment by PayPal or Venmo. What do you need today?"
)


def _funding(lowered: str) -> str:
    return "venmo" if "venmo" in lowered else "paypal"


def _money_ask(text: str) -> bool:
    lowered = text.lower()
    return "refund" in lowered or "discount" in lowered or "afford" in lowered


def _is_unresolved(text: str) -> bool:
    return (
        text.startswith("Hi!")
        or text.startswith("Here are our cash prices")
        or text.startswith("Which service")
        or text.startswith("Please type a question")
    )


def _amount(t: str) -> int | None:
    m = re.search(r"\$\s?(\d+(?:\.\d{2})?)", t)
    if not m:
        return None
    return round(float(m.group(1)) * 100)
