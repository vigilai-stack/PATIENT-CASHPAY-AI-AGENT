#!/usr/bin/env python3
"""Connectivity probe only. This file does not import the clinic app.

It only checks that the OpenAI endpoint answers and names a model. It does not
test prices or permissions; those are covered by the test suite.

Exit 0 is not a live result. Exit 4 means the endpoint answered and named a
model. Exit 2 means the key was missing. This probe is not the clinic app.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-6-luna"
TIMEOUT_SECONDS = 8
MAX_COMPLETION_TOKENS = 120
PATIENT_TEXT = "Synthetic patient, no real person: What is the cash price for a knee MRI without contrast?"
SECRET = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def redact(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "[redacted]")
    return SECRET.sub("[redacted]", text)


def scrub(value, key: str):
    if isinstance(value, str):
        return redact(value, key)
    if isinstance(value, dict):
        return {str(k): scrub(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    return value


def emit(payload: dict, key: str) -> None:
    text = json.dumps(scrub(payload, key), indent=2, sort_keys=True)
    if key and key in text:
        text = json.dumps({"error": "redaction_failed", "integration_proved": False}, indent=2, sort_keys=True)
    sys.stdout.write(text + "\n")


def request_body() -> dict:
    return {
        "model": MODEL,
        "store": False,
        "reasoning_effort": "none",
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "messages": [
            {"role": "system", "content": "Reply with one short JSON object."},
            {"role": "user", "content": PATIENT_TEXT},
        ],
    }


def judge(completion: dict | None, http_status: int | None) -> dict:
    """Record the HTTP result. Do not invent an application outcome."""
    usage = None
    response_model = None
    if isinstance(completion, dict):
        response_model = completion.get("model") or None
        raw_usage = completion.get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                k: raw_usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in raw_usage
            }
    connectivity_ok = http_status == 200 and bool(response_model) and bool(usage)
    return {
        "mode": "connectivity",
        "endpoint": ENDPOINT,
        "http_status": http_status,
        "response_model": response_model,
        "usage": usage,
        "connectivity_ok": connectivity_ok,
        "integration_proved": False,
        "live_call_succeeded": False,
        "grounded_price_cents": None,
        "permission": None,
        "error": None if connectivity_ok else "connectivity_not_established",
    }


def self_check() -> int:
    model_only = judge({"model": MODEL}, 200)
    with_usage = judge(
        {"model": MODEL, "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}},
        200,
    )
    ok = (
        model_only["connectivity_ok"] is False
        and model_only["integration_proved"] is False
        and model_only["grounded_price_cents"] is None
        and model_only["permission"] is None
        and with_usage["connectivity_ok"] is True
        and with_usage["integration_proved"] is False
        and with_usage["grounded_price_cents"] is None
        and with_usage["live_call_succeeded"] is False
    )
    emit(
        {
            "mode": "self_check",
            "self_check_passed": ok,
            "integration_proved": False,
            "live_call_attempted": False,
            "live_call_succeeded": False,
            "model_only": model_only,
            "with_usage": with_usage,
            "error": None if ok else "self_check_failed",
        },
        "",
    )
    return 0 if ok else 1


def live_call(key: str) -> int:
    if os.environ.get("SMOKE_DENY_NETWORK"):
        emit({"mode": "connectivity", "error": "network_blocked", "integration_proved": False, "request_count": 0}, key)
        return 1
    body = json.dumps(request_body()).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    completion = None
    status = None
    error = None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(20_000)
        error = "http_" + str(status)
    except Exception as exc:
        raw = b""
        error = type(exc).__name__
    if status == 200 and raw:
        try:
            completion = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            error = "malformed_body"
    judged = judge(completion if isinstance(completion, dict) else None, status)
    judged["live_call_attempted"] = True
    judged["request_count"] = 1
    judged["integration_proved"] = False
    judged["live_call_succeeded"] = False
    if error:
        judged["error"] = error
        judged["connectivity_ok"] = False
    emit(judged, key)
    return 4 if judged["connectivity_ok"] else 1


def main(argv: list[str]) -> int:
    if argv and argv not in (["--self-check"],):
        sys.stderr.write("usage: python3 openai_live_smoke.py [--self-check]\n")
        return 2
    if argv == ["--self-check"]:
        return self_check()
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key.strip():
        emit(
            {
                "mode": "connectivity",
                "error": "missing_key",
                "integration_proved": False,
                "live_call_attempted": False,
                "live_call_succeeded": False,
                "request_count": 0,
                "connectivity_ok": False,
            },
            "",
        )
        return 2
    return live_call(key.strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
