## **Changelog**

Data note: runs entirely on synthetic data; no real patient or clinic data is used.

#### **Version 0.1.0 (PayPal Hackathon 2026)**

##### **What is in this version**

- **Patient chat agent** (`app/agent.py`, with the OpenAI interpreter in `app/llm.py`): plain-language prices, saved conversation per patient, booking with a 15-minute hold, pay in full / deposit / monthly plan / invoice, PayPal + Venmo checkout, refund and discount requests routed to staff, capped small actions (extend hold, resend link, switch method), Botox per-unit quantity handling.
- **Spending firewall** (`app/guard.py`, `config/clinic_policy.yaml`): allowlisted actions, caps on deposits, plans, discounts, and refunds, role-based staff approval, injection detection, price validation against the database.
- **PayPal sandbox adapter** (`app/payments/sandbox.py`): Orders v2, Payments v2, Subscriptions v1, Invoicing v2, Webhooks v1, sandbox only, with idempotency keys, decline mapping, and negative-test support.
- **PayPal JS SDK v6** checkout page with PayPal and Venmo buttons (`app/web/templates/pay.html`).
- **Optional PayPal Agent Toolkit bridge** (`app/payments/toolkit_bridge.py`): read-only order details and transaction list through PayPal's own tool definitions.
- **Interface:** glass panels on a violet background; an 8-scene home page (Scene 1 animates a refund request that stops at a human, $0 moved); a role bar with one-click demo logins (Patient, Staff, Billing lead); the PayPal Hackathon 2026 badge and the team bar on every page; layouts for computer and phone.
- **Staff console** (`app/web/templates/staff.html`): approvals queue, payments, reconciliation, hash-chained audit log with verification, read-only billing analyst, and a **Reset demo** button locked behind an **Allow reset** switch with a note for judges.
- **Rolling demo calendar** (`app/seed.py`, `ensure_future_slots`): every service keeps open times about five weeks ahead (Monday to Saturday). It only adds open times and never changes holds, bookings, or payments.
- **Clinic time:** appointment times are stored in UTC and shown to patients in Houston time (America/Chicago), for example "8:00 AM CT". When a patient picks a time, the chat shows that time in words, never an internal id.
- **Team About:** both team members' About sections appear in the README and in the home page Team Members cards, with the same layout for each.
- **FHIR R4** `Appointment` written per confirmed booking (`app/fhir.py`), with a bundled stub (`fhir_stub/`) for the demo.
- **Offline payment adapter** (`app/payments/fake.py`): the full flow runs with no keys, for tests and development.
- **End-to-end demo check** (`scripts/demo.py`): 14 steps; it fails if any step fails.
- **Sandbox smoke test** (`scripts/sandbox_smoke.py`): OAuth, orders, decline, invoice, transaction search, toolkit check.
- **Executable walkthrough** (`notebooks/walkthrough.ipynb`): 7 sections, from the price question to the billing analyst.
- **Screenshots** (`media/gallery_01`–`gallery_14`): home page, patient chat, prices, pay options, checkout, guard, approvals, payments, reconciliation, audit, billing lead, PayPal vs Venmo.
- **Tests:** unit, integration, and failure-path tests (`tests/`).
- **Deployment:** Docker Compose on AWS Lightsail, with Caddy for automatic HTTPS, for the live demo (`deploy/lightsail/`).
