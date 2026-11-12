"""Gallery screenshots from the running app (1500x1000, 3:2). Usage:
    pip install playwright && playwright install chromium
    BASE_URL=http://localhost:8000 python scripts/media/screens.py
Writes media/gallery_01 ... gallery_14. Run it against a fresh demo database.
"""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.getenv("BASE_URL", "http://localhost:8000")
OUT = Path(__file__).resolve().parents[2] / "media"


def main():
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})

        def shot(name):
            page.wait_for_timeout(700)
            page.screenshot(path=OUT / f"gallery_{name}.png")

        def say(text):
            page.fill("#msg", text)
            page.click("#say button")
            page.wait_for_timeout(900)

        def last_bot_button(label):
            page.locator(".msg.bot").last.locator(".chips a, .chips button", has_text=label).first.click()
            page.wait_for_timeout(900)

        def tab(name):
            page.click(f"#tabs button[data-t={name}]")
            page.wait_for_timeout(1200)

        page.goto(BASE)
        page.wait_for_timeout(3500)
        shot("01_landing")

        page.click("#rail-patient")
        page.wait_for_url("**/patient")
        shot("02_try_as_patient")
        say("How much is a knee MRI?")
        shot("03_patient_price")
        page.click("#starts button.trick")
        page.wait_for_selector(".acard")
        shot("08_guard_injection")
        say("lab price")
        shot("04_patient_cbc_lab")
        last_bot_button("Book Lab panel")
        page.locator(".msg.bot").last.locator(".chips button").first.click()
        page.wait_for_timeout(900)
        shot("05_pay_options")
        last_bot_button("Pay a 25% deposit")
        shot("06_checkout_link")
        last_bot_button("Open secure checkout")
        page.wait_for_url("**/pay/*")
        shot("07_checkout")
        if page.locator("#approve").count():
            page.click("#approve")
            page.wait_for_timeout(900)

        page.click("#rail-staff")
        page.wait_for_selector(".ag-row")
        shot("09_staff_approvals")
        tab("payments")
        shot("10_payments")
        tab("recon")
        page.click("#reconcile")
        page.wait_for_timeout(1200)
        shot("11_reconciliation")
        tab("audit")
        page.click("#verify")
        page.wait_for_timeout(1200)
        shot("12_audit_verified")

        page.click("#rail-lead")
        page.wait_for_selector(".ag-row")
        shot("13_billing_lead")
        page.click('button[data-q="payments by method"]')
        page.wait_for_timeout(1500)
        page.locator("#chart").scroll_into_view_if_needed()
        page.evaluate("window.scrollBy(0, 160)")
        shot("14_paypal_vs_venmo")
        browser.close()
    print("Saved screenshots to", OUT)


if __name__ == "__main__":
    main()
