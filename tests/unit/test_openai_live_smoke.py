"""Local checks for the connectivity probe. No network and no API key."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "openai_live_smoke.py"


def _run(args, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    env["SMOKE_DENY_NETWORK"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_script_imports_stdlib_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= {"__future__", "json", "os", "re", "sys", "urllib"}
    text = SCRIPT.read_text(encoding="utf-8")
    assert "import app" not in text
    assert "sk-" not in text.replace("sk-[A-Za-z0-9_\\-]{8,}", "")


def test_self_check_refuses_to_invent_integration():
    result = _run(["--self-check"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "self_check"
    assert payload["integration_proved"] is False
    assert payload["live_call_succeeded"] is False
    assert payload["self_check_passed"] is True
    assert payload["model_only"]["connectivity_ok"] is False
    assert payload["model_only"]["grounded_price_cents"] is None
    assert payload["model_only"]["permission"] is None
    assert payload["with_usage"]["connectivity_ok"] is True
    assert payload["with_usage"]["integration_proved"] is False
    assert "sk-" not in result.stdout


def test_missing_key_makes_no_request():
    result = _run([])
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "missing_key"
    assert payload["live_call_attempted"] is False
    assert payload["integration_proved"] is False
    assert payload["request_count"] == 0
    assert "permission" not in payload
    assert "sk-" not in result.stdout + result.stderr


def test_whitespace_key_is_ignored():
    result = _run([], extra_env={"OPENAI_API_KEY": "   "})
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "missing_key"


def test_network_guard_blocks_a_present_key_shape_without_sending():
    result = _run([], extra_env={"OPENAI_API_KEY": "sk-test-placeholder"})
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "network_blocked"
    assert payload["integration_proved"] is False
    assert payload["request_count"] == 0
    assert "sk-test-placeholder" not in result.stdout
    assert "sk-" not in result.stdout
