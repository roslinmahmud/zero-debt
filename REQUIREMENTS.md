# CC Loan Tracker: Requirements

A small self-hosted web app to track, calculate and visualize a **Cash Credit (CC)** loan at a Bangladeshi bank (Islami Bank Bangladesh PLC). The loan is repaid from Finland in EUR, with no fixed EMI.

> **Status:** Draft. The bank's rules (rate, day basis, posting frequency, charges) are **assumptions until they're confirmed from a real statement**. See §10.

---

## 1. Goal

Answer four questions at any moment:

1. **How much is owed right now?** (BDT, plus EUR equivalent)
2. **How much is it costing per day / month?**
3. **If I send X per month, when is it paid off, and how much profit will I still pay?**
4. **How much do I need to settle it fully on date Y?**

## 2. Domain: how a CC account works

- A **limit** (e.g. Tk 10 lakh); the outstanding can be anything up to it.
- No fixed EMI. Deposits reduce the outstanding; withdrawals and charges increase it.
- **Profit/rent accrues daily**: `outstanding × rate / day_basis` (360 or 365).
- Accrued profit is **posted as one debit** at month-end or quarter-end. Once posted, it becomes part of the outstanding balance, so it compounds.
- A deposit reduces the outstanding **from its value date**, so it cuts future profit straight away.
- **Annual renewal/expiry**: a renewal fee may apply, and the rate may change.
- **Excise duty**: debited yearly, using slabs based on the balance (slabs are set in the national budget).
- **Islamic terminology:** IBBL calls interest "profit" or "rent". Late or overdue amounts may attract **"compensation"** instead of penal interest. The math is treated the same.

## 3. MVP features

### 3.1 Account setup
| Field | Type | Notes |
|---|---|---|
| Bank | text | e.g. Islami Bank Bangladesh PLC |
| Account ref | text | Last 4 digits only |
| Limit | decimal BDT | |
| Profit rate | decimal % p.a. | Changes are recorded in `rate_change` |
| Day basis | 360 \| 365 | Confirm from the statement |
| Posting frequency | monthly \| quarterly | Confirm from the statement |
| Posting day | `month_end` or a specific day | |
| Renewal date | date | |
| Opening checkpoint | date + balance | Taken from the latest statement |

### 3.2 Transactions
- Fields: date (**value date credited to the loan**), type, amount BDT, note.
- Types: `deposit`, `withdrawal`, `profit_posted`, `excise`, `fee`, `compensation`, `other_charge`.
- Remittance metadata for deposits (optional): EUR sent, FX rate, transfer fee (EUR), method (Wise / Remitly / bank / bKash / family member), proof upload.
- Full create, read, update and delete, so mistakes can be fixed.
- **CSV import** from the statement (phase 1.5; manual entry is fine for the MVP).

### 3.3 Statement checkpoints / reconciliation
- Enter the bank's official balance on a date.
- The engine **resets to the checkpoint**, so later calculations start from the bank's figure.
- Show the **gap** between the app's calculated profit and the profit the bank posted for each period.
- Flag a gap over Tk 10 (configurable). It usually means the rate, day basis or posting date is wrong.

### 3.4 Dashboard
- Current outstanding (BDT, plus EUR at the stored or latest FX rate)
- Profit accrued since the last posting (unposted)
- Next posting date and estimated posting amount
- **Daily cost**: `outstanding × rate / day_basis`
- Limit utilization %
- Days until renewal
- Lifetime totals: deposited, profit paid, charges paid (BDT, plus EUR actually sent)
- Last reconciliation gap

### 3.5 Charts
1. **Outstanding over time** (actual, plus a projected dashed line)
2. **Profit posted per period** (bars)
3. **Cumulative profit paid** (line)
4. **EUR cost per deposit** (shows good and bad FX months)

### 3.6 What-if simulator (does not change real data)
- Inputs: monthly deposit (BDT or EUR with an FX assumption), deposit day of month, optional lump sums (date + amount), optional rate change.
- Output: payoff date, months to payoff, total future profit, and a projected balance line.
- **Compare 2–3 scenarios side by side** (e.g. Tk 30k / 50k / 80k per month).
- **Settle on date Y**: outstanding + accrued profit up to Y + settlement fee (if any).
- **Timing insight**: savings from depositing on day 1 vs day 25 of the month.

### 3.7 Reminders (optional for the MVP)
- Telegram reminder before the posting date: "Send money now to reduce this period's profit."
- Renewal date reminder 30 days ahead.

## 4. Calculation engine

A pure module with no DB or I/O, fully unit-tested, using `Decimal` everywhere.

```
state = last checkpoint (date, balance), accrued = 0

for day in range(start_date, target_date):
    balance += sum(transactions on day)          # value-dated
    rate     = effective rate on day (rate_change)
    accrued += balance * rate / day_basis        # keep full precision
    if is_posting_day(day):
        posted   = round(accrued, 2)
        balance += posted
        accrued  = 0
        emit(posting event)
```

Rules:
- Precision: `Decimal`, rounded to 2 decimal places (poisha) **only when posted**.
- If the balance would go negative, cap it at 0 and record the surplus as a credit balance (flag it).
- Payment order: by default a deposit reduces the outstanding balance directly, because posted profit is already in the balance. Make this configurable in case the bank applies deposits to overdue compensation first.
- Simulation uses the same loop with synthetic future transactions.
- Payoff = the first date the projected balance is ≤ 0.

## 5. Data model

```sql
account(
  id, bank, account_ref, limit_bdt, day_basis,           -- 360|365
  posting_frequency,                                      -- monthly|quarterly
  posting_day, renewal_date, currency DEFAULT 'BDT', created_at
)

rate_change(id, account_id, effective_date, rate_pct)

txn(
  id, account_id, date, type, amount_bdt,
  eur_sent NULL, fx_rate NULL, transfer_fee_eur NULL,
  method NULL, proof_path NULL, note, created_at
)

statement_checkpoint(id, account_id, date, balance_bdt, note)
```

Store **inputs only**. Balances, accruals and projections are always computed, never stored.

## 6. Non-functional

- Single user and self-hosted. Protect it with basic auth or put it behind Tailscale.
- SQLite. One file, easy to back up.
- Mobile-friendly.
- Export all data as JSON or CSV. Import from JSON.
- Nothing sensitive in the repo: no account numbers, names or NID.

## 7. Suggested stack

- **Backend:** FastAPI + SQLAlchemy + SQLite (`uv` for packages)
- **Frontend:** server-rendered pages + HTMX (or plain JS) + Chart.js
- **Engine:** `engine.py`, pure functions, tested with `pytest`

## 8. Build order

1. Engine + tests (§9)
2. Account setup + checkpoint
3. Transactions (manual entry)
4. Dashboard
5. Reconciliation view
6. Charts
7. What-if simulator
8. CSV import, reminders

## 9. Acceptance tests

1. Tk 500,000 at 12%, 365 basis, 30 days, no transactions → accrued ≈ **Tk 4,931.51**.
2. Same, with a 360 basis → ≈ **Tk 5,000.00**.
3. Depositing Tk 100,000 on day 10 instead of day 20 reduces profit by ≈ **Tk 328.77** (10 × 100,000 × 12% / 365).
4. Profit posted at a period end accrues profit itself in the next period (compounding).
5. A rate change mid-period splits the accrual correctly at the effective date.
6. A checkpoint overrides the computed balance from its date onward.
7. Recalculating a past period from statement data matches the bank's posted profit **within Tk 1**.
8. A deposit larger than the outstanding → balance 0, credit flagged, no negative balance.
9. The simulator's payoff date moves earlier when the monthly deposit increases.
10. The settlement quote on date Y = balance + accrued up to Y + fee.

## 10. Open questions (confirm from the statement or the bank)

- [ ] Product / investment mode (CC under which Islamic mode: HPSM, Bai-Muajjal, Musharaka…)
- [ ] Current profit/rent rate, and whether it's fixed or floating
- [ ] Day basis: 360 or 365
- [ ] Posting frequency: monthly or quarterly, and on which day
- [ ] Is profit computed on the **daily** outstanding balance?
- [ ] Late/overdue **compensation** rate, if any
- [ ] Excise duty amount and timing
- [ ] Renewal fee and renewal date
- [ ] Early/full settlement fee (one IBBL scheme lists 0.50% of the outstanding, but this needs confirming for this product)
- [ ] Order in which a deposit is applied (charges → profit → principal?)

**How to confirm:** take 3–6 months of statement entries with at least one profit posting. Work the effective rate out backwards from the posted profit, then test it against both 360 and 365.
