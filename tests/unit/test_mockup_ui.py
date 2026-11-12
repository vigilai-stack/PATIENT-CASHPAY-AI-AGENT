"""The judge mockup (liquid glass, Prezi-style scenes) is the app's real UI, wired to the app's own routes."""

import re
from pathlib import Path

from app.models import Service
from app.pricing import money
from tests.conftest import staff_client

STATIC = Path(__file__).resolve().parents[2] / "app" / "web" / "static"


def test_home_is_the_mockup_deck_with_local_one_click_roles(env):
    page = env["client"].get("/").text
    for i in range(1, 9):
        assert f'<section class="scene" id="s{i}"' in page
    assert 'id="team"' in page and 'id="hero1"' in page and 'id="progdots"' in page
    assert '<script src="/static/mock.js"></script>' in page and "/static/mock.css" in page
    assert "cashpay.vigilaisolutions.com/demo/login" not in page  # personas log in on this server, same tab
    for who, bid in (("patient", "try-patient"), ("staff", "try-staff"), ("billing_lead", "try-lead")):
        assert re.search(rf'action="/demo/login/{who}">.*?id="{bid}"', page, re.S)
    assert "Refunds need a person" in page and "Human approval required" in page


def test_mockup_price_matches_the_clinic_price_list(env, db_session):
    mri = db_session.query(Service).filter_by(code="MRI-KNEE").one()
    page = env["client"].get("/").text
    assert money(mri.price_cents) == "$450.00"
    assert '<span class="price">$450</span>' in page


def test_every_page_has_role_tabs_badge_and_team_bar(env, patient):
    pages = {"": env["client"].get("/").text, "patient": patient.get("/patient").text}
    pages["staff"] = staff_client(env).get("/staff").text
    for active, text in pages.items():
        assert 'id="role-rail"' in text and 'id="hackathon-badge"' in text and 'id="team-bar"' in text
        for rid in ("rail-patient", "rail-staff", "rail-lead"):
            assert f'id="{rid}"' in text
        assert text.count('aria-current="page"') == (1 if active else 0)
    assert 'id="rail-patient" aria-current="page"' in pages["patient"]
    assert 'id="rail-staff" aria-current="page"' in pages["staff"]
    lead = env["client"]
    lead.post("/demo/login/billing_lead")
    assert 'id="rail-lead" aria-current="page"' in lead.get("/staff").text


def test_background_is_one_css_variable():
    css = {p.name: p.read_text() for p in STATIC.glob("*.css")}
    defs = [(n, m) for n, t in css.items() for m in re.findall(r"--bg:\s*([^;]+);", t)]
    assert defs == [("chrome.css", "#1E1033")]  # violet
    for n, t in css.items():
        assert "rgba(7,11,20" not in t, n


def test_refund_animation_respects_reduced_motion_and_stays_on_scene_1(patient):
    assert "prefers-reduced-motion: reduce" in (STATIC / "mock.js").read_text()
    chat = patient.get("/patient").text
    assert "Hero1.mount" not in chat and "hero1.js" not in chat  # too small to read inside the card


def test_ported_assets_use_static_paths():
    static = Path(__file__).resolve().parents[2] / "app" / "web" / "static"
    for name in ("mock.js", "mock.css"):
        assert "assets/" not in (static / name).read_text(encoding="utf-8")
