"""Exercise the running service with synthetic data; no third-party accounts."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
key = os.getenv("API_KEY", "")
if not key and (ROOT / ".env").exists():
    key = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    ).get("API_KEY", "")
if not key:
    raise SystemExit("Run python3 scripts/setup.py first, or set API_KEY.")
BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")


def call(method, path, body=None, request_key=None, expected=200):
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    if request_key:
        headers["Idempotency-Key"] = request_key
    request = Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        result = json.load(response)
        if response.status != expected:
            raise RuntimeError(f"{method} {path}: expected {expected}, got {response.status}: {result}")
        return result


request_key = "demo-" + uuid4().hex
body = {"ledger_csv": (ROOT / "examples/ledger.csv").read_text(), "bank_csv": (ROOT / "examples/bank.csv").read_text()}
report = call("POST", "/v1/reconciliations", body, request_key, 201)
assert report["summary"] == {
    "matched": 2,
    "missing_in_bank": 1,
    "missing_in_ledger": 1,
    "amount_mismatch": 1,
    "currency_mismatch": 1,
    "duplicate": 1,
}
assert call("POST", "/v1/reconciliations", body, request_key)["id"] == report["id"]
assert call("GET", "/v1/reconciliations/" + report["id"]) == report
print(json.dumps(report["summary"], indent=2))
print("PASS: six classifications, durable report retrieval and idempotent replay.")
