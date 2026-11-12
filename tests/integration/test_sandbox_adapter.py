"""Sandbox adapter: request-shape tests with respx (always run) + a live sandbox test (skips without keys)."""

import json
import os

import httpx
import pytest
import respx

from app.payments.adapter import PaymentDeclined, PayPalTimeout
from app.payments.sandbox import SandboxPayPalAdapter

BASE = "https://api-m.sandbox.paypal.com"


@pytest.fixture
def mocked():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.post("/v1/oauth2/token").respond(json={"access_token": "T", "expires_in": 3600})
        yield m


def test_refuses_live_url():
    with pytest.raises(ValueError):
        SandboxPayPalAdapter("a", "b", "https://api-m.paypal.com")


def test_create_order_sends_request_id_and_amount(mocked):
    route = mocked.post("/v2/checkout/orders").respond(
        json={"id": "O1", "status": "CREATED", "links": [{"rel": "approve", "href": "https://x/approve"}]}
    )
    a = SandboxPayPalAdapter("id", "secret", BASE)
    r = a.create_order("AUTHORIZE", 11250, "rid-1", "7", "Deposit")
    req = route.calls.last.request
    assert req.headers["PayPal-Request-Id"] == "rid-1"
    body = json.loads(req.content)
    assert body["intent"] == "AUTHORIZE" and body["purchase_units"][0]["amount"]["value"] == "112.50"
    assert r == {"id": "O1", "status": "CREATED", "approve_url": "https://x/approve"}


def test_capture_decline_maps_to_payment_declined(mocked):
    route = mocked.post("/v2/checkout/orders/O1/capture").respond(
        422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]}
    )
    a = SandboxPayPalAdapter("id", "secret", BASE)
    with pytest.raises(PaymentDeclined):
        a.capture_order("O1", "rid", mock="INSTRUMENT_DECLINED")
    assert "INSTRUMENT_DECLINED" in route.calls.last.request.headers["PayPal-Mock-Response"]


def test_timeout_maps_to_paypal_timeout(mocked):
    mocked.post("/v2/checkout/orders/O1/capture").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(PayPalTimeout):
        SandboxPayPalAdapter("id", "secret", BASE).capture_order("O1", "rid")


def test_refund_and_webhook_verify(mocked):
    mocked.post("/v2/payments/captures/C1/refund").respond(json={"id": "R1", "status": "COMPLETED"})
    mocked.post("/v1/notifications/verify-webhook-signature").respond(json={"verification_status": "SUCCESS"})
    a = SandboxPayPalAdapter("id", "secret", BASE, webhook_id="WH")
    assert a.refund_capture("C1", 2500, "r")["refund_id"] == "R1"
    assert a.verify_webhook({"PAYPAL-AUTH-ALGO": "x"}, b'{"id":"E1"}') is True
    assert SandboxPayPalAdapter("id", "secret", BASE).verify_webhook({}, b"{}") is False


needs_keys = pytest.mark.skipif(
    not (os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET")),
    reason="PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set",
)


@pytest.mark.sandbox
@needs_keys
def test_live_sandbox_create_and_get_order():
    a = SandboxPayPalAdapter(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"], BASE)
    o = a.create_order("CAPTURE", 100, f"pytest-{os.getpid()}", "pytest", "pytest smoke order")
    assert o["status"] in ("CREATED", "PAYER_ACTION_REQUIRED")
    assert a.get_order(o["id"])["id"] == o["id"]
