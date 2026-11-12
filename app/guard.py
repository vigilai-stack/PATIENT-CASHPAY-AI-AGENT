"""Spending firewall. Every agent action passes through here before anything touches PayPal.

The guard does not trust the model, the patient, or webhook text. Amounts come from server state,
actions come from an allowlist, and refunds/discounts only ever create a staff approval item.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from app.config import load_policy
from app.pricing import money

ROLE_RANK = {"agent": 0, "patient": 0, "staff": 1, "billing_lead": 2}


@dataclass
class Decision:
    allowed: bool
    rule: str
    needs_approval: bool = False
    required_role: str = "staff"
    message: str = ""


class GuardDenied(Exception):
    def __init__(self, decision: Decision):
        super().__init__(decision.message or decision.rule)
        self.decision = decision


class Guard:
    def __init__(self, policy: dict | None = None):
        self.policy = policy or load_policy()
        self.allow = set(self.policy["allowlist"])
        self.caps = self.policy["caps"]

    # ---- agent-callable actions -------------------------------------------------
    def check(self, action: str, **ctx) -> Decision:
        if action not in self.allow:
            return Decision(False, "allowlist", message=f"Action '{action}' is not on the allowlist.")
        fn = getattr(self, f"_check_{action}", None)
        return fn(**ctx) if fn else Decision(True, "allowlist")

    def require(self, action: str, **ctx) -> Decision:
        d = self.check(action, **ctx)
        if not d.allowed:
            raise GuardDenied(d)
        return d

    def _check_create_checkout(self, quote_cents: int, amount_cents: int, **_) -> Decision:
        if amount_cents <= 0 or amount_cents > quote_cents:
            return Decision(False, "checkout_amount_equals_quote", message="Checkout must equal the quoted price.")
        return Decision(True, "checkout_amount_equals_quote")

    def _check_create_deposit_auth(self, quote_cents: int, percent: int, amount_cents: int, **_) -> Decision:
        lo, hi = self.caps["deposit_percent_min"], self.caps["deposit_percent_max"]
        if not lo <= percent <= hi:
            return Decision(False, "deposit_percent_range", message=f"Deposit must be {lo}-{hi}% of the price.")
        if amount_cents != round(quote_cents * percent / 100):
            return Decision(False, "deposit_amount_from_quote", message="Deposit amount must come from the quote.")
        return Decision(True, "deposit_percent_range")

    def _check_create_plan(self, quote_cents: int, months: int, **_) -> Decision:
        lo, hi = self.caps["plan_months_min"], self.caps["plan_months_max"]
        if not lo <= months <= hi:
            return Decision(False, "plan_months_range", message=f"Plans run {lo}-{hi} months.")
        if quote_cents < self.caps["plan_min_total_cents"]:
            return Decision(
                False,
                "plan_min_total",
                message=f"Monthly plans start at {money(self.caps['plan_min_total_cents'])} total.",
            )
        return Decision(True, "plan_months_range")

    def _check_create_invoice(self, quote_cents: int, amount_cents: int, **_) -> Decision:
        if amount_cents <= 0 or amount_cents > quote_cents:
            return Decision(False, "invoice_amount_le_quote", message="Invoice cannot exceed the quoted price.")
        return Decision(True, "invoice_amount_le_quote")

    def _check_request_refund(self, captured_cents: int, refunded_cents: int, amount_cents: int, **_) -> Decision:
        remaining = captured_cents - refunded_cents
        if amount_cents <= 0 or amount_cents > remaining:
            return Decision(
                False,
                "refund_le_captured_minus_refunded",
                message="Refund cannot exceed what was paid minus earlier refunds.",
            )
        role = "billing_lead" if amount_cents > self.caps["billing_lead_refund_threshold_cents"] else "staff"
        return Decision(True, "refund_needs_staff_approval", needs_approval=True, required_role=role)

    def _check_request_discount(self, percent: int, **_) -> Decision:
        cap = self.caps["discount_max_percent"]
        if percent <= 0 or percent > cap:
            return Decision(False, "discount_max_percent", message=f"Discounts are capped at {cap}%.")
        return Decision(True, "discount_needs_staff_approval", needs_approval=True)

    def _small(self, key: str, used: int) -> Decision:
        cap = self.caps["small_actions"][key]
        if used >= cap:
            return Decision(False, key, message="That quick action has reached its limit for this booking.")
        return Decision(True, key)

    def _check_extend_hold(self, used: int = 0, **_) -> Decision:
        return self._small("extend_hold_max_per_appointment", used)

    def _check_resend_payment_link(self, used: int = 0, **_) -> Decision:
        return self._small("resend_link_max_per_appointment", used)

    def _check_switch_payment_method(self, used: int = 0, **_) -> Decision:
        return self._small("switch_method_max_per_appointment", used)

    # ---- execution of money moves (staff only, never the agent) -----------------
    def check_execute(self, approval_status: str, required_role: str, actor_role: str) -> Decision:
        if approval_status != "APPROVED":
            return Decision(False, "execute_requires_approved_record", message="Needs an approved request first.")
        if ROLE_RANK.get(actor_role, 0) < ROLE_RANK[required_role]:
            return Decision(False, "role_threshold", message=f"Needs a {required_role.replace('_', ' ')}.")
        return Decision(True, "approved_by_authorized_role")

    def can_decide(self, required_role: str, actor_role: str) -> bool:
        return ROLE_RANK.get(actor_role, 0) >= ROLE_RANK[required_role] and ROLE_RANK.get(actor_role, 0) >= 1


# ---- prompt-injection detector --------------------------------------------------
_PATTERNS = [
    r"ignore (all |your |the |previous |prior )*(rules|instructions|prompts?)",
    r"disregard (all |your |the )*(rules|instructions)",
    r"\b(as|i am|i'm) (the |an )?(admin|administrator|staff|manager|developer|system)\b",
    r"\bsystem prompt\b",
    r"\b(approve|execute|authorize) (the |my |this )?(refund|discount|payment)",
    r"\brefund me\b",
    r"\bjailbreak\b",
    r"\bdeveloper mode\b",
    r"\boverride\b.*\b(limit|cap|guard|policy)",
]
_HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def detect_injection(text: str) -> list[str]:
    """Return reasons the text looks like an injection attempt (empty list = clean)."""
    reasons: list[str] = []
    if _HIDDEN.search(text):
        reasons.append("hidden_unicode")
    lowered = _HIDDEN.sub("", text).lower()
    for p in _PATTERNS:
        if re.search(p, lowered):
            reasons.append(f"pattern:{p[:24]}")
            break
    for blob in _B64.findall(text):
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False).decode("utf-8", "ignore")
        except Exception:
            continue
        if any(re.search(p, decoded.lower()) for p in _PATTERNS):
            reasons.append("base64_payload")
            break
    return reasons
