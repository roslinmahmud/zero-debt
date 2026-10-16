"""Pure CC loan calculation engine: no DB, no I/O, Decimal throughout."""

from __future__ import annotations

import bisect
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Sequence

ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
ONE_DAY = timedelta(days=1)

TXN_TYPES = (
    "deposit",
    "withdrawal",
    "profit_posted",
    "excise",
    "fee",
    "compensation",
    "other_charge",
)
CHARGE_TYPES = ("excise", "fee", "compensation", "other_charge")


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Config:
    day_basis: int = 365
    posting_frequency: str = "monthly"  # monthly | quarterly
    posting_day: str = "month_end"  # month_end | "1".."31"
    # A recorded profit_posted txn up to this many days after the period end belongs to that period.
    posting_grace_days: int = 7

    def __post_init__(self) -> None:
        if self.day_basis not in (360, 365):
            raise ValueError("day_basis must be 360 or 365")
        if self.posting_frequency not in ("monthly", "quarterly"):
            raise ValueError("posting_frequency must be monthly or quarterly")
        if self.posting_day != "month_end" and not 1 <= int(self.posting_day) <= 31:
            raise ValueError("posting_day must be 'month_end' or 1..31")
        if self.posting_grace_days < 0:
            raise ValueError("posting_grace_days must be >= 0")


@dataclass(frozen=True)
class Txn:
    date: date
    type: str
    amount: Decimal  # always positive; direction comes from type

    def __post_init__(self) -> None:
        if self.type not in TXN_TYPES:
            raise ValueError(f"unknown txn type {self.type!r}")
        if self.amount < 0:
            raise ValueError("amount must be positive")

    @property
    def signed(self) -> Decimal:
        return -self.amount if self.type == "deposit" else self.amount


@dataclass(frozen=True)
class RateChange:
    effective_date: date
    rate_pct: Decimal


@dataclass(frozen=True)
class Checkpoint:
    date: date
    balance: Decimal  # bank's end-of-day balance, including that day's txns


# ---------------------------------------------------------------- outputs


@dataclass(frozen=True)
class Posting:
    date: date
    period_start: date
    computed: Decimal
    actual: Decimal | None = None

    @property
    def amount(self) -> Decimal:
        return self.computed if self.actual is None else self.actual

    @property
    def estimated(self) -> bool:
        return self.actual is None

    @property
    def gap(self) -> Decimal | None:
        return None if self.actual is None else self.actual - self.computed


@dataclass(frozen=True)
class CheckpointDiff:
    date: date
    computed: Decimal
    statement: Decimal

    @property
    def gap(self) -> Decimal:
        return self.statement - self.computed


@dataclass(frozen=True)
class Day:
    date: date
    balance: Decimal  # signed; negative means credit
    accrued: Decimal
    rate_pct: Decimal

    @property
    def outstanding(self) -> Decimal:
        return max(self.balance, ZERO)


@dataclass
class Result:
    end: date  # last day processed
    balance: Decimal  # signed; negative means credit balance
    accrued: Decimal  # unposted, full precision
    rate_pct: Decimal
    postings: list[Posting] = field(default_factory=list)
    checkpoint_diffs: list[CheckpointDiff] = field(default_factory=list)
    credit_dates: list[date] = field(default_factory=list)
    days: list[Day] = field(default_factory=list)
    paid_off: date | None = None

    @property
    def outstanding(self) -> Decimal:
        return max(self.balance, ZERO)

    @property
    def credit(self) -> Decimal:
        return max(-self.balance, ZERO)


# ---------------------------------------------------------------- helpers


def is_posting_day(d: date, cfg: Config) -> bool:
    if cfg.posting_frequency == "quarterly" and d.month % 3:
        return False
    last = calendar.monthrange(d.year, d.month)[1]
    if cfg.posting_day == "month_end":
        return d.day == last
    return d.day == min(int(cfg.posting_day), last)


def next_posting_day(d: date, cfg: Config) -> date:
    """First posting day on or after d."""
    while not is_posting_day(d, cfg):
        d += ONE_DAY
    return d


def daily_cost(balance: Decimal, rate_pct: Decimal, day_basis: int) -> Decimal:
    return max(balance, ZERO) * rate_pct / HUNDRED / Decimal(day_basis)


class _Rates:
    def __init__(self, changes: Iterable[RateChange]):
        ordered = sorted(changes, key=lambda r: r.effective_date)
        if not ordered:
            raise ValueError("at least one rate is required")
        self.dates = [r.effective_date for r in ordered]
        self.rates = [r.rate_pct for r in ordered]

    def on(self, d: date) -> Decimal:
        i = bisect.bisect_right(self.dates, d) - 1
        return self.rates[max(i, 0)]


def _actual_postings(txns: Iterable[Txn], cfg: Config) -> dict[date, Decimal]:
    grace = timedelta(days=cfg.posting_grace_days)
    out: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for t in txns:
        if t.type == "profit_posted":
            out[next_posting_day(t.date - grace, cfg)] += t.amount
    return dict(out)


# ---------------------------------------------------------------- core loop


def run(
    *,
    start: date,
    end: date,
    opening_balance: Decimal,
    cfg: Config,
    rate_changes: Sequence[RateChange],
    txns: Sequence[Txn] = (),
    checkpoints: Sequence[Checkpoint] = (),
    opening_accrued: Decimal = ZERO,
    collect_days: bool = False,
    stop_when_paid: bool = False,
) -> Result:
    """Process each day in [start, end).

    opening_balance is the balance before txns dated `start`. A checkpoint on a day replaces
    the balance for that day (its txns are assumed to be already in the bank's figure).
    Recorded profit_posted txns replace the engine's computed posting for their period.
    """
    rates = _Rates(rate_changes)
    basis = Decimal(cfg.day_basis)
    by_day: dict[date, list[Txn]] = defaultdict(list)
    for t in txns:
        if start <= t.date < end:
            by_day[t.date].append(t)
    cps = {c.date: c.balance for c in checkpoints}
    actual = _actual_postings(txns, cfg)

    res = Result(end=start - ONE_DAY, balance=opening_balance, accrued=opening_accrued, rate_pct=rates.on(start))
    balance, accrued = opening_balance, opening_accrued
    period_start = start
    in_credit = balance < 0
    d = start
    while d < end:
        moved = sum((t.signed for t in by_day.get(d, ())), ZERO)
        if d in cps:
            if d != start:
                res.checkpoint_diffs.append(CheckpointDiff(d, balance + moved, cps[d]))
            balance = cps[d]
        else:
            balance += moved

        if balance < 0 and not in_credit:
            res.credit_dates.append(d)
        in_credit = balance < 0

        rate = rates.on(d)
        if balance > 0:
            accrued += balance * rate / HUNDRED / basis

        if is_posting_day(d, cfg):
            computed = q2(accrued)
            act = actual.get(d)
            if act is None and d not in cps:
                balance += computed
            res.postings.append(Posting(d, period_start, computed, act))
            accrued = ZERO
            period_start = d + ONE_DAY

        if collect_days:
            res.days.append(Day(d, balance, accrued, rate))
        res.end, res.rate_pct = d, rate

        if stop_when_paid and balance + q2(accrued) <= 0:
            res.paid_off = d
            break
        d += ONE_DAY

    res.balance, res.accrued = balance, accrued
    return res


# ---------------------------------------------------------------- settlement


@dataclass(frozen=True)
class Quote:
    date: date
    balance: Decimal
    accrued: Decimal
    fee: Decimal

    @property
    def total(self) -> Decimal:
        return self.balance + self.accrued + self.fee


def settlement_quote(state: Result, fee_pct: Decimal) -> Quote:
    """Amount to close the account at the end of state.end."""
    fee = q2(state.outstanding * fee_pct / HUNDRED)
    return Quote(state.end, state.balance, q2(state.accrued), fee)


# ---------------------------------------------------------------- simulation


@dataclass(frozen=True)
class Scenario:
    name: str
    monthly_bdt: Decimal
    deposit_day: int = 1
    lump_sums: tuple[tuple[date, Decimal], ...] = ()
    rate_changes: tuple[RateChange, ...] = ()


@dataclass
class SimResult:
    scenario: Scenario
    payoff_date: date | None
    months: Decimal | None
    total_profit: Decimal
    total_deposited: Decimal
    series: list[tuple[date, Decimal]]


def monthly_dates(start: date, end: date, day: int) -> list[date]:
    """Dates in [start, end) falling on `day` of each month (clamped to month end)."""
    out = []
    y, m = start.year, start.month
    while True:
        d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if d >= end:
            return out
        if d >= start:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def simulate(
    *,
    state: Result,
    cfg: Config,
    rate_changes: Sequence[RateChange],
    scenario: Scenario,
    future_txns: Sequence[Txn] = (),
    horizon_days: int = 365 * 40,
) -> SimResult:
    """Project forward from `state` (end of state.end) with synthetic deposits."""
    start = state.end + ONE_DAY
    end = start + timedelta(days=horizon_days)
    synthetic: list[Txn] = []
    if scenario.monthly_bdt > 0:
        synthetic += [Txn(d, "deposit", scenario.monthly_bdt) for d in monthly_dates(start, end, scenario.deposit_day)]
    synthetic += [Txn(d, "deposit", amt) for d, amt in scenario.lump_sums if start <= d < end and amt > 0]
    txns = [*future_txns, *synthetic]

    res = run(
        start=start,
        end=end,
        opening_balance=state.balance,
        opening_accrued=state.accrued,
        cfg=cfg,
        rate_changes=[*rate_changes, *scenario.rate_changes],
        txns=txns,
        collect_days=True,
        stop_when_paid=True,
    )
    total_profit = sum((p.amount for p in res.postings), ZERO) + q2(res.accrued)
    deposited = sum((t.amount for t in synthetic if t.date <= res.end), ZERO)
    if res.paid_off is not None:
        deposited += min(res.balance + q2(res.accrued), ZERO)  # drop the overshoot of the final deposit

    series = [(state.end, state.outstanding)]
    for day in res.days:
        if day.date.day == 1 or day.date == res.end:
            series.append((day.date, day.outstanding))

    months = None
    if res.paid_off is not None:
        months = (Decimal((res.paid_off - state.end).days) / Decimal("30.4375")).quantize(Decimal("0.1"))
    return SimResult(scenario, res.paid_off, months, total_profit, deposited, series)
