"""Plain-language price explanations. Every dollar amount comes from the database."""

from __future__ import annotations

import re

from app.models import Service

_AMOUNT = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?")


# Demo checkout ceiling only. Not a medical limit, a clinical dose, or a promotional minimum.
UNIT_QUANTITY_MAX = 200

_SCIENTIFIC = re.compile(r"\d(?:\.\d+)?[eE][+-]?\d+")
_DECIMAL = re.compile(r"\d*\.\d+|\d+\.(?!\d)")
_GROUPED = re.compile(r"\d,\d")
_NEGATIVE = re.compile(r"(?:^|[^\d])-\s*\d+")
_RANGE = re.compile(r"\d\s*(?:-|/|–|—|~|to|or|through|and)\s*\d", re.I)
_UNIT_WORD = re.compile(r"units?(?:\b|\d)", re.I)


def money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def is_per_unit(service: Service) -> bool:
    return getattr(service, "price_unit", None) == "unit"


def quantity_message(kind: str) -> str:
    if kind == "whole":
        return (
            "Enter one plain whole number of units, the quantity your clinician or staff authorized. "
            "I will not round, convert scientific notation, or choose a quantity."
        )
    if kind == "range":
        return (
            f"Enter one whole number of units from 1 to {UNIT_QUANTITY_MAX}. "
            f"{UNIT_QUANTITY_MAX} is a demo checkout ceiling, not a medical limit. "
            "I will not choose a quantity."
        )
    if kind == "ambiguous":
        return (
            "That has more than one quantity. Send the single whole number of units "
            "your clinician or staff authorized. I will not pick between them."
        )
    return "Enter the whole number of units your clinician or staff authorized. I will not choose a quantity for you."


def quantity_followup(text: str) -> bool:
    """True when a message is trying to give a unit quantity, including an invalid one."""
    if _UNIT_WORD.search(text) or _RANGE.search(text) or _SCIENTIFIC.search(text):
        return True
    if _DECIMAL.search(text) or _GROUPED.search(text) or _NEGATIVE.search(text):
        return True
    return bool(re.fullmatch(r"\s*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*(?:units?)?\s*", text, re.I))


def parse_unit_quantity(text: str, *, allow_bare: bool) -> tuple[int | None, str | None]:
    """Return (quantity, error). (None, None) means the message did not attempt a quantity.

    A quantity is accepted only when the message states one plain positive integer.
    Ranges, alternatives, slash pairs, and scientific or embedded forms are refused.
    The parser never keeps the integer nearest the word "units".
    """
    unitish = bool(_UNIT_WORD.search(text))
    bare = re.fullmatch(r"\s*\d+\s*(?:units?)?\s*", text, re.I)
    integers = [int(n) for n in re.findall(r"\d+", text)]
    attempted = unitish or bool(bare) or _RANGE.search(text) or _SCIENTIFIC.search(text)
    if allow_bare:
        attempted = attempted or _DECIMAL.search(text) or _GROUPED.search(text) or _NEGATIVE.search(text)
        attempted = attempted or len(integers) > 1
    elif not unitish:
        attempted = False
    if not attempted:
        return None, None
    if _SCIENTIFIC.search(text) or _DECIMAL.search(text) or _GROUPED.search(text):
        return None, quantity_message("whole")
    if _NEGATIVE.search(text):
        return None, quantity_message("range")
    if _RANGE.search(text) or len(set(integers)) != 1:
        return None, quantity_message("ambiguous")
    if not unitish and not bare:
        return None, None
    qty = integers[0]
    if qty < 1 or qty > UNIT_QUANTITY_MAX:
        return None, quantity_message("range")
    return qty, None


def line_total_cents(unit_cents: int, quantity: int) -> int:
    if quantity < 1 or quantity > UNIT_QUANTITY_MAX:
        raise ValueError(quantity_message("range"))
    return unit_cents * quantity


def derived_amounts(unit_cents: int, quantity: int, caps: dict) -> set[int]:
    """Dollar amounts a per-unit explanation may show for one explicit quantity."""
    total = line_total_cents(unit_cents, quantity)
    vals = {unit_cents, total}
    for pct in range(caps["deposit_percent_min"], caps["deposit_percent_max"] + 1):
        vals.add(round(total * pct / 100))
    if total >= caps["plan_min_total_cents"]:
        for months in range(caps["plan_months_min"], caps["plan_months_max"] + 1):
            vals.update(plan_schedule(total, months))
    return vals


def deposit_cents(quote_cents: int, percent: int) -> int:
    return round(quote_cents * percent / 100)


def plan_schedule(total_cents: int, months: int) -> list[int]:
    base = total_cents // months
    first = total_cents - base * (months - 1)
    return [first] + [base] * (months - 1)


def explain(
    service: Service,
    quote_cents: int | None = None,
    deposit_percent: int = 25,
    plan_months: int = 3,
    plan_allowed: bool = True,
    quantity: int | None = None,
) -> str:
    if is_per_unit(service) and quantity is None and quote_cents is None:
        unit = money(service.price_cents)
        return "\n".join(
            [
                f"{service.name}: the cash price is {unit} per unit, not a flat treatment total.",
                "Enter the whole number of units your clinician or staff authorized. "
                "I will not choose a quantity for you.",
                f"Up to {UNIT_QUANTITY_MAX} units is only a demo checkout ceiling, not a medical limit.",
                f"What's included: {service.includes}.",
                f"Not included: {service.excludes}.",
                f"After you enter a quantity, you can pay that total, authorize a {deposit_percent}% deposit "
                "(collected at check-in), use a monthly plan when the total qualifies, or get an invoice by email.",
                "This is a good-faith estimate for the units you enter; if the clinician recommends something extra, "
                "you'll see the price before anything is added.",
            ]
        )
    if is_per_unit(service) and quantity is not None:
        computed = service.price_cents * quantity
        q = quote_cents if quote_cents is not None else computed
        if quote_cents is not None and quote_cents != computed:
            head = (
                f"{service.name}: {quantity} units at {money(service.price_cents)} each; "
                f"the current total is {money(q)}."
            )
        else:
            head = f"{service.name}: {quantity} units x {money(service.price_cents)} = {money(q)} total."
    else:
        q = quote_cents if quote_cents is not None else service.price_cents
        head = f"{service.name}: the cash price is {money(q)} total."
    lines = [
        head,
        f"What's included: {service.includes}.",
        f"Not included: {service.excludes}.",
        f"Ways to pay: pay {money(q)} now, or hold your spot with a {deposit_percent}% deposit of "
        f"{money(deposit_cents(q, deposit_percent))} (the rest is collected at check-in)",
    ]
    if plan_allowed:
        sched = plan_schedule(q, plan_months)
        lines[-1] += f", or a {plan_months}-month plan starting at {money(sched[0])}"
    lines[-1] += ", or an invoice by email."
    if is_per_unit(service):
        lines.append(
            "This total uses only the quantity you entered. It should be the one your clinician or staff "
            "authorized. This is a synthetic sandbox price, not a dose recommendation."
        )
    lines.append(
        "This is a good-faith estimate for this visit; if the clinician recommends something extra, "
        "you'll see the price before anything is added."
    )
    return "\n".join(lines)


def amounts_in(text: str) -> list[int]:
    out = []
    for whole, frac in _AMOUNT.findall(text):
        out.append(int(whole.replace(",", "")) * 100 + int(frac or 0))
    return out


def invalid_amounts(text: str, allowed_cents: set[int]) -> list[int]:
    """Amounts in the reply that are not backed by a database value."""
    return [a for a in amounts_in(text) if a not in allowed_cents]
