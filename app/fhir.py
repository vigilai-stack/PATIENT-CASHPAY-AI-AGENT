"""Write one FHIR R4 Appointment per confirmed booking to a test FHIR server (stub or HAPI)."""

from __future__ import annotations

from datetime import timedelta

import httpx

from app.models import Appointment
from app.pricing import is_per_unit


def appointment_resource(appt: Appointment) -> dict:
    start = appt.slot.starts_at
    return {
        "resourceType": "Appointment",
        "status": "booked",
        "description": (
            f"{appt.service.name} ({appt.quantity} units)" if is_per_unit(appt.service) else appt.service.name
        ),
        "start": start.isoformat() + "Z",
        "end": (start + timedelta(minutes=appt.service.duration_min)).isoformat() + "Z",
        "serviceType": [{"text": appt.service.name}],
        "participant": [
            {"actor": {"display": appt.patient.name}, "status": "accepted"},
            {
                "actor": {"display": f"{appt.slot.clinic.name} - {appt.slot.clinic.location}"},
                "status": "accepted",
            },
        ],
        "identifier": [{"system": "urn:cashpay:appointment", "value": str(appt.id)}],
    }


def write_appointment(base_url: str, appt: Appointment, client: httpx.Client | None = None) -> str | None:
    if not base_url:
        return None
    c = client or httpx.Client(timeout=5.0)
    r = c.post(
        f"{base_url}/Appointment",
        json=appointment_resource(appt),
        headers={"Content-Type": "application/fhir+json"},
    )
    r.raise_for_status()
    return r.json().get("id")
