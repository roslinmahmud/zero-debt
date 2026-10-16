"""Glue between the DB and the pure engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import engine as eng
from app import models as m

ZERO = Decimal("0")


@dataclass
class Ledger:
    account: m.Account
    cfg: eng.Config
    rates: list[eng.RateChange]
    txns: list[eng.Txn]
    checkpoints: list[eng.Checkpoint]
    db_txns: list[m.Txn]

    @property
    def ready(self) -> bool:
        return bool(self.rates and self.checkpoints)


def get_account(db: Session) -> m.Account | None:
    return db.scalars(select(m.Account).order_by(m.Account.id)).first()


def config_for(acc: m.Account) -> eng.Config:
    return eng.Config(
        day_basis=acc.day_basis,
        posting_frequency=acc.posting_frequency,
        posting_day=acc.posting_day,
        posting_grace_days=acc.posting_grace_days,
    )


def load(db: Session, acc: m.Account) -> Ledger:
    rates = db.scalars(select(m.RateChange).where(m.RateChange.account_id == acc.id)).all()
    txns = db.scalars(select(m.Txn).where(m.Txn.account_id == acc.id).order_by(m.Txn.date, m.Txn.id)).all()
    cps = db.scalars(
        select(m.StatementCheckpoint)
        .where(m.StatementCheckpoint.account_id == acc.id)
        .order_by(m.StatementCheckpoint.date)
    ).all()
    return Ledger(
        account=acc,
        cfg=config_for(acc),
        rates=[eng.RateChange(r.effective_date, r.rate_pct) for r in rates],
        txns=[eng.Txn(t.date, t.type, t.amount_bdt) for t in txns],
        checkpoints=[eng.Checkpoint(c.date, c.balance_bdt) for c in cps],
        db_txns=list(txns),
    )


def history(lg: Ledger, as_of: date, collect_days: bool = False) -> eng.Result:
    """Engine state at the end of `as_of`, starting from the first checkpoint."""
    first = lg.checkpoints[0]
    return eng.run(
        start=first.date,
        end=max(as_of, first.date - timedelta(days=1)) + timedelta(days=1),
        opening_balance=first.balance,
        cfg=lg.cfg,
        rate_changes=lg.rates,
        txns=lg.txns,
        checkpoints=lg.checkpoints,
        collect_days=collect_days,
    )


def fx_rate(lg: Ledger) -> Decimal | None:
    if lg.account.fx_eur_bdt:
        return lg.account.fx_eur_bdt
    with_fx = [t for t in lg.db_txns if t.fx_rate]
    return with_fx[-1].fx_rate if with_fx else None


def dashboard(lg: Ledger, today: date) -> dict:
    acc = lg.account
    res = history(lg, today)
    next_post = eng.next_posting_day(today + timedelta(days=1), lg.cfg)
    ahead = eng.run(
        start=today + timedelta(days=1),
        end=next_post + timedelta(days=1),
        opening_balance=res.balance,
        opening_accrued=res.accrued,
        cfg=lg.cfg,
        rate_changes=lg.rates,
        txns=[t for t in lg.txns if t.date > today],
    )
    est_posting = ahead.postings[-1].amount if ahead.postings else ZERO
    fx = fx_rate(lg)

    past = [t for t in lg.db_txns if t.date <= today]
    deposited = sum((t.amount_bdt for t in past if t.type == "deposit"), ZERO)
    charges = sum((t.amount_bdt for t in past if t.type in eng.CHARGE_TYPES), ZERO)
    eur_sent = sum(((t.eur_sent or ZERO) + (t.transfer_fee_eur or ZERO) for t in past if t.type == "deposit"), ZERO)
    profit_paid = sum((p.amount for p in res.postings), ZERO)

    last_gap = None
    reconciled = [p for p in res.postings if p.actual is not None]
    if reconciled:
        last_gap = reconciled[-1]

    return {
        "today": today,
        "outstanding": res.outstanding,
        "credit": res.credit,
        "outstanding_eur": (res.outstanding / fx) if fx else None,
        "fx": fx,
        "accrued": eng.q2(res.accrued),
        "rate_pct": res.rate_pct,
        "next_posting": next_post,
        "est_posting": est_posting,
        "daily_cost": eng.q2(eng.daily_cost(res.balance, res.rate_pct, acc.day_basis)),
        "monthly_cost": eng.q2(eng.daily_cost(res.balance, res.rate_pct, acc.day_basis) * 30),
        "utilization": (res.outstanding / acc.limit_bdt * 100) if acc.limit_bdt else None,
        "days_to_renewal": (acc.renewal_date - today).days if acc.renewal_date else None,
        "deposited": deposited,
        "charges": charges,
        "profit_paid": profit_paid,
        "eur_sent": eur_sent,
        "last_gap": last_gap,
        "threshold": acc.recon_threshold_bdt,
        "settlement": eng.settlement_quote(res, acc.settlement_fee_pct),
    }


def reconciliation(lg: Ledger, today: date) -> dict:
    res = history(lg, today)
    return {"postings": list(reversed(res.postings)), "diffs": list(reversed(res.checkpoint_diffs))}


def avg_monthly_deposit(lg: Ledger, today: date, days: int = 90) -> Decimal:
    since = today - timedelta(days=days)
    total = sum((t.amount for t in lg.txns if t.type == "deposit" and since < t.date <= today), ZERO)
    return eng.q2(total * 30 / days)


def chart_data(lg: Ledger, today: date) -> dict:
    res = history(lg, today, collect_days=True)
    actual = [{"x": d.date.isoformat(), "y": float(d.outstanding)} for d in res.days]

    monthly = avg_monthly_deposit(lg, today)
    sim = eng.simulate(
        state=res,
        cfg=lg.cfg,
        rate_changes=lg.rates,
        scenario=eng.Scenario("trend", monthly),
        future_txns=[t for t in lg.txns if t.date > today],
        horizon_days=365 * 10,
    )
    projected = [{"x": d.isoformat(), "y": float(v)} for d, v in sim.series]

    cumulative, running = [], ZERO
    for p in res.postings:
        running += p.amount
        cumulative.append({"x": p.date.isoformat(), "y": float(running)})

    eur_cost = []
    for t in lg.db_txns:
        if t.type == "deposit" and t.eur_sent:
            total_eur = t.eur_sent + (t.transfer_fee_eur or ZERO)
            eur_cost.append(
                {
                    "x": t.date.isoformat(),
                    "eur": float(total_eur),
                    "effective": float(eng.q2(t.amount_bdt / total_eur)),
                }
            )

    return {
        "outstanding": {"actual": actual, "projected": projected, "monthly": float(monthly)},
        "postings": [
            {"x": p.date.isoformat(), "y": float(p.amount), "estimated": p.estimated} for p in res.postings
        ],
        "cumulative": cumulative,
        "eur_cost": eur_cost,
    }


def run_scenarios(lg: Ledger, today: date, scenarios: list[eng.Scenario]) -> list[eng.SimResult]:
    res = history(lg, today)
    future = [t for t in lg.txns if t.date > today]
    return [
        eng.simulate(state=res, cfg=lg.cfg, rate_changes=lg.rates, scenario=s, future_txns=future)
        for s in scenarios
    ]


def timing_insight(lg: Ledger, today: date, scenario: eng.Scenario) -> dict | None:
    if scenario.monthly_bdt <= 0:
        return None
    early, late = run_scenarios(
        lg,
        today,
        [
            eng.Scenario("day 1", scenario.monthly_bdt, 1, scenario.lump_sums, scenario.rate_changes),
            eng.Scenario("day 25", scenario.monthly_bdt, 25, scenario.lump_sums, scenario.rate_changes),
        ],
    )
    return {"early": early, "late": late, "saving": late.total_profit - early.total_profit}


def settle_on(lg: Ledger, on: date) -> eng.Quote:
    return eng.settlement_quote(history(lg, on), lg.account.settlement_fee_pct)
