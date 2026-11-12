"""Tiny FHIR R4 test server (Appointment only) for the local demo. Swap for HAPI via FHIR_BASE_URL."""

from __future__ import annotations

import itertools

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FHIR test stub")
_store: dict[str, dict] = {}
_seq = itertools.count(1)


@app.get("/fhir/metadata")
def metadata():
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "fhirVersion": "4.0.1",
        "rest": [
            {
                "mode": "server",
                "resource": [{"type": "Appointment", "interaction": [{"code": "create"}, {"code": "read"}]}],
            }
        ],
    }


@app.post("/fhir/Appointment", status_code=201)
async def create(request: Request):
    body = await request.json()
    if body.get("resourceType") != "Appointment" or "status" not in body or "participant" not in body:
        raise HTTPException(400, "Invalid Appointment")
    rid = f"appt-{next(_seq)}"
    body["id"] = rid
    _store[rid] = body
    return JSONResponse(body, status_code=201, headers={"Location": f"/fhir/Appointment/{rid}"})


@app.get("/fhir/Appointment/{rid}")
def read(rid: str):
    if rid not in _store:
        raise HTTPException(404)
    return _store[rid]


@app.get("/fhir/Appointment")
def search():
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(_store),
        "entry": [{"resource": r} for r in _store.values()],
    }
