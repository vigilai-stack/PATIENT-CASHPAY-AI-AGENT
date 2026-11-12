"""PayPal sandbox adapter (REST, Orders v2 / Payments v2 / Subscriptions v1 / Invoicing v2 / Webhooks v1).

Sandbox only: the base URL is checked at startup and live URLs are refused (see app.config).
Every POST that can move money carries a PayPal-Request-Id so retries never double-charge.
"""

from __future__ import annotations

import json
import time

import httpx

from app.payments.adapter import OrderNotFound, PaymentDeclined, PayPalError, PayPalTimeout

DECLINE_CODES = {
    "INSTRUMENT_DECLINED",
    "INSUFFICIENT_FUNDS",
    "TRANSACTION_REFUSED",
    "PAYER_ACTION_REQUIRED",
    "ORDER_NOT_APPROVED",
    "PAYER_CANNOT_PAY",
}


def _value(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


class SandboxPayPalAdapter:
    mode = "sandbox"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        webhook_id: str = "",
        transport: httpx.BaseTransport | None = None,
    ):
        if "sandbox" not in base_url:
            raise ValueError("Sandbox only")
        self._id, self._secret, self.base, self.webhook_id = client_id, client_secret, base_url, webhook_id
        self._http = httpx.Client(base_url=base_url, timeout=httpx.Timeout(15.0, connect=5.0), transport=transport)
        self._token: str | None = None
        self._token_exp = 0.0

    # -- plumbing --
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._http.post(
            "/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(self._id, self._secret)
        )
        r.raise_for_status()
        data = r.json()
        self._token, self._token_exp = data["access_token"], time.time() + int(data.get("expires_in", 300))
        return self._token

    def _req(
        self,
        method: str,
        path: str,
        *,
        json_body=None,
        request_id: str | None = None,
        mock: str | None = None,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self._auth()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if mock:  # sandbox negative testing
            headers["PayPal-Mock-Response"] = json.dumps({"mock_application_codes": mock})
        try:
            r = self._http.request(method, path, json=json_body, headers=headers)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout) as e:
            raise PayPalTimeout(path) from e
        if r.status_code == 404:
            raise OrderNotFound(path)
        if r.status_code >= 400:
            body = r.json() if r.content else {}
            issues = {d.get("issue") for d in body.get("details", []) if isinstance(d, dict)}
            hit = issues & DECLINE_CODES
            if hit:
                raise PaymentDeclined(sorted(hit)[0])
            raise PayPalError(f"{r.status_code} {body.get('name', '')} {sorted(issues)}")
        return r.json() if r.content else {}

    def client_ready(self) -> bool:
        try:
            self._auth()
            return True
        except Exception:
            return False

    # -- orders / payments --
    def create_order(self, intent, amount_cents, request_id, reference_id, description):
        body = {
            "intent": intent,
            "purchase_units": [
                {
                    "reference_id": reference_id,
                    "description": description[:127],
                    "amount": {"currency_code": "USD", "value": _value(amount_cents)},
                }
            ],
        }
        data = self._req("POST", "/v2/checkout/orders", json_body=body, request_id=request_id)
        approve = next(
            (link["href"] for link in data.get("links", []) if link.get("rel") in ("approve", "payer-action")),
            None,
        )
        return {"id": data["id"], "status": data["status"], "approve_url": approve}

    def get_order(self, order_id):
        data = self._req("GET", f"/v2/checkout/orders/{order_id}")
        out = {"id": data["id"], "status": data["status"]}
        pu = (data.get("purchase_units") or [{}])[0].get("payments", {})
        if pu.get("captures"):
            out["capture_id"] = pu["captures"][0]["id"]
        if pu.get("authorizations"):
            out["authorization_id"] = pu["authorizations"][0]["id"]
        return out

    def capture_order(self, order_id, request_id, mock=None):
        data = self._req(
            "POST", f"/v2/checkout/orders/{order_id}/capture", json_body={}, request_id=request_id, mock=mock
        )
        cap = data["purchase_units"][0]["payments"]["captures"][0]
        if cap["status"] == "DECLINED":
            raise PaymentDeclined("INSTRUMENT_DECLINED")
        return {"status": data["status"], "capture_id": cap["id"]}

    def authorize_order(self, order_id, request_id, mock=None):
        data = self._req(
            "POST",
            f"/v2/checkout/orders/{order_id}/authorize",
            json_body={},
            request_id=request_id,
            mock=mock,
        )
        auth = data["purchase_units"][0]["payments"]["authorizations"][0]
        return {"status": data["status"], "authorization_id": auth["id"]}

    def capture_authorization(self, authorization_id, amount_cents, request_id):
        data = self._req(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/capture",
            json_body={
                "amount": {"currency_code": "USD", "value": _value(amount_cents)},
                "final_capture": True,
            },
            request_id=request_id,
        )
        return {"status": data["status"], "capture_id": data["id"]}

    def void_authorization(self, authorization_id, request_id):
        self._req("POST", f"/v2/payments/authorizations/{authorization_id}/void", request_id=request_id)
        return {"status": "VOIDED"}

    def refund_capture(self, capture_id, amount_cents, request_id):
        data = self._req(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json_body={"amount": {"currency_code": "USD", "value": _value(amount_cents)}},
            request_id=request_id,
        )
        return {"status": data["status"], "refund_id": data["id"]}

    # -- subscriptions (monthly plan) --
    def create_plan(self, total_cents, months, schedule, request_id, name):
        prod = self._req(
            "POST",
            "/v1/catalogs/products",
            request_id=f"{request_id}-prod",
            json_body={"name": name[:127], "type": "SERVICE", "category": "MEDICAL_SERVICES"},
        )
        first, rest = schedule[0], schedule[1] if len(schedule) > 1 else schedule[0]
        plan = self._req(
            "POST",
            "/v1/billing/plans",
            request_id=f"{request_id}-plan",
            json_body={
                "product_id": prod["id"],
                "name": f"{name[:100]} plan",
                "status": "ACTIVE",
                "billing_cycles": [
                    {
                        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                        "tenure_type": "REGULAR",
                        "sequence": 1,
                        "total_cycles": months,
                        "pricing_scheme": {"fixed_price": {"currency_code": "USD", "value": _value(rest)}},
                    }
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "payment_failure_threshold": 2,
                    # first instalment absorbs the rounding so the plan totals the quote exactly
                    "setup_fee": {"currency_code": "USD", "value": _value(first - rest)},
                    "setup_fee_failure_action": "CANCEL",
                },
            },
        )
        sub = self._req(
            "POST",
            "/v1/billing/subscriptions",
            request_id=f"{request_id}-sub",
            json_body={"plan_id": plan["id"]},
        )
        approve = next((link["href"] for link in sub.get("links", []) if link.get("rel") == "approve"), None)
        return {
            "plan_id": plan["id"],
            "subscription_id": sub["id"],
            "status": sub["status"],
            "approve_url": approve,
        }

    # -- invoicing --
    def create_invoice(self, amount_cents, email, description, request_id):
        inv = self._req(
            "POST",
            "/v2/invoicing/invoices",
            request_id=request_id,
            json_body={
                "detail": {"currency_code": "USD", "note": "Cash-pay visit (synthetic demo data)"},
                "primary_recipients": [{"billing_info": {"email_address": email}}],
                "items": [
                    {
                        "name": description[:200],
                        "quantity": "1",
                        "unit_amount": {"currency_code": "USD", "value": _value(amount_cents)},
                    }
                ],
            },
        )
        inv_id = inv.get("id") or inv.get("href", "").rstrip("/").split("/")[-1]
        sent = self._req(
            "POST",
            f"/v2/invoicing/invoices/{inv_id}/send",
            request_id=f"{request_id}-send",
            json_body={"send_to_recipient": True},
        )
        return {"invoice_id": inv_id, "status": "SENT", "pay_url": sent.get("href")}

    # -- webhooks --
    def verify_webhook(self, headers, body):
        h = {k.lower(): v for k, v in headers.items()}
        if not self.webhook_id:
            return False
        payload = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": self.webhook_id,
            "webhook_event": json.loads(body),
        }
        try:
            data = self._req("POST", "/v1/notifications/verify-webhook-signature", json_body=payload)
        except PayPalError:
            return False
        return data.get("verification_status") == "SUCCESS"
