"""Deterministic, stateful local PayPal stand-in. Runs with no keys and no network.

It mirrors PayPal response shapes, honours PayPal-Request-Id idempotency, signs its own webhooks
with a local HMAC key, and supports fault injection (decline, timeout) for failure tests.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json

from app.payments.adapter import OrderNotFound, PaymentDeclined, PayPalTimeout

SIG_HEADER = "x-fake-signature"


class FakePayPalAdapter:
    mode = "fake"

    def __init__(self, webhook_key: str = "local-demo-webhook-key"):
        self.webhook_key = webhook_key.encode()
        self.orders: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self._idem: dict[str, dict] = {}
        self._seq = itertools.count(1)
        self.faults: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str]] = []  # (method, request_id) for tests

    # -- helpers --
    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._seq):06d}"

    def _fault(self, op: str) -> str | None:
        q = self.faults.get(op)
        return q.pop(0) if q else None

    def _idempotent(self, op: str, request_id: str, fn):
        self.calls.append((op, request_id))
        fault = self._fault(op)
        if fault == "timeout":  # the request never got an answer
            raise PayPalTimeout(op)
        key = f"{op}:{request_id}"
        if key not in self._idem:
            self._idem[key] = fn()
        if fault == "timeout_after_commit":  # PayPal did the work but the reply was lost
            raise PayPalTimeout(op)
        return self._idem[key]

    # -- orders --
    def create_order(self, intent, amount_cents, request_id, reference_id, description):
        def do():
            oid = self._id("FAKEORDER")
            self.orders[oid] = {
                "id": oid,
                "intent": intent,
                "status": "CREATED",
                "amount_cents": amount_cents,
                "reference_id": reference_id,
                "description": description,
            }
            return {"id": oid, "status": "CREATED", "approve_url": f"/pay/approve/{oid}"}

        return self._idempotent("create_order", request_id, do)

    def approve(self, order_id: str) -> dict:
        o = self.orders.get(order_id)
        if not o:
            raise OrderNotFound(order_id)
        if o["status"] == "CREATED":
            o["status"] = "APPROVED"
        return o

    def get_order(self, order_id):
        o = self.orders.get(order_id)
        if not o:
            raise OrderNotFound(order_id)
        return dict(o)

    def capture_order(self, order_id, request_id, mock=None):
        def do():
            o = self.get_order(order_id)
            if mock or self._fault("decline"):
                self.orders[order_id]["status"] = "APPROVED"
                raise PaymentDeclined(mock or "INSTRUMENT_DECLINED")
            if o["status"] == "COMPLETED":
                return {"status": "COMPLETED", "capture_id": o["capture_id"]}
            cid = self._id("FAKECAP")
            self.captures[cid] = {
                "id": cid,
                "amount_cents": o["amount_cents"],
                "refunded_cents": 0,
                "order_id": order_id,
            }
            self.orders[order_id].update(status="COMPLETED", capture_id=cid)
            return {"status": "COMPLETED", "capture_id": cid}

        return self._idempotent("capture_order", request_id, do)

    def authorize_order(self, order_id, request_id, mock=None):
        def do():
            o = self.get_order(order_id)
            if mock:
                raise PaymentDeclined(mock)
            aid = self._id("FAKEAUTH")
            self.authorizations[aid] = {"id": aid, "amount_cents": o["amount_cents"], "status": "CREATED"}
            self.orders[order_id].update(status="COMPLETED", authorization_id=aid)
            return {"status": "COMPLETED", "authorization_id": aid}

        return self._idempotent("authorize_order", request_id, do)

    def capture_authorization(self, authorization_id, amount_cents, request_id):
        def do():
            a = self.authorizations[authorization_id]
            cid = self._id("FAKECAP")
            a["status"] = "CAPTURED"
            self.captures[cid] = {"id": cid, "amount_cents": amount_cents, "refunded_cents": 0}
            return {"status": "COMPLETED", "capture_id": cid}

        return self._idempotent("capture_authorization", request_id, do)

    def void_authorization(self, authorization_id, request_id):
        def do():
            self.authorizations[authorization_id]["status"] = "VOIDED"
            return {"status": "VOIDED"}

        return self._idempotent("void_authorization", request_id, do)

    def refund_capture(self, capture_id, amount_cents, request_id):
        def do():
            cap = self.captures.setdefault(
                capture_id,
                {
                    "id": capture_id,
                    "amount_cents": 10**9,  # seeded history
                    "refunded_cents": 0,
                },
            )
            if cap["refunded_cents"] + amount_cents > cap["amount_cents"]:
                raise PaymentDeclined("REFUND_AMOUNT_EXCEEDED")
            cap["refunded_cents"] += amount_cents
            return {"status": "COMPLETED", "refund_id": self._id("FAKEREFUND")}

        return self._idempotent("refund_capture", request_id, do)

    def create_plan(self, total_cents, months, schedule, request_id, name):
        def do():
            return {
                "plan_id": self._id("FAKEPLAN"),
                "subscription_id": self._id("FAKESUB"),
                "status": "APPROVAL_PENDING",
                "approve_url": None,
            }

        return self._idempotent("create_plan", request_id, do)

    def create_invoice(self, amount_cents, email, description, request_id):
        def do():
            iid = self._id("FAKEINV")
            return {"invoice_id": iid, "status": "SENT", "pay_url": None}

        return self._idempotent("create_invoice", request_id, do)

    # -- webhooks --
    def sign(self, body: bytes) -> str:
        return hmac.new(self.webhook_key, body, hashlib.sha256).hexdigest()

    def make_event(self, event_id: str, event_type: str, resource: dict) -> tuple[bytes, dict]:
        body = json.dumps({"id": event_id, "event_type": event_type, "resource": resource}).encode()
        return body, {SIG_HEADER: self.sign(body)}

    def verify_webhook(self, headers, body):
        sig = {k.lower(): v for k, v in headers.items()}.get(SIG_HEADER, "")
        return hmac.compare_digest(sig, self.sign(body))
