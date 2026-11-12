"""Alembic 0002 adds per-unit columns without rewriting rows that already exist."""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.seed import seed

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def test_upgrade_from_0001_preserves_rows(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'pre-unit.db'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "0001"], cwd=PROJECT_ROOT, env=env)
    db = sqlite3.connect(tmp_path / "pre-unit.db")
    now = datetime(2026, 1, 15, 12, 0, 0).isoformat(sep=" ")
    db.execute("INSERT INTO clinics (name, location) VALUES ('Kept Clinic', '1 Kept St')")
    db.execute("INSERT INTO patients (name, email) VALUES ('Row Keeper', 'row.keeper@patient.example')")
    db.execute(
        "INSERT INTO services (code, name, category, price_cents, includes, excludes, duration_min, keywords) "
        "VALUES ('MRI-KNEE', 'MRI knee without contrast', 'imaging', 45000, 'scan', 'dye', 45, 'mri knee')"
    )
    db.execute(
        "INSERT INTO staff_users (name, email, role) VALUES ('Jordan Kim', 'jordan.kim@clinic.example', 'staff')"
    )
    db.execute(
        "INSERT INTO slots (clinic_id, service_id, starts_at, status) VALUES (1, 1, ?, 'BOOKED')",
        (now,),
    )
    db.execute(
        "INSERT INTO appointments (patient_id, slot_id, service_id, status, quote_cents, small_actions, created_at) "
        "VALUES (1, 1, 1, 'CONFIRMED', 45000, '{}', ?)",
        (now,),
    )
    db.execute(
        "INSERT INTO payments (appointment_id, kind, intent, amount_cents, status, funding, request_id, "
        "paypal_capture_id, refunded_cents, created_at, updated_at) "
        "VALUES (1, 'FULL', 'CAPTURE', 45000, 'CAPTURED', 'paypal', 'keep-1', 'KEEPCAP', 0, ?, ?)",
        (now, now),
    )
    db.commit()
    before = {
        "clinics": db.execute("SELECT COUNT(*) FROM clinics").fetchone()[0],
        "patients": db.execute("SELECT name FROM patients").fetchone()[0],
        "price": db.execute("SELECT price_cents FROM services WHERE code='MRI-KNEE'").fetchone()[0],
        "quote": db.execute("SELECT quote_cents FROM appointments").fetchone()[0],
        "paid": db.execute("SELECT amount_cents FROM payments").fetchone()[0],
    }
    db.close()

    subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, env=env)
    subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, env=env)

    db = sqlite3.connect(tmp_path / "pre-unit.db")
    assert db.execute("SELECT COUNT(*) FROM clinics").fetchone()[0] == before["clinics"]
    assert db.execute("SELECT name FROM patients").fetchone()[0] == before["patients"]
    assert db.execute("SELECT price_cents, price_unit FROM services WHERE code='MRI-KNEE'").fetchone() == (
        before["price"],
        "visit",
    )
    assert db.execute("SELECT quote_cents, quantity, status FROM appointments").fetchone() == (
        before["quote"],
        1,
        "CONFIRMED",
    )
    assert db.execute("SELECT amount_cents, status FROM payments").fetchone() == (before["paid"], "CAPTURED")
    assert db.execute("SELECT name, role FROM staff_users").fetchone() == ("Jordan Kim", "staff")
    db.close()

    engine = create_engine(url)
    with Session(engine) as session:
        seed(session)
        seed(session)
    db = sqlite3.connect(tmp_path / "pre-unit.db")
    assert db.execute("SELECT name FROM patients ORDER BY id").fetchone()[0] == "Row Keeper"
    assert db.execute("SELECT price_cents FROM services WHERE code='MRI-KNEE'").fetchone()[0] == 45000
    assert db.execute("SELECT quote_cents FROM appointments WHERE patient_id=1").fetchone()[0] == 45000
    botox = db.execute("SELECT price_cents, price_unit FROM services WHERE code='MEDSPA-BOTOX'").fetchone()
    assert botox == (1300, "unit")
    assert db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    db.close()
