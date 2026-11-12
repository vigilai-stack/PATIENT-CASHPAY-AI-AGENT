"""PayPal sandbox smoke test. Needs PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET in the environment.

Without the secret it prints SKIP and exits 0 (so automated test runs pass without keys). With keys it:
  1. gets an OAuth token,
  2. creates a CAPTURE order and an AUTHORIZE (deposit) order and prints their approve links,
  3. reads them back, 4. checks negative testing (INSTRUMENT_DECLINED) maps to a decline,
  5. tries Invoicing and Transaction Search (reports if the feature is off in the sandbox app),
  6. runs the optional paypal-agent-toolkit read-only check if installed.
After approving an order in the browser with a sandbox buyer, capture it with:
  python scripts/sandbox_smoke.py --capture ORDER_ID
Output is redacted (no tokens, no secret) and saved to evidence/sandbox_smoke.json.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.payments import toolkit_bridge  # noqa: E402
from app.payments.adapter import PaymentDeclined, PayPalError  # noqa: E402
from app.payments.sandbox import SandboxPayPalAdapter  # noqa: E402

BASE = "https://api-m.sandbox.paypal.com"


def main(argv: list[str]) -> int:
    cid, secret = os.getenv("PAYPAL_CLIENT_ID", ""), os.getenv("PAYPAL_CLIENT_SECRET", "")
    if not (cid and secret):
        print("SKIP: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set. Nothing was called.")
        return 0
    a = SandboxPayPalAdapter(cid, secret, BASE)
    out: dict = {"ran_at_utc": datetime.now(UTC).isoformat(timespec="seconds"), "base": BASE, "steps": []}

    def rec(name, ok, **info):
        out["steps"].append({"step": name, "ok": ok, **info})
        print(("PASS " if ok else "FAIL ") + name, json.dumps(info))

    if "--capture" in argv:
        oid = argv[argv.index("--capture") + 1]
        try:
            r = a.capture_order(oid, f"smoke-cap-{oid}")
            rec("capture_approved_order", True, order=oid, status=r["status"], capture=r["capture_id"])
        except PayPalError as e:
            rec("capture_approved_order", False, order=oid, error=str(e))
        return _save(out)

    rec("oauth_token", a.client_ready())
    run = uuid.uuid4().hex[:8]
    for intent, cents in (("CAPTURE", 12500), ("AUTHORIZE", 11250)):
        try:
            o = a.create_order(intent, cents, f"smoke-{run}-{intent}", "smoke", f"Smoke {intent}")
            g = a.get_order(o["id"])
            rec(f"create_order_{intent.lower()}", True, order=o["id"], status=g["status"], approve=o["approve_url"])
        except PayPalError as e:
            rec(f"create_order_{intent.lower()}", False, error=str(e))
    try:
        o = a.create_order("CAPTURE", 100, f"smoke-{run}-neg", "smoke", "Negative test")
        a.capture_order(o["id"], f"smoke-{run}-negcap", mock="INSTRUMENT_DECLINED")
        rec("negative_test_decline", False, note="capture unexpectedly succeeded")
    except PaymentDeclined as e:
        rec("negative_test_decline", True, code=e.code)
    except PayPalError as e:
        rec("negative_test_decline", False, error=str(e))
    try:
        inv = a.create_invoice(4900, "buyer@example.com", "Smoke invoice", f"smoke-{run}-inv")
        rec("invoice_create_send", True, invoice=inv["invoice_id"])
    except PayPalError as e:
        rec("invoice_create_send", False, error=str(e), hint="Turn on Invoicing in the sandbox app settings.")
    try:
        start = datetime.now(UTC).replace(microsecond=0)
        a._req(
            "GET",
            f"/v1/reporting/transactions?start_date={(start.replace(day=1)).isoformat()}&end_date={start.isoformat()}",
        )
        rec("transaction_search", True)
    except PayPalError as e:
        rec("transaction_search", False, error=str(e), hint="Turn on Transaction Search in the sandbox app.")
    if toolkit_bridge.available():
        try:
            toolkit_bridge.run_readonly(cid, secret, "list_transactions", {})
            rec("agent_toolkit_readonly", True)
        except Exception as e:  # report, don't hide
            rec("agent_toolkit_readonly", False, error=str(e)[:200])
    else:
        rec("agent_toolkit_readonly", False, note="paypal-agent-toolkit not installed (pip install .[toolkit])")
    return _save(out)


def _save(out: dict) -> int:
    p = Path(__file__).resolve().parent.parent / "evidence" / "sandbox_smoke.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(out, indent=2))
    print(f"Saved {p}")
    return 0 if all(s["ok"] for s in out["steps"] if s["step"] != "agent_toolkit_readonly") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
