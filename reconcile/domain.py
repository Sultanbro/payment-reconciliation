"""Deterministic payment reconciliation; amounts never pass through float."""

import csv
import io
import re
from collections import defaultdict
from decimal import Decimal


class InvalidCSV(ValueError):
    pass


def parse_csv(value: str) -> dict:
    reader = csv.DictReader(io.StringIO(value.lstrip("\ufeff"), newline=""), strict=True)
    if reader.fieldnames != ["transaction_id", "amount", "currency"]:
        raise InvalidCSV("Expected columns: transaction_id,amount,currency")
    rows = defaultdict(list)
    for number, row in enumerate(reader, 2):
        if number > 5001:
            raise InvalidCSV("Maximum 5000 rows per file")
        if None in row or any(v is None for v in row.values()):
            raise InvalidCSV(f"Row {number}: incorrect column count")
        identifier, amount, currency = (row[k].strip() for k in reader.fieldnames)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            raise InvalidCSV(f"Row {number}: invalid transaction_id")
        if not re.fullmatch(r"-?\d{1,12}(\.\d{1,2})?", amount):
            raise InvalidCSV(f"Row {number}: amount must have at most two decimal places")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise InvalidCSV(f"Row {number}: currency must be three uppercase letters")
        rows[identifier].append({"amount": format(Decimal(amount), ".2f"), "currency": currency})
    if not rows:
        raise InvalidCSV("CSV must contain at least one payment")
    return rows


def reconcile(ledger_csv: str, bank_csv: str) -> dict:
    ledger, bank = parse_csv(ledger_csv), parse_csv(bank_csv)
    counts = dict.fromkeys(
        ["matched", "missing_in_bank", "missing_in_ledger", "amount_mismatch", "currency_mismatch", "duplicate"], 0
    )
    findings = []
    for identifier in sorted(ledger.keys() | bank.keys()):
        left, right = ledger.get(identifier, []), bank.get(identifier, [])
        if len(left) > 1 or len(right) > 1:
            status = "duplicate"
        elif not left:
            status = "missing_in_ledger"
        elif not right:
            status = "missing_in_bank"
        elif left[0]["currency"] != right[0]["currency"]:
            status = "currency_mismatch"
        elif Decimal(left[0]["amount"]) != Decimal(right[0]["amount"]):
            status = "amount_mismatch"
        else:
            status = "matched"
        counts[status] += 1
        findings.append({"transaction_id": identifier, "status": status, "ledger": left, "bank": right})
    return {"summary": counts, "payments": findings}
