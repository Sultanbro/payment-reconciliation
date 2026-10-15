from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from reconcile.api import create_app
from reconcile.domain import InvalidCSV, parse_csv, reconcile

KEY = "test-only-" + "x" * 32
HEADER = "transaction_id,amount,currency\n"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "reports.db", KEY)) as client:
        client.headers["X-API-Key"] = KEY
        yield client


def test_business_classifications():
    ledger = HEADER + "same,0.10,USD\namount,5,USD\nmissing,1,EUR\nfx,1,EUR\ndup,1,USD\ndup,1,USD\nrefund,-4.20,USD\n"
    bank = HEADER + "same,0.1,USD\namount,5.01,USD\nextra,2,USD\nfx,1,USD\ndup,1,USD\nrefund,-4.2,USD\n"
    assert reconcile(ledger, bank)["summary"] == {
        "matched": 2,
        "amount_mismatch": 1,
        "missing_in_bank": 1,
        "missing_in_ledger": 1,
        "currency_mismatch": 1,
        "duplicate": 1,
    }


@pytest.mark.parametrize(
    "row",
    [
        "a,NaN,USD",
        "a,1.001,USD",
        "a,1e2,USD",
        "a,1,usd",
        "=formula,1,USD",
        "a,1",
        "a,1,USD,extra",
        "a,9999999999999,USD",
    ],
)
def test_reject_invalid_rows(row):
    with pytest.raises(InvalidCSV):
        parse_csv(HEADER + row)


def test_empty_and_duplicate_columns():
    for value in [HEADER, "transaction_id,amount,amount\na,1,1\n"]:
        with pytest.raises(InvalidCSV):
            parse_csv(value)


def test_row_limit_and_bom():
    assert parse_csv("\ufeff" + HEADER + "a,1,USD")["a"][0]["amount"] == "1.00"
    with pytest.raises(InvalidCSV):
        parse_csv(HEADER + "a,1,USD\n" * 5001)


def payload():
    return {"ledger_csv": HEADER + "abc,10,USD", "bank_csv": HEADER + "abc,10.00,USD"}


def test_create_replay_conflict_and_persistence(client, tmp_path):
    response = client.post("/v1/reconciliations", json=payload(), headers={"Idempotency-Key": "order-1"})
    assert response.status_code == 201
    report = response.json()
    replay = client.post("/v1/reconciliations", json=payload(), headers={"Idempotency-Key": "order-1"})
    assert replay.status_code == 200
    assert replay.json() == report
    changed = payload() | {"bank_csv": HEADER + "abc,11,USD"}
    assert client.post("/v1/reconciliations", json=changed, headers={"Idempotency-Key": "order-1"}).status_code == 409
    with TestClient(create_app(tmp_path / "reports.db", KEY)) as restarted:
        assert restarted.get(response.headers["Location"], headers={"X-API-Key": KEY}).json() == report


def test_auth_and_not_found(client):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/reconciliations/unknown").status_code == 404
    client.headers.pop("X-API-Key")
    assert client.post("/v1/reconciliations", json=payload()).status_code == 401
    assert client.get("/v1/reconciliations/unknown/export").status_code == 401


def test_required_key_validation_and_body_limit(client):
    assert client.post("/v1/reconciliations", json=payload()).status_code == 422
    assert client.post("/v1/reconciliations", content=b"x" * 512001).status_code == 413
    assert (
        client.post(
            "/v1/reconciliations", json=payload() | {"bank_csv": "bad"}, headers={"Idempotency-Key": "x"}
        ).status_code
        == 422
    )


def test_csv_export_escapes_formulas(client):
    value = HEADER + "-formula,10,USD\n"
    created = client.post(
        "/v1/reconciliations", json={"ledger_csv": value, "bank_csv": value}, headers={"Idempotency-Key": "export"}
    )
    response = client.get(created.headers["Location"] + "/export")
    assert response.status_code == 200
    assert "'-formula" in response.text
    assert response.headers["content-type"].startswith("text/csv")


def test_concurrent_replay_creates_one_report(client):
    def submit(_):
        return client.post("/v1/reconciliations", json=payload(), headers={"Idempotency-Key": "parallel"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(16)))
    assert sum(r.status_code == 201 for r in responses) == 1
    assert {r.status_code for r in responses} == {200, 201}
    assert len({r.json()["id"] for r in responses}) == 1


def test_missing_secret_fails_startup(tmp_path, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        create_app(tmp_path / "x.db")
