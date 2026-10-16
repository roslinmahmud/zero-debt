import base64
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import backup, services
from app import engine as eng
from app import models as m
from app.db import UPLOAD_DIR, get_db, init_db

log = logging.getLogger("loan_tracker")
BASE = Path(__file__).resolve().parent
AUTH_USER = os.environ.get("LOAN_TRACKER_USER", "")
AUTH_PASSWORD = os.environ.get("LOAN_TRACKER_PASSWORD", "")
PROOF_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_PROOF_BYTES = 5 * 1024 * 1024
MAX_IMPORT_BYTES = 20 * 1024 * 1024
METHODS = ("Wise", "Remitly", "bank", "bKash", "family member", "other")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not (AUTH_USER and AUTH_PASSWORD):
        log.warning("LOAN_TRACKER_USER/PASSWORD not set: no authentication. Keep the app behind Tailscale.")
    yield


app = FastAPI(title="CC Loan Tracker", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ---------------------------------------------------------------- formatting


def fmt_bdt(v, decimals: int = 2) -> str:
    """Bangladeshi digit grouping: 12,34,567.89"""
    if v is None:
        return "–"
    v = Decimal(v)
    whole, _, frac = f"{abs(v):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups)
    return ("-" if v < 0 else "") + whole + (f".{frac}" if frac else "")


templates.env.filters["tk"] = fmt_bdt
templates.env.filters["num"] = lambda v, d=2: "–" if v is None else f"{Decimal(v):,.{d}f}"


# ---------------------------------------------------------------- security


def _authorized(request: Request) -> bool:
    if not (AUTH_USER and AUTH_PASSWORD):
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    user_ok = secrets.compare_digest(user.encode(), AUTH_USER.encode())
    pw_ok = secrets.compare_digest(pw.encode(), AUTH_PASSWORD.encode())
    return user_ok and pw_ok


@app.middleware("http")
async def security(request: Request, call_next):
    if not _authorized(request):
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Loan Tracker"'})
    # Basic auth is sent automatically by browsers, so reject cross-site form posts.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and urlparse(source).netloc != request.headers.get("host"):
            return Response("Cross-site request blocked", 403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# ---------------------------------------------------------------- errors & parsing


class FormError(Exception):
    pass


class NeedsSetup(Exception):
    pass


@app.exception_handler(FormError)
async def form_error(request: Request, exc: FormError):
    return templates.TemplateResponse(request, "error.html", {"message": str(exc)}, status_code=400)


@app.exception_handler(NeedsSetup)
async def needs_setup(_request: Request, _exc: NeedsSetup):
    return RedirectResponse("/setup", 303)


def num(v: str | None, label: str, *, optional: bool = False, min_: Decimal | None = Decimal("0")) -> Decimal | None:
    v = (v or "").strip().replace(",", "")
    if not v:
        if optional:
            return None
        raise FormError(f"{label} is required")
    try:
        out = Decimal(v)
    except InvalidOperation:
        raise FormError(f"{label}: '{v}' is not a number") from None
    if not out.is_finite() or (min_ is not None and out < min_):
        raise FormError(f"{label} must be at least {min_}")
    return out


def day(v: str | None, label: str, *, optional: bool = False) -> date | None:
    v = (v or "").strip()
    if not v:
        if optional:
            return None
        raise FormError(f"{label} is required")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise FormError(f"{label}: '{v}' is not a valid date (YYYY-MM-DD)") from None


def require_ledger(db: Session = Depends(get_db)) -> services.Ledger:
    acc = services.get_account(db)
    if acc is None:
        raise NeedsSetup
    lg = services.load(db, acc)
    if not lg.ready:
        raise NeedsSetup
    return lg


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, 303)


# ---------------------------------------------------------------- dashboard


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, lg: services.Ledger = Depends(require_ledger)):
    ctx = services.dashboard(lg, date.today())
    return templates.TemplateResponse(request, "dashboard.html", {"d": ctx, "acc": lg.account})


# ---------------------------------------------------------------- setup


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    acc = services.get_account(db)
    rates = []
    if acc:
        rates = db.scalars(
            select(m.RateChange).where(m.RateChange.account_id == acc.id).order_by(m.RateChange.effective_date)
        ).all()
    return templates.TemplateResponse(request, "setup.html", {"acc": acc, "rates": rates})


@app.post("/setup")
def setup_save(
    db: Session = Depends(get_db),
    bank: str = Form(...),
    account_ref: str = Form(""),
    limit_bdt: str = Form(...),
    day_basis: int = Form(365),
    posting_frequency: str = Form("monthly"),
    posting_day: str = Form("month_end"),
    posting_grace_days: int = Form(7),
    renewal_date: str = Form(""),
    fx_eur_bdt: str = Form(""),
    settlement_fee_pct: str = Form("0"),
    recon_threshold_bdt: str = Form("10"),
    opening_date: str = Form(""),
    opening_balance: str = Form(""),
    rate_pct: str = Form(""),
):
    account_ref = account_ref.strip()
    if account_ref and not (account_ref.isdigit() and len(account_ref) == 4):
        raise FormError("Account ref must be exactly the last 4 digits")
    try:
        eng.Config(day_basis, posting_frequency, posting_day, posting_grace_days)
    except ValueError as exc:
        raise FormError(str(exc)) from None

    acc = services.get_account(db)
    is_new = acc is None
    if is_new:
        acc = m.Account()
        db.add(acc)
    acc.bank = bank.strip()[:120] or "Bank"
    acc.account_ref = account_ref
    acc.limit_bdt = num(limit_bdt, "Limit")
    acc.day_basis = day_basis
    acc.posting_frequency = posting_frequency
    acc.posting_day = posting_day
    acc.posting_grace_days = posting_grace_days
    acc.renewal_date = day(renewal_date, "Renewal date", optional=True)
    acc.fx_eur_bdt = num(fx_eur_bdt, "FX rate", optional=True)
    acc.settlement_fee_pct = num(settlement_fee_pct, "Settlement fee") or Decimal("0")
    acc.recon_threshold_bdt = num(recon_threshold_bdt, "Reconciliation threshold")

    if is_new:
        cp_date = day(opening_date, "Opening checkpoint date")
        cp_bal = num(opening_balance, "Opening balance", min_=None)
        rate = num(rate_pct, "Profit rate")
        db.flush()
        db.add(m.StatementCheckpoint(account_id=acc.id, date=cp_date, balance_bdt=cp_bal, note="Opening"))
        db.add(m.RateChange(account_id=acc.id, effective_date=cp_date, rate_pct=rate))
    db.commit()
    return redirect("/" if is_new else "/setup")


@app.post("/rates")
def rate_add(
    db: Session = Depends(get_db),
    effective_date: str = Form(...),
    rate_pct: str = Form(...),
):
    acc = services.get_account(db)
    if acc is None:
        raise NeedsSetup
    db.add(m.RateChange(account_id=acc.id, effective_date=day(effective_date, "Effective date"), rate_pct=num(rate_pct, "Rate")))
    db.commit()
    return redirect("/setup")


@app.post("/rates/{rid}/delete")
def rate_delete(rid: int, db: Session = Depends(get_db)):
    r = db.get(m.RateChange, rid)
    if r:
        count = len(db.scalars(select(m.RateChange.id)).all())
        if count <= 1:
            raise FormError("At least one rate is required")
        db.delete(r)
        db.commit()
    return redirect("/setup")


# ---------------------------------------------------------------- transactions


@app.get("/transactions", response_class=HTMLResponse)
def txn_list(request: Request, type: str = "", lg: services.Ledger = Depends(require_ledger)):
    rows = [t for t in reversed(lg.db_txns) if not type or t.type == type]
    return templates.TemplateResponse(
        request, "transactions.html", {"rows": rows, "types": eng.TXN_TYPES, "selected": type}
    )


@app.get("/transactions/new", response_class=HTMLResponse)
def txn_new(request: Request, lg: services.Ledger = Depends(require_ledger)):
    return templates.TemplateResponse(
        request, "txn_form.html", {"t": None, "types": eng.TXN_TYPES, "methods": METHODS, "today": date.today()}
    )


@app.get("/transactions/{tid}/edit", response_class=HTMLResponse)
def txn_edit(request: Request, tid: int, db: Session = Depends(get_db), lg: services.Ledger = Depends(require_ledger)):
    t = db.get(m.Txn, tid)
    if t is None:
        return redirect("/transactions")
    return templates.TemplateResponse(
        request, "txn_form.html", {"t": t, "types": eng.TXN_TYPES, "methods": METHODS, "today": date.today()}
    )


async def _save_proof(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    ext = Path(upload.filename).suffix.lower()
    if ext not in PROOF_TYPES:
        raise FormError(f"Proof must be one of: {', '.join(PROOF_TYPES)}")
    data = await upload.read(MAX_PROOF_BYTES + 1)
    if len(data) > MAX_PROOF_BYTES:
        raise FormError("Proof file is larger than 5 MB")
    name = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return name


def _proof_file(name: str | None) -> Path | None:
    if not name:
        return None
    path = (UPLOAD_DIR / name).resolve()
    if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
        return None
    return path


@app.post("/transactions")
@app.post("/transactions/{tid}")
async def txn_save(
    tid: int | None = None,
    db: Session = Depends(get_db),
    lg: services.Ledger = Depends(require_ledger),
    date_: str = Form(..., alias="date"),
    type: str = Form(...),
    amount_bdt: str = Form(""),
    eur_sent: str = Form(""),
    fx_rate: str = Form(""),
    transfer_fee_eur: str = Form(""),
    method: str = Form(""),
    note: str = Form(""),
    remove_proof: str = Form(""),
    proof: UploadFile | None = File(None),
):
    if type not in eng.TXN_TYPES:
        raise FormError("Unknown transaction type")
    eur = num(eur_sent, "EUR sent", optional=True)
    fx = num(fx_rate, "FX rate", optional=True)
    amount = num(amount_bdt, "Amount (BDT)", optional=True)
    if amount is None:
        if eur is None or fx is None:
            raise FormError("Amount (BDT) is required (or give EUR sent and FX rate)")
        amount = eng.q2(eur * fx)
    if method and method not in METHODS:
        raise FormError("Unknown method")

    t = db.get(m.Txn, tid) if tid else None
    if tid and t is None:
        return redirect("/transactions")
    if t is None:
        t = m.Txn(account_id=lg.account.id)
        db.add(t)
    t.date = day(date_, "Date")
    t.type = type
    t.amount_bdt = amount
    is_deposit = type == "deposit"
    t.eur_sent = eur if is_deposit else None
    t.fx_rate = fx if is_deposit else None
    t.transfer_fee_eur = num(transfer_fee_eur, "Transfer fee", optional=True) if is_deposit else None
    t.method = (method or None) if is_deposit else None
    t.note = note.strip()[:2000]

    new_proof = await _save_proof(proof)
    if (new_proof or remove_proof) and t.proof_path:
        old = _proof_file(t.proof_path)
        if old:
            old.unlink()
        t.proof_path = None
    if new_proof:
        t.proof_path = new_proof
    db.commit()
    return redirect("/transactions")


@app.post("/transactions/{tid}/delete")
def txn_delete(tid: int, db: Session = Depends(get_db)):
    t = db.get(m.Txn, tid)
    if t:
        path = _proof_file(t.proof_path)
        if path:
            path.unlink()
        db.delete(t)
        db.commit()
    return redirect("/transactions")


@app.get("/transactions/{tid}/proof")
def txn_proof(tid: int, db: Session = Depends(get_db)):
    t = db.get(m.Txn, tid)
    path = _proof_file(t.proof_path if t else None)
    if path is None:
        return Response("Not found", 404)
    return FileResponse(path, media_type=PROOF_TYPES[path.suffix], filename=f"proof-{t.date}{path.suffix}")


# ---------------------------------------------------------------- checkpoints & reconciliation


@app.get("/checkpoints", response_class=HTMLResponse)
def checkpoints(request: Request, db: Session = Depends(get_db), lg: services.Ledger = Depends(require_ledger)):
    rows = db.scalars(select(m.StatementCheckpoint).order_by(m.StatementCheckpoint.date.desc())).all()
    return templates.TemplateResponse(request, "checkpoints.html", {"rows": rows, "today": date.today()})


@app.post("/checkpoints")
def checkpoint_add(
    db: Session = Depends(get_db),
    lg: services.Ledger = Depends(require_ledger),
    date_: str = Form(..., alias="date"),
    balance_bdt: str = Form(...),
    note: str = Form(""),
):
    db.add(
        m.StatementCheckpoint(
            account_id=lg.account.id,
            date=day(date_, "Date"),
            balance_bdt=num(balance_bdt, "Balance", min_=None),
            note=note.strip()[:500],
        )
    )
    db.commit()
    return redirect("/checkpoints")


@app.post("/checkpoints/{cid}/delete")
def checkpoint_delete(cid: int, db: Session = Depends(get_db)):
    c = db.get(m.StatementCheckpoint, cid)
    if c:
        if len(db.scalars(select(m.StatementCheckpoint.id)).all()) <= 1:
            raise FormError("At least one checkpoint (the opening balance) is required")
        db.delete(c)
        db.commit()
    return redirect("/checkpoints")


@app.get("/reconciliation", response_class=HTMLResponse)
def reconciliation(request: Request, lg: services.Ledger = Depends(require_ledger)):
    ctx = services.reconciliation(lg, date.today())
    return templates.TemplateResponse(
        request, "reconciliation.html", {**ctx, "threshold": lg.account.recon_threshold_bdt, "acc": lg.account}
    )


# ---------------------------------------------------------------- charts


@app.get("/charts")
def charts():
    return redirect("/")


@app.get("/api/charts")
def charts_data(lg: services.Ledger = Depends(require_ledger)):
    return JSONResponse(services.chart_data(lg, date.today()))


# ---------------------------------------------------------------- simulator


def _parse_lumps(text: str) -> tuple[tuple[date, Decimal], ...]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.replace(",", " ").split()
        if len(parts) != 2:
            raise FormError(f"Lump sum line '{line}' must be: YYYY-MM-DD amount")
        out.append((day(parts[0], "Lump sum date"), num(parts[1], "Lump sum amount")))
    return tuple(out)


@app.get("/simulator", response_class=HTMLResponse)
def simulator(request: Request, lg: services.Ledger = Depends(require_ledger)):
    q = request.query_params
    today = date.today()
    submitted = "s1_amount" in q
    fx = num(q.get("fx"), "FX assumption", optional=True) or services.fx_rate(lg)
    form = {
        "fx": fx,
        "lumps": q.get("lumps", ""),
        "rate_date": q.get("rate_date", ""),
        "rate_pct": q.get("rate_pct", ""),
        "settle_date": q.get("settle_date", ""),
        "scenarios": [],
    }
    lumps = _parse_lumps(form["lumps"])
    extra_rates: tuple[eng.RateChange, ...] = ()
    if form["rate_date"] or form["rate_pct"]:
        extra_rates = (eng.RateChange(day(form["rate_date"], "Rate change date"), num(form["rate_pct"], "New rate")),)

    defaults = (("30000", "BDT"), ("50000", "BDT"), ("80000", "BDT"))
    scenarios = []
    for i in (1, 2, 3):
        amount = q.get(f"s{i}_amount", "" if submitted else defaults[i - 1][0])
        currency = q.get(f"s{i}_currency", defaults[i - 1][1])
        dep_day = int(num(q.get(f"s{i}_day", "1"), "Deposit day") or 1)
        if not 1 <= dep_day <= 31:
            raise FormError("Deposit day must be 1–31")
        form["scenarios"].append({"amount": amount, "currency": currency, "day": dep_day})
        value = num(amount, f"Scenario {i} amount", optional=True)
        if value is None:
            continue
        if currency == "EUR":
            if not fx:
                raise FormError("An FX assumption is needed for EUR scenarios")
            label = f"€{value:,.0f}/mo (day {dep_day})"
            value = eng.q2(value * fx)
        else:
            label = f"Tk {fmt_bdt(value, 0)}/mo (day {dep_day})"
        scenarios.append(eng.Scenario(label, value, dep_day, lumps, extra_rates))

    results = services.run_scenarios(lg, today, scenarios)
    insight = services.timing_insight(lg, today, scenarios[0]) if scenarios else None
    settle = None
    if form["settle_date"]:
        settle_date = day(form["settle_date"], "Settlement date")
        if settle_date < lg.checkpoints[0].date:
            raise FormError("Settlement date is before the opening checkpoint")
        settle = services.settle_on(lg, settle_date)

    chart = [
        {"label": r.scenario.name, "points": [{"x": d.isoformat(), "y": float(v)} for d, v in r.series]}
        for r in results
    ]
    return templates.TemplateResponse(
        request,
        "simulator.html",
        {"form": form, "results": results, "insight": insight, "settle": settle, "chart": chart, "today": today},
    )


# ---------------------------------------------------------------- data export / import


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request):
    return templates.TemplateResponse(request, "data.html", {})


@app.get("/export.json")
def export_json(db: Session = Depends(get_db)):
    body = json.dumps(backup.export_json(db), indent=2)
    return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="loan-tracker-{date.today()}.json"'})


@app.get("/export/transactions.csv")
def export_csv(db: Session = Depends(get_db)):
    return Response(
        backup.export_txn_csv(db),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="transactions-{date.today()}.csv"'},
    )


@app.post("/import")
async def import_json(db: Session = Depends(get_db), file: UploadFile = File(...), confirm: str = Form("")):
    if confirm != "yes":
        raise FormError("Tick the confirmation box: importing replaces all existing data")
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise FormError("Import file is too large")
    try:
        backup.import_json(db, json.loads(raw))
        db.commit()
    except (ValueError, KeyError, TypeError) as exc:
        db.rollback()
        raise FormError(f"Import failed: {exc}") from None
    return redirect("/")
