import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.payments.fake import FakePayPalAdapter

# Model settings from the developer's own shell must not leak into tests.
# Tests that need them set them explicitly with monkeypatch.setenv.
_MODEL_ENV_VARS = (
    "LLM_MODE",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_TIMEOUT_SECONDS",
    "OPENAI_MAX_OUTPUT_TOKENS",
    "OPENAI_MAX_REQUESTS_PER_MINUTE",
    "OPENAI_MAX_CONCURRENCY",
    "CONVERSATION_PROMPT_CHARS",
)


@pytest.fixture(autouse=True)
def _isolate_model_env(monkeypatch):
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fhir_store():
    store = []

    def handler(request: httpx.Request):
        store.append(request.read())
        return httpx.Response(201, json={"id": f"appt-{len(store)}", "resourceType": "Appointment"})

    return store, httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def env(tmp_path, monkeypatch, fhir_store):
    db_url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("PAYMENT_MODE", "fake")
    monkeypatch.setenv("FHIR_BASE_URL", "http://fhir.test/fhir")
    settings = load_settings()
    adapter = FakePayPalAdapter(settings.fake_webhook_key)
    app = create_app(settings, adapter=adapter, fhir_client=fhir_store[1])
    if db_url.startswith("postgresql"):
        from app import db

        db.Base.metadata.drop_all(db.get_engine())
    with TestClient(app) as client:
        yield {"client": client, "adapter": adapter, "app": app, "fhir": fhir_store[0]}


@pytest.fixture
def patient(env):
    c = env["client"]
    assert c.post("/demo/login/patient", follow_redirects=False).status_code == 303
    return c


@pytest.fixture
def db_session(env):
    from app import db

    with db.SessionLocal() as s:
        yield s


def chat(client, text):
    r = client.post("/api/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def book_and_pay(client, kind="pay full", service="mri price"):
    j = chat(client, "book " + service)
    hold = next(b["send"] for b in j["buttons"] if b["send"].startswith("hold "))
    chat(client, hold)
    j = chat(client, kind)
    return int(j["pay_url"].rsplit("/", 1)[1]) if j.get("pay_url") else None


def staff_client(env, role="staff"):
    c = env["client"]
    c.post(f"/demo/login/{role}", follow_redirects=False)
    return c
