import json
import os
import tempfile
from datetime import date, timedelta

os.environ["LOAN_TRACKER_DATA"] = tempfile.mkdtemp(prefix="loan-tracker-test-")
os.environ.pop("LOAN_TRACKER_USER", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, fmt_bdt  # noqa: E402

TODAY = date.today()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_redirects_to_setup_then_full_flow(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"

    opening = TODAY - timedelta(days=60)
    r = client.post(
        "/setup",
        data={
            "bank": "Islami Bank Bangladesh PLC", "account_ref": "1234", "limit_bdt": "1000000",
            "day_basis": "365", "posting_frequency": "monthly", "posting_day": "month_end",
            "posting_grace_days": "7", "renewal_date": str(TODAY + timedelta(days=20)), "fx_eur_bdt": "130",
            "settlement_fee_pct": "0.5", "recon_threshold_bdt": "10",
            "opening_date": str(opening), "opening_balance": "500000", "rate_pct": "12",
        },
    )
    assert r.status_code == 200

    r = client.post(
        "/transactions",
        data={"date": str(TODAY - timedelta(days=10)), "type": "deposit", "eur_sent": "300", "fx_rate": "135",
              "transfer_fee_eur": "2", "method": "Wise", "note": "monthly"},
        files={"proof": ("receipt.png", b"\x89PNG fake", "image/png")},
    )
    assert r.status_code == 200
    assert "40,500.00" in client.get("/transactions").text

    r = client.post("/transactions", data={"date": str(TODAY), "type": "bogus", "amount_bdt": "1"})
    assert r.status_code == 400

    r = client.post("/checkpoints", data={"date": str(TODAY - timedelta(days=5)), "balance_bdt": "470000"})
    assert r.status_code == 200

    for url in ("/", "/transactions", "/checkpoints", "/reconciliation", "/charts", "/setup", "/data",
                "/simulator", f"/simulator?s1_amount=300&s1_currency=EUR&s1_day=5&s2_amount=50000&s2_day=1"
                f"&s3_amount=&fx=130&settle_date={TODAY + timedelta(days=30)}&lumps=2030-01-01+10000"):
        r = client.get(url)
        assert r.status_code == 200, url

    data = client.get("/api/charts").json()
    assert data["outstanding"]["actual"] and data["eur_cost"][0]["effective"] > 0

    exported = client.get("/export.json").json()
    assert exported["account"]["account_ref"] == "1234"
    assert len(exported["transactions"]) == 1
    r = client.post("/import", data={"confirm": "yes"},
                    files={"file": ("x.json", json.dumps(exported).encode(), "application/json")})
    assert r.status_code == 200
    assert client.get("/export.json").json()["transactions"] == exported["transactions"]


def test_cross_site_post_blocked(client):
    r = client.post("/checkpoints", data={"date": str(TODAY), "balance_bdt": "1"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_bdt_grouping():
    assert fmt_bdt("1234567.5") == "12,34,567.50"
    assert fmt_bdt("-999") == "-999.00"
