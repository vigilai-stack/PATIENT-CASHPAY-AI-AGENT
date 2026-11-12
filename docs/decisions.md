## **Decisions**

Data note: runs entirely on synthetic data; no real patient or clinic data is used.

The choices that shape this project, and why they were made. Each one names the code that implements it.

#### **1. The agent can talk about money, but never move it on its own**

**Decision:** the chat agent acts only through `Clinic` methods in `app/services.py`, and every one of those asks the guard (`app/guard.py`) first. Refunds and discounts only ever *create* a staff approval item; they are never executed by the agent.

**Why:** if the agent could be talked into a refund, it would be unsafe to put in front of money. The agent refuses, logs the attempt, and files a flagged request for a human. That behaviour is tested in `tests/failure/test_failures.py::test_injection_in_patient_message` and step 9 of `scripts/demo.py`.

**Trade-off:** a patient asking for a refund gets a queue, not an answer in the chat.

#### **2. Amounts come from the database, never from the patient or the model**

**Decision:** every dollar shown is checked against the database (`app/pricing.py::invalid_amounts`). A reply containing an amount that isn't in the allowed set is blocked and replaced with a request for the service name.

**Why:** price hallucination is the most direct way this kind of agent harms a patient or a clinic. Checking the reply after it is built is a second line of defence behind building it from database values in the first place.

#### **3. A rule-based engine underneath, the language model on top**

**Decision:** the agent has a built-in rule-based intent engine (`app/agent.py`) that needs no API key. A language model, when enabled, only replaces the step that works out *what the patient is asking*.

**Why:** the app runs and is fully testable with no API keys, and every automated run gets identical behaviour. The guard boundary is the same either way: the model produces *intents*, never amounts, approvals, or PayPal calls, so swapping the intent engine does not change the safety story.

**Trade-off:** the built-in engine is less fluent than a language model, which is why the live demo runs the OpenAI interpreter (`app/llm.py`, `LLM_MODE=openai`) on top of the same guard. Prices, quantities, refunds, and payments still stay in `Clinic` and the guard. See `docs/openai.md`.

#### **4. PayPal sandbox only; live URLs are refused at startup**

**Decision:** `app/config.py` raises `LiveEndpointRefused` unless `PAYPAL_BASE_URL` contains `sandbox`, and the sandbox adapter raises on a non-sandbox base URL too.

**Why:** a hackathon demo should fail loudly, not charge real money. This also makes the sandbox smoke test (`scripts/sandbox_smoke.py`) meaningful: it exercises the real PayPal REST APIs (Orders, Payments, Subscriptions, Invoicing, Webhooks) without any live risk.

#### **5. Deposits are authorized, not captured, until check-in**

**Decision:** a deposit creates a PayPal `AUTHORIZE` order; front desk captures it at check-in (`capture_deposit` in `app/services.py`, staff API in `app/main.py`).

**Why:** it matches how clinics actually work (hold the money, collect it when the patient arrives), and it keeps the agent from finalizing a charge for a visit that may not happen. The demo script walks this exact path (steps 3, 5, and 8).

#### **6. Idempotency and webhook handling**

**Decision:** every money-moving PayPal POST carries a `PayPal-Request-Id`; confirmations are idempotent on the server (`Idempotency-Key`); webhook deliveries are verified by signature, deduplicated, and parked for retry on timeout.

**Why:** payments fail in the middle: timeouts, duplicate webhooks, out-of-order events. Each of those has a test in `tests/failure/` (`test_duplicate_webhook`, `test_webhook_out_of_order`, `test_paypal_timeout_retry_returns_same_capture`, `test_duplicate_request`), and reconciliation (`Clinic.reconcile`) flags mismatches between PayPal records and the clinic's own payments.

#### **7. Staff approval is role-based, and enforced on the server**

**Decision:** refunds over $100 need the billing lead; smaller ones need front-desk staff. The check is in `Guard.check_execute` and `Clinic.decide`, not in the UI.

**Why:** a check only in the UI can be bypassed by calling the API directly. The demo script checks the server rule: front desk trying to approve a billing-lead refund gets a refusal (demo step 10), and the billing lead can then decide it (step 11).

#### **8. Hash-chained audit log**

**Decision:** `app/audit.py` records every agent action, guard decision, and money move in an append-only log where each row hashes the previous row. `verify_chain` recomputes the chain.

**Why:** for a clinic, "who approved what, and when" is the record that matters after the fact. A tamper-evident chain makes that record checkable by anyone with database access, and the tamper test (`test_audit_chain_detects_tamper`) proves a changed row is detected.

#### **9. FHIR for bookings, via a stub you can swap out**

**Decision:** one FHIR R4 `Appointment` is written per confirmed booking (`app/fhir.py`). In the demo it goes to a tiny stub in `fhir_stub/`; `FHIR_BASE_URL` points it at a real server (e.g. HAPI) with no code change.

**Why:** clinics already speak FHIR, so writing the booking in FHIR R4 means other clinic systems can read it without a custom format. The stub keeps the demo self-contained.

#### **10. Works with no keys**

**Decision:** with no PayPal keys, the app runs with an offline payment adapter (`PAYMENT_MODE=fake`, `FakePayPalAdapter`), and the checkout buttons stand in for the PayPal/Venmo popup while calling the same server code.

**Why:** anyone, including a reviewer without a PayPal developer account, can run the whole flow, including the guard, approvals, and audit chain, and the automated tests run without keys. Sandbox mode, used by the live demo, is the same code with the real PayPal adapter swapped in.

#### **11. One project, demo, and docs**

**Decision:** the source is organized as one project, with a `README.md`, this `docs/` folder, and a notebook walkthrough describing the same system. It is published with a live hosted demo.

**Why:** the demo, the code, and the docs should describe the same version. `scripts/doc_lint.py` checks that the public docs exist and follow the wording rules.

#### **12. Botox is priced per unit, and the quantity is never assumed**

**Decision:** Medspa Botox is $13.00 per unit in `app/seed.py`, with `price_unit` set to `unit` (migration `alembic/versions/0002_per_unit_service.py`). The patient must enter a whole number of units, from 1 to 200, authorized by a clinician or staff member. The agent (`app/agent.py`, `app/pricing.py::parse_unit_quantity`) refuses an ambiguous quantity instead of picking one, and 200 is a demo checkout ceiling, not a medical limit.

**Why:** a per-unit price quoted as a flat total would be wrong by the number of units, and an agent that guesses a quantity would be choosing a clinical amount. The $13.00 figure is an illustrative sandbox price for this demo, not any real clinic's pricing. It is not a treatment total.

**Trade-off:** Botox booking takes one extra step, entering the units, instead of a single price answer.

#### **What was deliberately not built**

- No live PayPal, no real patient data, no production authentication (demo logins are one-click entries for judging).
- No hard dependency on a model provider; without an API key, the app falls back to the rule-based engine (decision 3).
- No high-availability deployment; Lightsail is a single instance for the demo (see `docs/DEPLOY_LIGHTSAIL.md`).
