from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app import engine as e

START = date(2025, 1, 1)
RATE = [e.RateChange(date(2020, 1, 1), D("12"))]
CFG365 = e.Config(day_basis=365)
CFG360 = e.Config(day_basis=360)


def run(cfg=CFG365, days=30, txns=(), rates=RATE, **kw):
    return e.run(
        start=START,
        end=START + timedelta(days=days),
        opening_balance=D("500000"),
        cfg=cfg,
        rate_changes=rates,
        txns=txns,
        **kw,
    )


# §9 acceptance tests


def test_1_accrual_365():
    res = run()
    assert e.q2(res.accrued) == D("4931.51")
    assert res.postings == []


def test_2_accrual_360():
    assert e.q2(run(CFG360).accrued) == D("5000.00")


def test_3_deposit_timing():
    early = run(txns=[e.Txn(date(2025, 1, 10), "deposit", D("100000"))])
    late = run(txns=[e.Txn(date(2025, 1, 20), "deposit", D("100000"))])
    assert e.q2(late.accrued - early.accrued) == D("328.77")


def test_4_posted_profit_compounds():
    res = run(days=59)  # Jan + Feb 2025
    jan, feb = res.postings
    assert jan.date == date(2025, 1, 31)
    assert jan.computed == e.q2(D("500000") * D("0.12") * 31 / 365)
    assert feb.computed == e.q2((D("500000") + jan.computed) * D("0.12") * 28 / 365)
    assert res.balance == D("500000") + jan.computed + feb.computed


def test_5_rate_change_mid_period():
    rates = [*RATE, e.RateChange(date(2025, 1, 16), D("15"))]
    res = run(rates=rates)
    expected = D("500000") * (D("0.12") * 15 + D("0.15") * 15) / 365
    assert res.accrued == expected


def test_6_checkpoint_overrides_balance():
    txns = [e.Txn(date(2025, 1, 5), "withdrawal", D("50000"))]
    cps = [e.Checkpoint(date(2025, 1, 10), D("400000"))]
    res = run(txns=txns, checkpoints=cps, collect_days=True)
    by_date = {d.date: d for d in res.days}
    assert by_date[date(2025, 1, 9)].balance == D("550000")
    assert by_date[date(2025, 1, 10)].balance == D("400000")
    assert res.balance == D("400000")
    assert res.checkpoint_diffs[0].gap == D("-150000")


def test_7_reconciles_with_bank_posting():
    # The bank's figure, worked out independently from daily balances on the statement.
    deposits = {date(2025, 1, 8): D("30000"), date(2025, 1, 22): D("45000")}
    bal, bank_profit = D("500000"), D("0")
    for i in range(31):
        day = START + timedelta(days=i)
        bal -= deposits.get(day, D("0"))
        bank_profit += bal * D("0.12") / 365
    bank_posted = e.q2(bank_profit)

    txns = [e.Txn(d, "deposit", a) for d, a in deposits.items()]
    txns.append(e.Txn(date(2025, 2, 1), "profit_posted", bank_posted))  # bank posted a day late
    res = run(days=40, txns=txns)
    jan = res.postings[0]
    assert jan.actual == bank_posted
    assert abs(jan.gap) <= 1
    # Engine must not double count: only the bank's posting is in the balance.
    assert res.balance == D("500000") - D("75000") + bank_posted


def test_8_overpayment_becomes_credit():
    txns = [e.Txn(date(2025, 1, 5), "deposit", D("600000"))]
    res = run(txns=txns, collect_days=True)
    assert res.outstanding == 0
    assert res.credit == D("100000") - 0  # accrual stops while in credit
    assert res.credit_dates == [date(2025, 1, 5)]
    assert all(d.outstanding >= 0 for d in res.days)
    assert res.accrued == D("500000") * D("0.12") * 4 / 365


def test_9_bigger_deposit_pays_off_earlier():
    state = run(days=0)
    sims = [
        e.simulate(state=state, cfg=CFG365, rate_changes=RATE, scenario=e.Scenario(str(m), D(m)))
        for m in ("30000", "50000", "80000")
    ]
    dates = [s.payoff_date for s in sims]
    assert all(dates)
    assert dates[0] > dates[1] > dates[2]
    assert sims[0].total_profit > sims[1].total_profit > sims[2].total_profit


def test_10_settlement_quote():
    state = run(days=20)
    q = e.settlement_quote(state, D("0.5"))
    assert q.fee == D("2500.00")
    assert q.total == state.balance + e.q2(state.accrued) + q.fee


# extra coverage


def test_quarterly_posting_days():
    cfg = e.Config(posting_frequency="quarterly")
    assert e.next_posting_day(date(2025, 1, 15), cfg) == date(2025, 3, 31)
    cfg = e.Config(posting_day="31")
    assert e.next_posting_day(date(2025, 2, 1), cfg) == date(2025, 2, 28)


def test_simulation_never_pays_when_deposit_below_profit():
    state = run(days=0)
    sim = e.simulate(
        state=state, cfg=CFG365, rate_changes=RATE, scenario=e.Scenario("tiny", D("1000")), horizon_days=730
    )
    assert sim.payoff_date is None


def test_invalid_txn_type():
    with pytest.raises(ValueError):
        e.Txn(START, "bogus", D("1"))
