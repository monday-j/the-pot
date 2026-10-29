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


from app.square_client import SquarePayout, SquarePayoutEntry, parse_payout, parse_payout_entry  # noqa: E402


def entry(eid, kind, payment_id, gross, fee):
    return SquarePayoutEntry(eid, kind, payment_id, gross, fee, gross + fee, "2026-01-02T10:00:00Z")


@pytest.fixture
def payout_square():
    """Square paid night 1 out on Jan 2: A and B (ours, $9.78 net each), a $20 sale we never pulled, and a refund."""
    sq = FakeSquare()
    sq.payouts = [SquarePayout("po_1", "SENT", 978 + 978 + 1956 - 500, "2026-01-02T08:00:00Z", "2026-01-03", 1)]
    sq.entries = {"po_1": [entry("e1", "CHARGE", "A", 1000, -22), entry("e2", "CHARGE", "B", 1000, -22),
                           entry("e3", "CHARGE", "UNPULLED", 2000, -44), entry("e4", "REFUND", "OLD", -500, 0)]}
    return sq


def test_parse_payout_and_entries():
    p = parse_payout({"id": "po_1", "status": "PAID", "amount_money": {"amount": 4366, "currency": "AUD"},
                      "created_at": "2026-01-02T08:00:00Z", "arrival_date": "2026-01-03", "version": 2})
    assert (p.id, p.status, p.amount_cents, p.arrival_date, p.version) == ("po_1", "PAID", 4366, "2026-01-03", 2)
    e = parse_payout_entry({"id": "poe_1", "type": "CHARGE", "gross_amount_money": {"amount": 1000},
                            "fee_amount_money": {"amount": -22}, "net_amount_money": {"amount": 978},
                            "type_charge_details": {"payment_id": "A"}, "effective_at": "2026-01-02T10:00:00Z"})
    assert (e.type, e.payment_id, e.gross_cents, e.fee_cents, e.net_cents) == ("CHARGE", "A", 1000, -22, 978)
    r = parse_payout_entry({"id": "poe_2", "type": "REFUND", "type_refund_details": {"payment_id": "A", "refund_id": "R"}})
    assert r.payment_id == "A"
    assert parse_payout_entry({"id": "poe_3", "type": "FEE"}).payment_id == ""


def test_import_matches_our_payments_and_lists_other_takings(two_nights, payout_square):
    r = bank.import_payouts(two_nights, payout_square, "2026-01-01T00:00:00Z", "2026-01-09T00:00:00Z")
    assert r == {"new": 1, "updated": 0, "unchanged": 0, "matched": 2}
    d = two_nights.execute("SELECT * FROM bank_deposit").fetchone()
    assert (d["deposited_on"], d["amount_cents"], d["payout_status"], d["seen_in_bank"]) == ("2026-01-03", 3412, "SENT", 0)
    s = bank.summary(two_nights, d, today="2026-01-04")
    assert s["imported"] and s["difference"] == 0 and s["flag"] == ""
    assert (s["tournament"], s["other"], s["adjustment"]) == ({"count": 2, "net": 1956}, {"count": 1, "net": 1956},
                                                             {"count": 1, "net": -500})
    matched = {r[0] for r in two_nights.execute("SELECT id FROM square_payment WHERE deposit_id=?", (d["id"],))}
    assert matched == {"A", "B"}  # the drink (D) wasn't in this payout, so it stays unbanked
    assert bank.summary(two_nights, d, today="2026-01-09")["flag"] == "not seen in the bank"
    bank.set_seen(two_nights, d["id"], True)
    d = two_nights.execute("SELECT * FROM bank_deposit").fetchone()
    assert bank.summary(two_nights, d, today="2026-01-09")["flag"] == ""


def test_reimport_only_fetches_entries_for_changed_payouts(two_nights, payout_square):
    bank.import_payouts(two_nights, payout_square, "a", "b")
    payout_square.calls.clear()
    assert bank.import_payouts(two_nights, payout_square, "a", "b")["unchanged"] == 1
    assert [c[0] for c in payout_square.calls] == ["payouts"]  # no entries call for an unchanged payout
    payout_square.payouts = [replace(payout_square.payouts[0], status="FAILED", version=2)]
    payout_square.calls.clear()
    assert bank.import_payouts(two_nights, payout_square, "a", "b")["updated"] == 1
    assert ("entries", "po_1") in payout_square.calls
    d = two_nights.execute("SELECT * FROM bank_deposit").fetchone()
    assert bank.summary(two_nights, d)["flag"] == "payout failed"
    assert two_nights.execute("SELECT COUNT(*) FROM square_payout_entry").fetchone()[0] == 4  # replaced, not doubled


def test_import_never_moves_a_payment_matched_by_hand(two_nights, payout_square):
    manual = deposit(two_nights, "2026-01-02", 978)
    bank.match(two_nights, manual["id"], ["A"])
    assert bank.import_payouts(two_nights, payout_square, "a", "b")["matched"] == 1  # only B
    assert two_nights.execute("SELECT deposit_id FROM square_payment WHERE id='A'").fetchone()[0] == manual["id"]


def test_a_payment_pulled_after_its_payout_is_matched_when_stored(two_nights, payout_square, monkeypatch):
    bank.import_payouts(two_nights, payout_square, "a", "b")
    deposit_id = two_nights.execute("SELECT id FROM bank_deposit").fetchone()[0]
    square.pull_tournament(two_nights, FakeSquare([paid("UNPULLED", 2000, 44, "2026-01-01T22:00:00Z")]), "t1")
    assert two_nights.execute("SELECT deposit_id FROM square_payment WHERE id='UNPULLED'").fetchone()[0] == deposit_id
    s = bank.summary(two_nights, two_nights.execute("SELECT * FROM bank_deposit").fetchone())
    assert s["other"]["count"] == 0 and s["tournament"]["count"] == 3  # no longer "other": it's a tournament payment


def test_payouts_need_the_payouts_permission(monkeypatch):
    from tests.test_square import square_with
    c, _, _ = square_with(monkeypatch, [403])
    with pytest.raises(RuntimeError, match="needs the PAYOUTS_READ permission"):
        c.list_payouts("a", "b")


def test_payout_pages(client, monkeypatch, payout_square):  # noqa: F811
    import time
    from app import main, sync
    monkeypatch.setattr(main, "GrpcParryClient", TwoNightsClient)
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    monkeypatch.setenv("SQUARE_API_KEY", "tok")
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    payout_square.payments = [paid("A", 1000, 22, "2026-01-01T19:00:00Z"), paid("B", 1000, 22, "2026-01-01T19:05:00Z")]
    monkeypatch.setattr(main, "HttpSquareClient", lambda: payout_square)
    client.post("/tournaments/t1/square/pull")
    assert "not a date" in client.post("/bank/square/pull", data={"since": "x"}).text
    client.post("/bank/square/pull", data={"since": "2026-01-01"})
    for _ in range(100):
        if square.status.get("payouts", {}).get("state") not in (None, "running"):
            break
        time.sleep(0.02)
    page = client.get("/bank").text
    assert "Square payouts, " in page and "1 new and 0 updated payouts since 2026-01-01" in page
    assert "Square payout" in page and "not seen in the bank" in page  # due back in January, long overdue
    page = client.get("/bank/1").text
    assert "other Square takings (1)" in page and "$0.00</b> adds up" in page and "Seen in bank" in page
    assert "Add payments" not in page  # Square's entries are the breakdown
    page = client.post("/bank/1", data={"deposited_on": "2030-01-01", "amount": "1", "reference": "SQ 123"}).text
    assert "SQ 123" in page and "Deposit 2026-01-03" in page and "$34.12</b> deposited" in page  # date/amount kept
    page = client.post("/bank/1/seen", data={"seen": "1"}).text
    assert "Seen in the bank." in page
    assert "class=\"bad\">not seen in the bank" not in client.get("/bank").text
    assert "$19.56</b> of it" in client.get("/tournaments/t1").text  # A and B, banked via the payout
