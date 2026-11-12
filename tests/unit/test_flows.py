import json

from app.models import Appointment, Payment
from tests.conftest import book_and_pay, chat, staff_client


def test_landing_and_health(env):
    c = env["client"]
    r = c.get("/")
    for who in ("patient", "staff", "billing_lead"):
        assert f'action="/demo/login/{who}"' in r.text  # role tabs and the Scene 8 one-click personas
    assert 'id="try-patient"' in r.text and 'id="try-staff"' in r.text and 'id="try-lead"' in r.text
    assert c.get("/health").json()["payment_mode"] == "fake"


def test_patient_api_needs_login(env):
    assert env["client"].post("/api/chat", json={"message": "hi"}).status_code == 401
    assert env["client"].get("/api/staff/payments").status_code == 401


def test_input_validation(patient):
    from app.conversation import MAX_ACCEPTED_MESSAGE_CHARS

    assert patient.post("/api/chat", json={"message": ""}).status_code == 422
    assert patient.post("/api/chat", json={"message": "   "}).status_code == 422
    long = "x" * 501
    accepted = patient.post("/api/chat", json={"message": long, "client_message_id": "valid-long-01"})
    assert accepted.status_code == 200
    assert accepted.json()["saved_message"] == long
    assert patient.post("/api/chat", json={"message": "x" * (MAX_ACCEPTED_MESSAGE_CHARS + 1)}).status_code == 422
    assert patient.post("/api/chat", json={}).status_code == 422
    patient_lines = [
        row["body"] for row in patient.get("/api/chat/transcript").json()["messages"] if row["speaker"] == "patient"
    ]
    assert patient_lines == [long]


def test_price_question_plain_language(patient):
    j = chat(patient, "How much is a knee MRI?")
    assert "$450.00" in j["text"] and "included" in j["text"].lower()


def test_full_payment_e2e_writes_fhir(env, patient, db_session):
    pid = book_and_pay(patient)
    assert patient.get(f"/pay/{pid}").status_code == 200
    r = patient.post(f"/api/payments/{pid}/confirm", json={"test_outcome": "approve"}).json()
    assert r["ok"] and r["status"] == "CAPTURED" and r["appointment_status"] == "CONFIRMED"
    appt = db_session.get(Appointment, r["appointment_id"])
    assert appt.fhir_appointment_id == "appt-1"
    body = json.loads(env["fhir"][0])
    assert body["resourceType"] == "Appointment" and body["status"] == "booked"


def test_deposit_authorize_then_staff_capture(env, patient):
    pid = book_and_pay(patient, "pay deposit")
    r = patient.post(f"/api/payments/{pid}/confirm", json={}).json()
    assert r["status"] == "AUTHORIZED" and r["amount"] == "$112.50"
    s = staff_client(env)
    r = s.post(f"/api/staff/payments/{pid}/capture-deposit").json()
    assert r["status"] == "CAPTURED"


def test_plan_and_invoice(env, patient, db_session):
    pid = book_and_pay(patient, "pay plan 3")
    r = patient.post(f"/api/payments/{pid}/confirm", json={}).json()
    assert r["status"] == "PLAN_ACTIVE"
    j = chat(patient, "book urgent care")
    chat(patient, next(b["send"] for b in j["buttons"]))
    j = chat(patient, "pay invoice")
    assert "invoice" in j["text"].lower()
    p = db_session.query(Payment).filter_by(kind="INVOICE").one()
    assert p.status == "INVOICE_SENT"


def test_plan_below_minimum_is_refused(patient):
    j = chat(patient, "book telehealth video")
    chat(patient, next(b["send"] for b in j["buttons"]))
    j = chat(patient, "pay plan 3")
    assert "Monthly plans start at $150.00" in j["text"]


def test_small_actions_are_capped(patient):
    j = chat(patient, "book mri")
    chat(patient, next(b["send"] for b in j["buttons"]))
    assert "more minutes" in chat(patient, "I need more time")["text"]
    assert "limit" in chat(patient, "I need more time")["text"]
    chat(patient, "pay full")
    assert chat(patient, "switch to venmo")["pay_url"]


def test_refund_request_goes_to_queue_then_staff_executes(env, patient):
    j = chat(patient, "I'd like a refund of $20.00 for my last visit please")
    assert "$20.00" in j["text"] and "can't approve" in j["text"]
    s = staff_client(env)
    rows = s.get("/api/staff/approvals").json()
    mine = next(r for r in rows if r["amount"] == "$20.00")
    assert mine["status"] == "PENDING"
    r = s.post(f"/api/staff/approvals/{mine['id']}/decide", json={"approve": True}).json()
    assert r["status"] == "EXECUTED"
    pays = s.get("/api/staff/payments").json()
    assert any(p["status"] == "PARTIALLY_REFUNDED" for p in pays)
    assert s.get("/api/staff/audit/verify").json()["ok"]


def test_discount_request_then_approval_lowers_quote(env, patient, db_session):
    j = chat(patient, "book mri")
    chat(patient, next(b["send"] for b in j["buttons"]))
    j = chat(patient, "that's expensive, any discount? 10%")
    assert "10% discount" in j["text"]
    s = staff_client(env)
    ar = next(r for r in s.get("/api/staff/approvals").json() if r["kind"] == "DISCOUNT")
    assert s.post(f"/api/staff/approvals/{ar['id']}/decide", json={"approve": True}).json()["status"] == "EXECUTED"
    appt = db_session.query(Appointment).filter_by(status="HELD").one()
    db_session.refresh(appt)
    assert appt.quote_cents == 40500


def test_staff_views_and_analyst(env):
    s = staff_client(env, "billing_lead")
    for u in (
        "/api/staff/approvals",
        "/api/staff/payments",
        "/api/staff/recon",
        "/api/staff/audit",
        "/api/staff/kpis",
        "/api/staff/me",
    ):
        assert s.get(u).status_code == 200
    assert s.get("/staff").status_code == 200
    for q in ("refunds by service", "collected by location", "payments by method", "status", "anything"):
        assert s.post("/api/staff/analyst", json={"question": q}).json()["title"]
    r = s.post("/api/staff/analyst", json={"question": "issue a refund to everyone"}).json()
    assert r["chart"] is None
    assert s.post("/api/staff/reconcile").json()["report"]


def test_staff_payment_list_is_clinic_wide_and_patients_are_refused(env):
    client = env["client"]
    assert client.get("/api/staff/payments").status_code == 401
    assert client.post("/demo/login/patient", follow_redirects=False).status_code == 303
    assert client.get("/api/staff/payments").status_code == 401
    staff = staff_client(env, "staff")
    names = {row["patient"] for row in staff.get("/api/staff/payments").json()}
    assert {"Lily Bennett", "Marcus Lee"} <= names
    page = staff.get("/staff").text
    assert "decision-note').value||'').trim()" not in page
    assert "still in the box" in page


def test_staff_audit_api_returns_full_details(env, db_session):
    from app import audit

    note = "n" * 240
    audit.record(db_session, "t", "long_detail", "OK", details={"note": note})
    db_session.commit()
    rows = staff_client(env, "staff").get("/api/staff/audit").json()
    match = next(row for row in rows if row["action"] == "long_detail")
    assert json.loads(match["details"])["note"] == note
