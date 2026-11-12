"""Optional bridge to PayPal's `paypal-agent-toolkit` (install with `pip install .[toolkit]`).

When the toolkit is installed and sandbox keys are set, read-only agent lookups (order details,
transaction list) go through the toolkit's own tool methods, so the agent uses the same tool
definitions PayPal ships. Money-moving calls stay in SandboxPayPalAdapter behind the guard.
"""

from __future__ import annotations


def available() -> bool:
    try:
        import paypal_agent_toolkit  # noqa: F401
    except Exception:
        return False
    return True


def run_readonly(client_id: str, secret: str, method: str, params: dict) -> dict:
    if method not in {"get_order_details", "list_transactions"}:
        raise PermissionError("Only read-only toolkit methods are bridged.")
    from paypal_agent_toolkit.shared.api import PayPalAPI
    from paypal_agent_toolkit.shared.configuration import Context

    return PayPalAPI(client_id, secret, Context(sandbox=True)).run(method, params)
