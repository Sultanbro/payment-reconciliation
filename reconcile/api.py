"""Single-tenant reconciliation API with durable, idempotent reports."""

import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from reconcile.domain import InvalidCSV, reconcile


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ledger_csv: str = Field(min_length=1, max_length=200_000)
    bank_csv: str = Field(min_length=1, max_length=200_000)


class Payment(BaseModel):
    amount: str
    currency: str


class Finding(BaseModel):
    transaction_id: str
    status: str
    ledger: list[Payment]
    bank: list[Payment]


class Report(BaseModel):
    id: str
    created_at: str
    summary: dict[str, int]
    payments: list[Finding]


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Buffer a bounded body before parsing JSON, including chunked requests.
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 512_000:
                return await JSONResponse({"detail": "Request exceeds 512 KB"}, 413)(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break

        async def buffered():
            nonlocal chunks
            if chunks is not None:
                body, chunks = b"".join(chunks), None
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, buffered, send)


def create_app(db_path=None, api_key=None):
    db_path = Path(db_path or os.getenv("DATABASE_PATH", "data/reports.db"))
    api_key = api_key or os.getenv("API_KEY", "")
    if len(api_key) < 32:
        raise RuntimeError("Set API_KEY to a random value of at least 32 characters")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def database():
        connection = sqlite3.connect(db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL)"
        )

    app = FastAPI(
        title="Payment Reconciliation",
        version="1.0.0",
        description="Compare a ledger against a bank export. Single-tenant API; authenticate using X-API-Key.",
    )
    app.add_middleware(BodyLimit)
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(key: str | None = Depends(key_header)):
        if not key or not hmac.compare_digest(key.encode(), api_key.encode()):
            raise HTTPException(401, "Invalid API key")

    @app.get("/health", tags=["Health"])
    def health():
        with database() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post(
        "/v1/reconciliations",
        status_code=201,
        response_model=Report,
        dependencies=[Depends(authorize)],
        tags=["Reports"],
    )
    def create(
        body: Input,
        response: Response,
        idempotency_key: str = Header(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"),
    ):
        digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        try:
            result = reconcile(body.ledger_csv, body.bank_csv)
        except (InvalidCSV, csv.Error) as error:
            raise HTTPException(422, str(error)) from error
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM reports WHERE request_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise HTTPException(409, "Idempotency key already used for different input")
                response.status_code = 200
                return {"id": existing["id"], "created_at": existing["created_at"], **json.loads(existing["result"])}
            identifier, created = str(uuid4()), datetime.now(UTC).isoformat()
            db.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?, ?)",
                (identifier, idempotency_key, digest, created, json.dumps(result)),
            )
        response.headers["Location"] = f"/v1/reconciliations/{identifier}"
        return {"id": identifier, "created_at": created, **result}

    def find(identifier):
        with database() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, "Report not found")
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["result"])}

    @app.get(
        "/v1/reconciliations/{identifier}", response_model=Report, dependencies=[Depends(authorize)], tags=["Reports"]
    )
    def report(identifier: str):
        return find(identifier)

    @app.get("/v1/reconciliations/{identifier}/export", dependencies=[Depends(authorize)], tags=["Reports"])
    def export(identifier: str):
        result = find(identifier)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["transaction_id", "status", "ledger", "bank"])
        for row in result["payments"]:
            # Prevent spreadsheet formulas, including IDs starting with '-'.
            safe_id = (
                "'" + row["transaction_id"]
                if row["transaction_id"].startswith(("=", "+", "-", "@"))
                else row["transaction_id"]
            )
            writer.writerow([safe_id, row["status"], json.dumps(row["ledger"]), json.dumps(row["bank"])])
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="reconciliation.csv"'},
        )

    return app
