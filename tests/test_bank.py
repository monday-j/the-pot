import pytest
from dataclasses import replace

from app import bank, config, square, sync
from app.payouts import RuleError
from tests.test_square import FakeSquare, TwoNightsClient, fnb, oauth_env, pay  # noqa: F401 (fixtures)
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


def pay_out(conn, day, *payout_ids):
    """Mark payouts paid at midday (UTC) on `day`."""
    for pid in payout_ids:
        conn.execute("UPDATE payout SET status='paid', paid_at=? WHERE id=?", (f"{day}T12:00:00+00:00", pid))


def test_the_first_reconciliation_is_the_opening_balance_and_later_ones_compare(two_nights):
    early = deposit(two_nights, "2026-01-02", 2543)
    e = bank.expected(two_nights, "2026-01-05")
    assert e["expected"] is None and e["in"] == 2543  # nothing to compare yet
    first = bank.reconcile(two_nights, "2026-01-05", 10000, "opening")
    assert two_nights.execute("SELECT expected_cents FROM reconciliation WHERE id=?", (first,)).fetchone()[0] == 10000
    assert two_nights.execute("SELECT reconciliation_id FROM bank_deposit WHERE id=?",
                              (early["id"],)).fetchone()[0] == first  # taken in, and locked
    deposit(two_nights, "2026-01-09", 1000)
    deposit(two_nights, "2026-01-20", 5000)  # after the next reconciliation's day: not counted yet
    [po] = [r[0] for r in two_nights.execute("SELECT id FROM payout WHERE event_id='e1' ORDER BY id LIMIT 1")]
    amount = two_nights.execute("SELECT amount_cents FROM payout WHERE id=?", (po,)).fetchone()[0]
    pay_out(two_nights, "2026-01-08", po)
    e = bank.expected(two_nights, "2026-01-10")
    assert (e["in"], e["out"], e["expected"]) == (1000, amount, 10000 + 1000 - amount)
    bank.reconcile(two_nights, "2026-01-10", 10000 + 1000 - amount - 300, "  $3 bank fee  ")  # doesn't match: kept
    [latest, opening] = bank.history(two_nights)
    assert (latest["difference"], latest["note"], latest["deposits"], latest["paid_out"]) == (-300, "$3 bank fee", 1, 1)
    assert opening["difference"] == 0
    assert bank.expected(two_nights, "2026-01-31")["expected"] == latest["balance_cents"] + 5000


def test_reconciliations_move_forward_and_only_the_latest_is_undone(two_nights, monkeypatch):
    monkeypatch.setattr(bank, "today", lambda: "2026-02-01")
    with pytest.raises(RuleError, match="hasn't happened"):
        bank.reconcile(two_nights, "2026-02-02", 0, "")
    with pytest.raises(RuleError, match="no reconciliation"):
        bank.undo_latest(two_nights)
    bank.reconcile(two_nights, "2026-01-05", 0, "")
    with pytest.raises(RuleError, match="after 2026-01-05"):
        bank.reconcile(two_nights, "2026-01-05", 0, "")
    d = deposit(two_nights, "2026-01-09", 1000)
    second = bank.reconcile(two_nights, "2026-01-10", 1000, "")
    bank.undo_latest(two_nights)
    assert two_nights.execute("SELECT reconciliation_id FROM bank_deposit WHERE id=?", (d["id"],)).fetchone()[0] is None
    assert bank.latest(two_nights)["reconciled_on"] == "2026-01-05"
    assert second not in [r["id"] for r in bank.history(two_nights)]


def test_money_dated_in_a_reconciled_period_is_locked(two_nights):
    bank.reconcile(two_nights, "2026-01-05", 0, "")
    with pytest.raises(RuleError, match="locked by the bank reconciliation of 2026-01-05"):
        deposit(two_nights, "2026-01-05", 100)
    deposit(two_nights, "2026-01-06", 100)


def test_tournaments_lock_once_clear_and_unlock_with_the_reconciliation(two_nights):
    bank.reconcile(two_nights, "2026-01-31", 0, "")
    assert square.lock_clear_tournaments(two_nights) == 0  # both nights have payments to sort out
    square.link_payment(two_nights, "A", "u1")
    square.link_payment(two_nights, "B", "u2")
    for player in ("u3", "u4"):
        square.resolve_entrant(two_nights, "t1", player, "paid cash")
    assert square.lock_clear_tournaments(two_nights) == 1
    locked = {r[0]: r[1] for r in two_nights.execute("SELECT id, locked_by FROM tournament")}
    assert locked["t1"] is not None and locked["t3"] is None
    with pytest.raises(RuleError, match="Locked by the bank reconciliation of 2026-01-31"):
        square.link_payment(two_nights, "A", None)
    with pytest.raises(RuleError, match="Locked"):
        square.unresolve_entrant(two_nights, "t1", "u3")
    square.link_payment(two_nights, "C", "u2")  # t3 isn't locked: still editable
    before = {r[0]: r[1] for r in two_nights.execute(
        "SELECT po.id, po.amount_cents FROM payout po JOIN event e ON e.id=po.event_id WHERE e.tournament_id='t1'")}
    two_nights.execute("UPDATE series_period SET entry_fee_cents=9900")
    sync.recompute_series(two_nights, 1)  # a locked tournament's figures are frozen
    after = {r[0]: r[1] for r in two_nights.execute(
        "SELECT po.id, po.amount_cents FROM payout po JOIN event e ON e.id=po.event_id WHERE e.tournament_id='t1'")}
    assert after == before
    bank.undo_latest(two_nights)
    assert two_nights.execute("SELECT locked_by FROM tournament WHERE id='t1'").fetchone()[0] is None
    square.link_payment(two_nights, "A", None)


def test_tournament_reconcile_reports_what_is_banked(two_nights, payout_square):
    bank.import_payouts(two_nights, payout_square, "a", "b")
    r = square.reconcile(two_nights, "t1")
    assert (r["net"], r["banked"]) == (1956, 1956)  # A and B; the ignored drink isn't tournament takings


def test_deposit_validation(two_nights):
    with pytest.raises(RuleError, match="more than"):
        bank.save_deposit(two_nights, None, "2026-01-02", 0, "", "")


def test_bank_pages(client):  # noqa: F811
    page = client.get("/bank").text
    assert "No deposits recorded yet" in page and "No reconciliations yet" in page
    assert "not a date" in client.post("/bank", data={"deposited_on": "soon", "amount": "5"}).text
    assert "more than $0" in client.post("/bank", data={"deposited_on": "2026-01-02", "amount": "0"}).text
    page = client.post("/bank", data={"deposited_on": "2026-01-02", "amount": "25.43", "reference": "SQ *PAYOUT"}).text
    assert "Deposit 2026-01-02" in page and "SQ *PAYOUT" in page
    page = client.post("/bank/1", data={"deposited_on": "2026-01-03", "amount": "30", "note": "fixed"}).text
    assert "Deposit 2026-01-03" in page and "$30.00</b> deposited" in page
    page = client.get("/bank?on=2026-01-04").text
    assert 'href="/bank/1"' in page and "$30.00</b> in" in page and "The opening balance" in page
    assert 'href="/bank"' in client.get("/").text  # in the nav
    page = client.post("/bank/reconcile", data={"reconciled_on": "2026-01-04", "balance": "130", "note": "start"}).text
    assert "Reconciled to <b>2026-01-04</b>" in page and "(opening balance)" in page
    page = client.post("/bank", data={"deposited_on": "2026-01-03", "amount": "5"}).text
    assert "locked by the bank reconciliation of 2026-01-04" in page
    client.post("/bank", data={"deposited_on": "2026-01-05", "amount": "20"})
    page = client.get("/bank?on=2026-01-06").text
    assert "$150.00</b> expected on 2026-01-06" in page
    page = client.post("/bank/reconcile", data={"reconciled_on": "2026-01-06", "balance": "149",
                                                 "note": "fee"}).text
    assert "-$1.00" in page and "Reconciled to <b>2026-01-06</b>" in page
    assert "Locked by the bank reconciliation of 2026-01-04" in client.post("/bank/1/delete").text
    assert "can't change" not in client.get("/bank/2").text
    client.post("/bank/reconcile/undo")
    client.post("/bank/reconcile/undo")
    assert "No reconciliations yet" in client.post("/bank/1/delete").text
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
    assert (d["deposited_on"], d["amount_cents"], d["payout_status"]) == ("2026-01-03", 3412, "SENT")
    s = bank.summary(two_nights, d)
    assert s["imported"] and sum(e["net_cents"] for e in s["entries"]) == d["amount_cents"]
    assert (s["tournament"], s["other"], s["adjustment"]) == ({"count": 2, "net": 1956}, {"count": 1, "net": 1956},
                                                             {"count": 1, "net": -500})
    matched = {r[0] for r in two_nights.execute("SELECT id FROM square_payment WHERE deposit_id=?", (d["id"],))}
    assert matched == {"A", "B"}  # the drink (D) wasn't in this payout, so it stays unbanked


def test_reimport_only_fetches_entries_for_changed_payouts(two_nights, payout_square):
    bank.import_payouts(two_nights, payout_square, "a", "b")
    payout_square.calls.clear()
    assert bank.import_payouts(two_nights, payout_square, "a", "b")["unchanged"] == 1
    assert [c[0] for c in payout_square.calls] == ["payouts"]  # no entries call for an unchanged payout
    payout_square.payouts = [replace(payout_square.payouts[0], status="FAILED", version=2)]
    payout_square.calls.clear()
    assert bank.import_payouts(two_nights, payout_square, "a", "b")["updated"] == 1
    assert ("entries", "po_1") in payout_square.calls
    assert two_nights.execute("SELECT payout_status FROM bank_deposit").fetchone()[0] == "FAILED"
    assert bank.expected(two_nights, "2026-01-31")["in"] == 0  # a failed payout never arrived
    assert two_nights.execute("SELECT COUNT(*) FROM square_payout_entry").fetchone()[0] == 4  # replaced, not doubled


def test_a_reconciled_payout_is_left_alone_on_reimport(two_nights, payout_square):
    bank.import_payouts(two_nights, payout_square, "a", "b")
    bank.reconcile(two_nights, "2026-01-05", 3412, "")
    payout_square.payouts = [replace(payout_square.payouts[0], amount_cents=1, version=2)]
    payout_square.calls.clear()
    assert bank.import_payouts(two_nights, payout_square, "a", "b")["unchanged"] == 1
    assert ("entries", "po_1") not in payout_square.calls
    assert two_nights.execute("SELECT amount_cents FROM bank_deposit").fetchone()[0] == 3412


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
    with pytest.raises(RuntimeError, match="read-only access doesn.t include PAYOUTS_READ: authorise again"):
        c.list_payouts("a", "b")


def test_payout_pages(client, monkeypatch, payout_square):  # noqa: F811
    import time
    from app import main, sync, web
    monkeypatch.setattr(web, "GrpcParryClient", TwoNightsClient)
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    oauth_env(monkeypatch)
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    payout_square.payments = [paid("A", 1000, 22, "2026-01-01T19:00:00Z"), paid("B", 1000, 22, "2026-01-01T19:05:00Z")]
    monkeypatch.setattr(web, "HttpSquareClient", lambda: payout_square)
    client.post("/tournaments/t1/square/pull")
    assert "not a date" in client.post("/bank/square/pull", data={"since": "x"}).text
    client.post("/bank/square/pull", data={"since": "2026-01-01"})
    for _ in range(100):
        if square.status.get("payouts", {}).get("state") not in (None, "running"):
            break
        time.sleep(0.02)
    page = client.get("/bank").text
    assert "Square payouts, " in page and "1 new and 0 updated payouts since 2026-01-01" in page
    assert "Square payout" in page and "$34.12</b> in" in page
    page = client.get("/bank/1").text
    assert "other Square takings (1)" in page and "Add payments" not in page  # Square's entries are the breakdown
    page = client.post("/bank/1", data={"deposited_on": "2030-01-01", "amount": "1", "reference": "SQ 123"}).text
    assert "SQ 123" in page and "Deposit 2026-01-03" in page and "$34.12</b> deposited" in page  # date/amount kept
    assert "$19.56</b> of it" in client.get("/tournaments/t1").text  # A and B, banked via the payout


def test_locked_things_refuse_changes_on_the_pages(client, monkeypatch):  # noqa: F811
    import time
    from app import main, web
    monkeypatch.setattr(web, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    with main.db.session() as conn:
        conn.execute("""INSERT INTO rule (series_id, period_id, game_name, fixed_pool_cents, splits)
                        VALUES (1, 1, 'Smash Ultimate', 1000, '["100"]')""")
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    with main.db.session() as conn:
        po = conn.execute("SELECT id FROM payout ORDER BY id LIMIT 1").fetchone()[0]
    client.post(f"/payouts/{po}/paid", data={"note": "cash"})
    client.post("/bank/reconcile", data={"reconciled_on": bank.today(), "balance": "0"})
    page = client.get("/tournaments/t1").text  # no Square payments pulled, no unfinalised events: clear
    assert "Locked by the <a href=\"/bank\">bank reconciliation</a>" in page
    assert "Locked by the bank reconciliation" in client.post("/tournaments/t1/entry-fee", data={"entry_fee": "9"}).text
    assert "Locked by the bank reconciliation" in client.post("/tournaments/t1/venue", data={"venue_mode": "none"}).text
    assert "Locked by the bank reconciliation" in client.post("/tournaments/t1/exclude").text
    assert "Locked by the bank reconciliation" in client.post(f"/payouts/{po}/paid", data={"undo": "1"}).text
    page = client.get("/bank").text
    assert "1 paid out, 2 tournaments" in page
    client.post("/bank/reconcile/undo")
    assert "Locked by" not in client.get("/tournaments/t1").text
    client.post(f"/payouts/{po}/paid", data={"undo": "1"})
    with main.db.session() as conn:
        assert conn.execute("SELECT status FROM payout WHERE id=?", (po,)).fetchone()[0] == "owed"
