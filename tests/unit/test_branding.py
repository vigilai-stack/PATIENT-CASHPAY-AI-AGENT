"""Displayed app name matches the repository name. Behavior and URLs stay the same."""

import re

from tests.conftest import book_and_pay, staff_client

BRAND = "Patient-cashpay_AI-agent"
OLD = "CashPay Clinic Agent"
FOOTER = f'<footer class="site-foot"><span><strong>PayPal Hackathon 2026</strong> &middot; {BRAND}</span>'
TEAMBAR = (
    '<aside class="teambar glass-b" id="team-bar" aria-label="Team Members">'
    '<a class="m" href="mailto:sangeeta@vigilaisolutions.com">'
    '<img src="/static/mock/team-sangeeta.jpg" alt="" width="32" height="32">'
    '<span class="tx"><span class="nm">Sangeeta Sinha</span>'
    '<span class="em">sangeeta@<wbr>vigilaisolutions.com</span></span></a><span class="tl">Team Members</span>'
    '<a class="m" href="mailto:Chittranjan.garg@gmail.com">'
    '<img src="/static/mock/team-chittranjan.jpg" alt="" width="32" height="32">'
    '<span class="tx"><span class="nm">Chittranjan Garg</span>'
    '<span class="em">Chittranjan.garg@<wbr>gmail.com</span></span></a></aside>'
)
POWERED = '<span class="powered" id="powered-by"><b>Powered by</b>'


def _chrome(text: str) -> None:
    assert BRAND in text
    assert OLD not in text
    assert "Patient CashPay Clinic Agent" not in text
    assert "CashPay <span>Clinic Agent</span>" not in text
    logo = '<div class="logo"><span class="nw">Patient-cashpay</span><wbr><span class="nw">_AI-agent</span></div>'
    assert logo in text and re.sub(r"<[^>]+>", "", logo) == BRAND
    assert FOOTER in text
    assert TEAMBAR in text
    assert 'id="hackathon-badge"' in text and "Hackathon 2026" in text
    assert 'id="role-rail"' in text
    assert POWERED in text and "AG Grid Community" in text and "MIT License" in text
    assert "Sangeeta Sinha" in text and "Chittranjan Garg" in text
    assert "mailto:Chittranjan.garg@gmail.com" in text
    # The site uses the capitalized address; only his verbatim About text keeps his own spelling.
    outside_about = re.sub(r'<details class="about">.*?</details>', "", text, flags=re.S)
    assert "chittranjan.garg@gmail.com" not in outside_about
    assert "mailto:sangeeta@vigilaisolutions.com" in text
    assert 'class="foot"' not in text
    assert f'content="{BRAND}"' in text


def test_pages_and_openapi_use_repository_name(env, patient):
    landing = env["client"].get("/")
    assert landing.status_code == 200
    assert f"<title>{BRAND}</title>" in landing.text
    _chrome(landing.text)
    assert "Botox" in landing.text and "/demo/login/patient" in landing.text

    chat = patient.get("/patient")
    assert chat.status_code == 200
    assert f"<title>Chat - {BRAND}</title>" in chat.text
    _chrome(chat.text)

    pid = book_and_pay(patient, "pay full")
    pay = patient.get(f"/pay/{pid}")
    assert pay.status_code == 200
    assert f"<title>Secure checkout - {BRAND}</title>" in pay.text
    _chrome(pay.text)

    staff = staff_client(env)
    console = staff.get("/staff")
    assert console.status_code == 200
    assert f"<title>Staff console - {BRAND}</title>" in console.text
    _chrome(console.text)

    spec = env["client"].get("/openapi.json")
    assert spec.status_code == 200
    assert spec.json()["info"]["title"] == BRAND
    health = env["client"].get("/health").json()
    assert health["ok"] is True and "title" not in health
