"""PaymentAdapter interface shared by the local (fake) and PayPal sandbox adapters."""

from __future__ import annotations

from typing import Protocol


class PayPalError(Exception):
    pass


class PaymentDeclined(PayPalError):
    def __init__(self, code: str = "INSTRUMENT_DECLINED"):
        super().__init__(code)
        self.code = code


class PayPalTimeout(PayPalError):
    pass


class OrderNotFound(PayPalError):
    pass


class PaymentAdapter(Protocol):
    mode: str

    def create_order(
        self, intent: str, amount_cents: int, request_id: str, reference_id: str, description: str
    ) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def capture_order(self, order_id: str, request_id: str, mock: str | None = None) -> dict: ...
    def authorize_order(self, order_id: str, request_id: str, mock: str | None = None) -> dict: ...
    def capture_authorization(self, authorization_id: str, amount_cents: int, request_id: str) -> dict: ...
    def void_authorization(self, authorization_id: str, request_id: str) -> dict: ...
    def refund_capture(self, capture_id: str, amount_cents: int, request_id: str) -> dict: ...
    def create_plan(self, total_cents: int, months: int, schedule: list[int], request_id: str, name: str) -> dict: ...
    def create_invoice(self, amount_cents: int, email: str, description: str, request_id: str) -> dict: ...
    def verify_webhook(self, headers: dict, body: bytes) -> bool: ...
