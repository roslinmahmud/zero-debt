"""JSON / CSV export and JSON import."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import engine as eng
from app import models as m

ACCOUNT_FIELDS = (
    "bank", "account_ref", "limit_bdt", "day_basis", "posting_frequency", "posting_day",
    "posting_grace_days", "renewal_date", "currency", "settlement_fee_pct", "recon_threshold_bdt",
)
TXN_FIELDS = (
    "date", "type", "amount_bdt", "method", "note",
)


def _ser(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


def export_json(db: Session) -> dict:
    acc = db.scalars(select(m.Account)).first()
    if acc is None:
        return {"version": 1, "account": None}
    return {
        "version": 1,
        "account": {f: _ser(getattr(acc, f)) for f in ACCOUNT_FIELDS},
        "rate_changes": [
            {"effective_date": _ser(r.effective_date), "rate_pct": _ser(r.rate_pct)}
            for r in db.scalars(select(m.RateChange).order_by(m.RateChange.effective_date))
        ],
        "transactions": [
            {f: _ser(getattr(t, f)) for f in TXN_FIELDS}
            for t in db.scalars(select(m.Txn).order_by(m.Txn.date, m.Txn.id))
        ],
        "checkpoints": [
            {"date": _ser(c.date), "balance_bdt": _ser(c.balance_bdt), "note": c.note}
            for c in db.scalars(select(m.StatementCheckpoint).order_by(m.StatementCheckpoint.date))
        ],
    }


def export_txn_csv(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TXN_FIELDS)
    for t in db.scalars(select(m.Txn).order_by(m.Txn.date, m.Txn.id)):
        w.writerow([_ser(getattr(t, f)) if getattr(t, f) is not None else "" for f in TXN_FIELDS])
    return buf.getvalue()


def _d(v, *, optional=False) -> Decimal | None:
    if v in (None, "") and optional:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError(f"invalid number {v!r}") from exc


def _date(v, *, optional=False) -> date | None:
    if v in (None, "") and optional:
        return None
    return date.fromisoformat(str(v))


def import_json(db: Session, data: dict) -> None:
    """Replace all data with the contents of an export. Raises ValueError on bad input."""
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("account"), dict):
        raise ValueError("not a Loan Tracker export (version 1)")
    a = data["account"]
    acc = m.Account(
        bank=str(a["bank"])[:120],
        account_ref=str(a.get("account_ref") or "")[-4:],
        limit_bdt=_d(a["limit_bdt"]),
        day_basis=int(a["day_basis"]),
        posting_frequency=str(a["posting_frequency"]),
        posting_day=str(a["posting_day"]),
        posting_grace_days=int(a.get("posting_grace_days", 7)),
        renewal_date=_date(a.get("renewal_date"), optional=True),
        currency=str(a.get("currency") or "BDT")[:3],
        settlement_fee_pct=_d(a.get("settlement_fee_pct") or "0"),
        recon_threshold_bdt=_d(a.get("recon_threshold_bdt") or "10"),
    )
    eng.Config(acc.day_basis, acc.posting_frequency, acc.posting_day, acc.posting_grace_days)  # validates

    for model in (m.Txn, m.RateChange, m.StatementCheckpoint, m.Account):
        db.execute(delete(model))
    db.add(acc)
    db.flush()
    for r in data.get("rate_changes", []):
        db.add(m.RateChange(account_id=acc.id, effective_date=_date(r["effective_date"]), rate_pct=_d(r["rate_pct"])))
    for t in data.get("transactions", []):
        if t["type"] not in eng.TXN_TYPES:
            raise ValueError(f"unknown txn type {t['type']!r}")
        db.add(
            m.Txn(
                account_id=acc.id,
                date=_date(t["date"]),
                type=t["type"],
                amount_bdt=_d(t["amount_bdt"]),
                method=(str(t["method"])[:30] if t.get("method") else None),
                note=str(t.get("note") or ""),
            )
        )
    for c in data.get("checkpoints", []):
        db.add(
            m.StatementCheckpoint(
                account_id=acc.id, date=_date(c["date"]), balance_bdt=_d(c["balance_bdt"]), note=str(c.get("note") or "")
            )
        )
