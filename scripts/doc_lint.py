"""Doc rules check (run by `make lint`). Fails on banned wording in the public-facing docs and UI."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "README.md",
    "docs/DEMO_GUIDE.md",
    "docs/DEPLOY_LIGHTSAIL.md",
    "docs/decisions.md",
    "docs/ai-tools.md",
    "docs/changelog.md",
    "docs/openai.md",
    *[str(p.relative_to(ROOT)) for p in (ROOT / "app/web/templates").glob("*.html")],
]
DATA_NOTE = "Data note: runs entirely on synthetic data; no real patient or clinic data is used."
RULES = [
    (re.compile(r"simulat", re.I), "do not use the word 'simulated'"),
    (re.compile(r"\bM\.?D\.?\b"), "use the correct degree abbreviation"),
    (re.compile(r"(?<!not )(?<!never )\bHIPAA[- ](certified|compliant)", re.I), "no HIPAA certification claim"),
    (re.compile(r"(?<!not )(?<!never )\bPCI[- ](certified|compliant)", re.I), "no PCI claim"),
    (re.compile(r"(?<!not )production[- ]validated", re.I), "no production-validated claim"),
]


def main() -> int:
    errors = []
    for f in FILES:
        p = ROOT / f
        if not p.exists():
            errors.append(f"{f}: missing")
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for rx, why in RULES:
                if rx.search(line):
                    errors.append(f"{f}:{i}: {why}: {line.strip()[:100]}")
    if (ROOT / "README.md").read_text(encoding="utf-8").count(DATA_NOTE) != 1:
        errors.append("README.md: must contain the exact data note line once")
    for e in errors:
        print(e)
    print("doc_lint: OK" if not errors else f"doc_lint: {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
