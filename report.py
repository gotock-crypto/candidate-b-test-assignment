#!/usr/bin/env python3
"""Build a point-in-time contract/work payment report using exact cents."""
import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


def read_csv(path, required):
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or not set(required) <= set(reader.fieldnames):
                raise ValueError(f"{path}: missing columns; expected {', '.join(required)}")
            rows = list(reader)
            for number, row in enumerate(rows, start=2):
                missing = [key for key in required if key not in row or row[key] is None]
                if missing:
                    raise ValueError(f"{path}:{number}: missing fields {', '.join(missing)}")
            return rows
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def cents(raw, label):
    try:
        value = Decimal(raw)
        if not value.is_finite() or value < 0 or value * 100 != (value * 100).to_integral_value():
            raise ValueError
        return int(value * 100)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label}: expected non-negative amount with at most 2 decimals, got {raw!r}")


def date_value(raw, label, optional=False):
    if not raw and optional:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        raise ValueError(f"{label}: expected ISO date, got {raw!r}")


def allocate(total, works):
    """Pro-rata integer-cent allocation using deterministic largest remainder."""
    if not works or total == 0:
        return {w["work_id"]: 0 for w in works}
    weights = {w["work_id"]: w["price_cents"] for w in works}
    denominator = sum(weights.values())
    if not denominator:
        return {key: 0 for key in weights}
    result = {key: total * value // denominator for key, value in weights.items()}
    left = total - sum(result.values())
    order = sorted(weights, key=lambda key: (-(total * weights[key] % denominator), key))
    for key in order[:left]:
        result[key] += 1
    return result


def build(data_dir, as_of):
    contracts = read_csv(data_dir / "contracts.csv", ["contract_id", "contractor", "amount", "signed_at"])
    works_raw = read_csv(data_dir / "works.csv", ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"])
    payments = read_csv(data_dir / "payments.csv", ["payment_id", "contract_id", "work_id", "purpose", "amount", "status", "created_at", "paid_at", "refunded_at"])

    cs = {}
    for contract in contracts:
        cid = contract["contract_id"].strip()
        if not cid or cid in cs:
            raise ValueError(f"Duplicate/empty contract_id: {cid!r}")
        contract["contract_id"] = cid
        contract["contractor"] = contract["contractor"].strip()
        if not contract["contractor"]:
            raise ValueError(f"{cid}: empty contractor")
        contract["amount_cents"] = cents(contract["amount"], cid)
        date_value(contract["signed_at"], cid + ".signed_at")
        cs[cid] = contract

    ws = {}
    for work in works_raw:
        wid = work["work_id"].strip()
        cid = work["contract_id"].strip()
        if not wid or wid in ws:
            raise ValueError(f"Duplicate/empty work_id: {wid!r}")
        if cid not in cs:
            raise ValueError(f"{wid}: unknown contract {cid!r}")
        if not work["title"].strip():
            raise ValueError(f"{wid}: empty title")
        work["work_id"], work["contract_id"] = wid, cid
        work["price_cents"] = cents(work["price"], wid)
        status = work["status"].strip()
        if status not in {"planned", "done", "cancelled"}:
            raise ValueError(f"{wid}: unknown status {status!r}")
        completed = date_value(work["completed_at"].strip(), wid + ".completed_at", True)
        cancelled = date_value(work["cancelled_at"].strip(), wid + ".cancelled_at", True)
        if status == "done" and completed is None:
            raise ValueError(f"{wid}: done work missing completed_at")
        if status == "cancelled" and cancelled is None:
            raise ValueError(f"{wid}: cancelled work missing cancelled_at")
        if completed and cancelled and completed > cancelled:
            raise ValueError(f"{wid}: completed_at is after cancelled_at")
        work["as_status"] = (
            "cancelled" if cancelled and cancelled <= as_of
            else "done" if completed and completed <= as_of
            else "planned"
        )
        work["paid_cents"] = 0
        ws[wid] = work

    contract_paid = {cid: 0 for cid in cs}
    direct_paid = {cid: 0 for cid in cs}
    seen_payments = set()
    for payment in payments:
        pid = payment["payment_id"].strip()
        cid = payment["contract_id"].strip()
        wid = payment["work_id"].strip()
        if not pid or pid in seen_payments:
            raise ValueError(f"Duplicate/empty payment_id: {pid!r}")
        seen_payments.add(pid)
        if cid not in cs:
            raise ValueError(f"{pid}: unknown contract {cid!r}")
        amount = cents(payment["amount"], pid)
        status = payment["status"].strip()
        if status not in {"paid", "queued", "refunded"}:
            raise ValueError(f"{pid}: unknown payment status {status!r}")
        date_value(payment["created_at"].strip(), pid + ".created_at")
        paid_at = date_value(payment["paid_at"].strip(), pid + ".paid_at", True)
        refunded_at = date_value(payment["refunded_at"].strip(), pid + ".refunded_at", True)
        if status == "paid" and paid_at is None:
            raise ValueError(f"{pid}: paid payment missing paid_at")
        if status == "refunded" and (paid_at is None or refunded_at is None):
            raise ValueError(f"{pid}: refunded payment requires paid_at and refunded_at")
        if paid_at and refunded_at and refunded_at < paid_at:
            raise ValueError(f"{pid}: refunded_at precedes paid_at")
        if wid and (wid not in ws or ws[wid]["contract_id"] != cid):
            raise ValueError(f"{pid}: invalid work_id {wid!r} for contract {cid!r}")
        # Structural validation above intentionally occurs before financial filtering.
        if status == "queued" or paid_at is None or paid_at > as_of:
            continue
        net = 0 if status == "refunded" and refunded_at <= as_of else amount
        contract_paid[cid] += net
        if wid:
            ws[wid]["paid_cents"] += net
            direct_paid[cid] += net

    for cid in cs:
        eligible = [
            work for work in ws.values()
            if work["contract_id"] == cid and work["as_status"] != "cancelled"
        ]
        allocation = allocate(max(0, contract_paid[cid] - direct_paid[cid]), eligible)
        for work in eligible:
            work["paid_cents"] += allocation[work["work_id"]]

    work_rows, contract_rows = [], []
    for cid, contract in cs.items():
        contract_works = [work for work in ws.values() if work["contract_id"] == cid]
        active_value = sum(work["price_cents"] for work in contract_works if work["as_status"] != "cancelled")
        cancelled_value = sum(work["price_cents"] for work in contract_works if work["as_status"] == "cancelled")
        for work in contract_works:
            work_rows.append([
                cid, work["work_id"], work["title"], work["as_status"],
                money(work["price_cents"]), money(work["paid_cents"]),
                money(work["price_cents"] - work["paid_cents"]),
            ])
        contract_rows.append([
            cid, contract["contractor"], money(contract["amount_cents"]),
            money(active_value + cancelled_value), money(cancelled_value),
            money(contract_paid[cid]), money(contract["amount_cents"] - contract_paid[cid]),
            money(active_value + cancelled_value - contract["amount_cents"]),
            money(sum(work["paid_cents"] for work in contract_works) - contract_paid[cid]),
        ])
    return work_rows, contract_rows


def money(value):
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--date", type=date.fromisoformat, required=True, help="Inclusive report date, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        work_rows, contract_rows = build(args.data_dir, args.date)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([
                "section", "contract_id", "work_id", "contractor", "title", "status",
                "contract_amount", "work_price", "cancelled_works", "paid", "balance",
                "contract_vs_work_value_difference", "contract_vs_work_paid_difference",
            ])
            writer.writerows([
                ["work", row[0], row[1], "", row[2], row[3], "", row[4], "", row[5], row[6], "", ""]
                for row in work_rows
            ])
            writer.writerows([
                ["contract", row[0], "", row[1], "", "", row[2], row[3], row[4], row[5], row[6], row[7], row[8]]
                for row in contract_rows
            ])
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
