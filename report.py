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
                if None in row:
                    raise ValueError(f"{path}:{number}: too many CSV fields")
                missing = [name for name in required if row.get(name) is None]
                if missing:
                    raise ValueError(f"{path}:{number}: malformed row, missing fields {', '.join(missing)}")
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


def require_values(row, fields, label):
    missing = [field for field in fields if row.get(field) is None or not row[field].strip()]
    if missing:
        raise ValueError(f"{label}: empty required field(s): {', '.join(missing)}")


def date_value(raw, label, optional=False):
    if not raw and optional:
        return None
    try:
        parsed = date.fromisoformat(raw)
        if parsed.isoformat() != raw:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise ValueError(f"{label}: expected ISO date, got {raw!r}")


def allocate(total, works):
    """Pro-rata integer-cent allocation, deterministic largest-remainder method."""
    if not works or total == 0:
        return {w["work_id"]: 0 for w in works}
    weights = {w["work_id"]: w["price_cents"] for w in works}
    denominator = sum(weights.values())
    if not denominator:
        return {k: 0 for k in weights}
    result = {k: total * v // denominator for k, v in weights.items()}
    left = total - sum(result.values())
    order = sorted(weights, key=lambda k: (-(total * weights[k] % denominator), k))
    for key in order[:left]:
        result[key] += 1
    return result


def build(data_dir, as_of):
    contracts = read_csv(data_dir / "contracts.csv", ["contract_id", "contractor", "amount", "signed_at"])
    works_raw = read_csv(data_dir / "works.csv", ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"])
    payments = read_csv(data_dir / "payments.csv", ["payment_id", "contract_id", "work_id", "amount", "status", "paid_at", "refunded_at"])
    cs = {}
    for c in contracts:
        require_values(c, ["contract_id", "contractor", "amount", "signed_at"], "contract row")
        c["contract_id"] = c["contract_id"].strip()
        c["contractor"] = c["contractor"].strip()
        if not c["contract_id"] or c["contract_id"] in cs:
            raise ValueError(f"Duplicate/empty contract_id: {c['contract_id']!r}")
        if not c["contractor"]:
            raise ValueError(f"{c['contract_id']}: empty contractor")
        c["amount_cents"] = cents(c["amount"], c["contract_id"])
        date_value(c["signed_at"], c["contract_id"] + ".signed_at")
        cs[c["contract_id"]] = c
    ws = {}
    for w in works_raw:
        require_values(w, ["work_id", "contract_id", "title", "price", "status"], "work row")
        wid = w["work_id"].strip()
        w["contract_id"] = w["contract_id"].strip()
        w["work_id"] = wid
        if not wid or wid in ws:
            raise ValueError(f"Duplicate/empty work_id: {wid!r}")
        if w["contract_id"] not in cs:
            raise ValueError(f"{wid}: unknown contract {w['contract_id']!r}")
        if not w["title"].strip():
            raise ValueError(f"{wid}: empty title")
        w["price_cents"] = cents(w["price"], wid)
        if w["status"] not in {"planned", "done", "cancelled"}:
            raise ValueError(f"{wid}: unknown status {w['status']!r}")
        completed = date_value(w["completed_at"], wid + ".completed_at", True)
        cancelled = date_value(w["cancelled_at"], wid + ".cancelled_at", True)
        if w["status"] == "done" and completed is None:
            raise ValueError(f"{wid}: done work missing completed_at")
        if w["status"] == "cancelled" and cancelled is None:
            raise ValueError(f"{wid}: cancelled work missing cancelled_at")
        w["as_status"] = "cancelled" if cancelled and cancelled <= as_of else "done" if completed and completed <= as_of else "planned"
        w["paid_cents"] = 0
        ws[wid] = w
    contract_paid = {cid: 0 for cid in cs}
    direct_paid = {cid: 0 for cid in cs}
    seen_payments = set()
    validated_payments = []
    for p in payments:
        require_values(p, ["payment_id", "contract_id", "purpose", "amount", "status", "created_at"], "payment row")
        pid = p["payment_id"].strip()
        cid, wid = p["contract_id"].strip(), p["work_id"].strip()
        p["payment_id"], p["contract_id"], p["work_id"] = pid, cid, wid
        if not pid or pid in seen_payments:
            raise ValueError(f"Duplicate/empty payment_id: {pid!r}")
        seen_payments.add(pid)
        if cid not in cs:
            raise ValueError(f"{pid}: unknown contract {cid!r}")
        if wid and (wid not in ws or ws[wid]["contract_id"] != cid):
            raise ValueError(f"{pid}: invalid work_id {wid!r} for {cid}")
        amount = cents(p["amount"], p["payment_id"])
        if not p["purpose"].strip():
            raise ValueError(f"{pid}: empty purpose")
        if p["status"] not in {"paid", "queued", "refunded"}:
            raise ValueError(f"{pid}: unknown payment status {p['status']!r}")
        date_value(p["created_at"], pid + ".created_at")
        paid_at = date_value(p["paid_at"], pid + ".paid_at", p["status"] == "queued")
        refunded_at = date_value(p["refunded_at"], pid + ".refunded_at", p["status"] != "refunded")
        if p["status"] == "refunded" and refunded_at < paid_at:
            raise ValueError(f"{pid}: refunded_at precedes paid_at")
        validated_payments.append((p, amount, paid_at, refunded_at))

    for p, amount, paid_at, refunded_at in validated_payments:
        cid, wid = p["contract_id"], p["work_id"]
        if p["status"] == "queued" or not paid_at or paid_at > as_of:
            continue
        net = 0 if p["status"] == "refunded" and refunded_at and refunded_at <= as_of else amount
        if p["status"] == "refunded" and not refunded_at:
            raise ValueError(f"{p['payment_id']}: refunded payment missing refunded_at")
        contract_paid[cid] += net
        if wid:
            ws[wid]["paid_cents"] += net
            direct_paid[cid] += net
    for cid in cs:
        eligible = [w for w in ws.values() if w["contract_id"] == cid and w["as_status"] != "cancelled"]
        alloc = allocate(max(0, contract_paid[cid] - direct_paid[cid]), eligible)
        for w in eligible:
            w["paid_cents"] += alloc[w["work_id"]]
    work_rows, contract_rows = [], []
    for cid, c in cs.items():
        cworks = [w for w in ws.values() if w["contract_id"] == cid]
        active_value = sum(w["price_cents"] for w in cworks if w["as_status"] != "cancelled")
        canceled_value = sum(w["price_cents"] for w in cworks if w["as_status"] == "cancelled")
        for w in cworks:
            work_rows.append([cid, w["work_id"], w["title"], w["as_status"], money(w["price_cents"]), money(w["paid_cents"]), money(w["price_cents"]-w["paid_cents"])])
        contract_rows.append([cid, c["contractor"], money(c["amount_cents"]), money(active_value), money(canceled_value), money(contract_paid[cid]), money(c["amount_cents"]-contract_paid[cid]), money(active_value+canceled_value-c["amount_cents"]), money(sum(w["paid_cents"] for w in cworks)-contract_paid[cid])])
    return work_rows, contract_rows


def money(value):
    return f"{value // 100}.{value % 100:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--date", type=date.fromisoformat, required=True, help="Report date, YYYY-MM-DD, inclusive")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    try:
        work_rows, contract_rows = build(args.data_dir, args.date)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["section", "contract_id", "work_id", "contractor", "title", "status", "contract_amount", "work_price", "cancelled_works", "paid", "balance", "contract_vs_work_value_difference", "contract_vs_work_paid_difference"])
            writer.writerows([["work", r[0], r[1], "", r[2], r[3], "", r[4], "", r[5], r[6], "", ""] for r in work_rows])
            writer.writerows([["contract", r[0], "", r[1], "", "", r[2], r[3], r[4], r[5], r[6], r[7], r[8]] for r in contract_rows])
    except (ValueError, OSError) as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    main()
