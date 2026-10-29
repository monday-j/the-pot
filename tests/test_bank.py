import pytest
from dataclasses import replace

from app import bank, config, square
from app.payouts import RuleError
from tests.test_square import FakeSquare, TwoNightsClient, fnb, pay  # noqa: F401 (fixtures)
from tests.test_sync import conn  # noqa: F401
from tests.test_web import client  # noqa: F401


def paid(pid, cents, fee, when):
    return replace(pay(pid, cents, when=when), fee_cents=fee)


@pytest.fixture
def two_nights(fnb, monkeypatch):  # noqa: F811
    """Night 1 (Jan 1): two $10 payments and a $6 drink (ignored). Night 2 (Jan 8): one $10 payment, fee pending."""
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    square.pull_tournament(fnb, FakeSquare([paid("A", 1000, 22, "2026-01-01T19:00:00Z"),
                                            paid("B", 1000, 22, "2026-01-01T19:05:00Z"),
                                            paid("D", 600, 13, "2026-01-01T21:00:00Z")]), "t1")
    fnb.execute("UPDATE square_payment SET ignored=1 WHERE id='D'")
    square.pull_tournament(fnb, FakeSquare([pay("C", 1000, when="2026-01-08T19:00:00Z")]), "t3")
    return fnb


def deposit(conn, day, cents):
    return conn.execute("SELECT * FROM bank_deposit WHERE id=?",
                        (bank.save_deposit(conn, None, day, cents, "SQUARE", ""),)).fetchone()


def test_suggestion_ticks_whole_earlier_days_that_fit(two_nights):
    d = deposit(two_nights, "2026-01-02", 2543)  # night 1 net: 978 + 978 + 587 (the drink counts: Square paid it out)
    options = bank.candidates(two_nights, d)
    assert {p["id"]: p["suggested"] for p in options} == {"A": True, "B": True, "D": True}  # night 2 is after it
    later = deposit(two_nights, "2026-01-09", 1000)
    assert {p["id"]: p["suggested"] for p in bank.candidates(two_nights, later)} == {"A": False, "B": False, "D": False,
                                                                                    "C": True}


def test_matching_adds_up_and_flags_differences(two_nights):
    d = deposit(two_nights, "2026-01-02", 2543)
    bank.match(two_nights, d["id"], ["A", "B", "D"])
    s = bank.summary(two_nights, d)
    assert (s["matched"], s["difference"], len(s["payments"])) == (2543, 0, 3)
    bank.unmatch(two_nights, d["id"], "D")
    assert bank.summary(two_nights, d)["difference"] == 587
    other = deposit(two_nights, "2026-01-03", 100)
    bank.match(two_nights, other["id"], ["A"])  # already in the first deposit: stays there
    assert bank.summary(two_nights, other)["payments"] == []
    two_nights.execute("DELETE FROM bank_deposit WHERE id=?", (d["id"],))
    assert two_nights.execute("SELECT COUNT(*) FROM square_payment WHERE deposit_id IS NOT NULL").fetchone()[0] == 0


def test_unbanked_days_are_grouped_and_flagged_when_overdue(two_nights):
    days = bank.unbanked(two_nights, today="2026-01-09")
    assert [(d["day"], d["count"], d["gross"], d["fees"], d["net"], d["pending"], d["overdue"]) for d in days] == [
        ("2026-01-01", 3, 2600, 57, 2543, 0, True), ("2026-01-08", 1, 1000, 0, 1000, 1, False)]


def test_tournament_reconcile_reports_what_is_banked(two_nights):
    d = deposit(two_nights, "2026-01-02", 2543)
    bank.match(two_nights, d["id"], ["A"])
    r = square.reconcile(two_nights, "t1")
    assert (r["net"], r["banked"]) == (1956, 978)  # the ignored drink isn't tournament takings


def test_deposit_validation(two_nights):
    with pytest.raises(RuleError, match="more than"):
        bank.save_deposit(two_nights, None, "2026-01-02", 0, "", "")


def test_bank_pages(client):  # noqa: F811
    assert "No deposits recorded yet" in client.get("/bank").text
    assert "not a date" in client.post("/bank", data={"deposited_on": "soon", "amount": "5"}).text
    assert "more than $0" in client.post("/bank", data={"deposited_on": "2026-01-02", "amount": "0"}).text
    page = client.post("/bank", data={"deposited_on": "2026-01-02", "amount": "25.43", "reference": "SQ *PAYOUT"}).text
    assert "Deposit 2026-01-02" in page and "SQ *PAYOUT" in page and "No unbanked Square payments" in page
    page = client.post("/bank/1", data={"deposited_on": "2026-01-03", "amount": "30", "note": "fixed"}).text
    assert "Deposit 2026-01-03" in page and "$30.00</b> deposited" in page
    page = client.get("/bank").text
    assert 'href="/bank/1"' in page and "$30.00" in page and "Every Square payment is matched" in page
    assert 'href="/bank"' in client.get("/").text  # in the nav
    assert "No deposits recorded yet" in client.post("/bank/1/delete").text
    assert client.get("/bank/1").status_code == 404
