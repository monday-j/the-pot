import time

import pytest

from app import config, main, square, sync
from app.payouts import RuleError
from app.square_client import SquarePayment, parse_payment
from tests.test_sync import TwoNightsClient, conn  # noqa: F401 (fixture)
from tests.test_web import client  # noqa: F401 (fixture)


def pay(pid, amount, fp="", when="2026-01-01T09:00:00Z", status="COMPLETED"):
    return SquarePayment(pid, when, status, amount, "CARD", "VISA", fp[-4:].rjust(4, "0"), fp)


class FakeSquare:
    def __init__(self, payments=None):
        self.payments = payments or []
        self.calls = []

    def list_payments(self, begin_time, end_time, location_id=""):
        self.calls.append((begin_time, end_time, location_id))
        return self.payments

    def close(self):
        pass


@pytest.fixture
def fnb(conn):  # noqa: F811
    """FNB with a $5 entry fee and a $5 bracket fee: each of t1's four attendees owes $10."""
    conn.execute("UPDATE series SET custom_slug='fnb' WHERE id=1")
    conn.execute("UPDATE series_period SET entry_fee_cents=500 WHERE id=1")
    sync.sync_series(conn, TwoNightsClient(), 1)
    return conn


def test_window_is_the_local_day_in_utc():
    assert square.window("2026-01-01", "Australia/Hobart") == ("2025-12-31T13:00:00Z", "2026-01-01T19:00:00Z")
    assert square.window("2026-07-01", "UTC") == ("2026-07-01T00:00:00Z", "2026-07-02T06:00:00Z")


def test_parse_payment_nets_refunds_and_reads_the_card():
    p = parse_payment({"id": "P", "created_at": "2026-01-01T09:00:00Z", "status": "COMPLETED", "source_type": "CARD",
                       "amount_money": {"amount": 1500, "currency": "AUD"}, "refunded_money": {"amount": 500},
                       "card_details": {"card": {"card_brand": "MASTERCARD", "last_4": "4242", "fingerprint": "fp-1"}}})
    assert (p.amount_cents, p.card_brand, p.last_4, p.fingerprint) == (1000, "MASTERCARD", "4242", "fp-1")
    assert parse_payment({"id": "C", "status": "COMPLETED", "source_type": "CASH",
                          "amount_money": {"amount": 1000}}).amount_cents == 1000


def test_pull_stores_completed_payments_once_and_uses_the_venue_location(fnb):
    fnb.execute("INSERT INTO venue (id, name, square_location_id) VALUES (1, 'Hall', 'LOC1')")
    fnb.execute("UPDATE series_period SET venue_id=1")
    sync.recompute_series(fnb, 1)
    client = FakeSquare([pay("P1", 1000, "fp-a"), pay("P2", 1000, "fp-b"), pay("P3", 999, status="PENDING")])
    assert square.pull_tournament(fnb, client, "t1") == {"new": 2, "updated": 0, "elsewhere": 0, "linked": 0}
    assert client.calls[0][2] == "LOC1"
    assert square.pull_tournament(fnb, client, "t1")["updated"] == 2
    assert fnb.execute("SELECT COUNT(*) FROM square_payment").fetchone()[0] == 2


def test_reconcile_finds_unpaid_short_over_and_unlinked(fnb):
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000, "fp-a"), pay("P2", 600, "fp-b"), pay("P3", 1500, "fp-c"),
                                            pay("P4", 1000), pay("P5", 400)]), "t1")
    square.link_payment(fnb, "P1", "u1")
    square.link_payment(fnb, "P2", "u2")
    square.link_payment(fnb, "P3", "u3")
    fnb.execute("UPDATE square_payment SET ignored=1 WHERE id='P5'")  # e.g. a drink, not a fee
    r = square.reconcile(fnb, "t1")
    assert (r["expected"], r["taken"], r["difference"], r["count"], r["unlinked"]) == (4000, 4100, 100, 4, 1)
    states = {p["id"]: (p["state"], p["diff"]) for p in r["players"]}
    assert states == {"u1": ("ok", 0), "u2": ("short", -400), "u3": ("over", 500), "u4": ("unpaid", -1000)}
    with pytest.raises(RuleError):
        square.link_payment(fnb, "P4", "nobody")


def test_a_card_seen_before_links_itself_and_other_tournaments_keep_their_payments(fnb):
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000, "fp-bob")]), "t1")
    square.link_payment(fnb, "P1", "u2")  # Bob paid at t1 with this card
    night2 = FakeSquare([pay("P1", 1000, "fp-bob"), pay("P9", 1000, "fp-bob", "2026-01-08T09:00:00Z"),
                         pay("P10", 1000, "fp-alice", "2026-01-08T09:05:00Z")])
    assert square.pull_tournament(fnb, night2, "t3") == {"new": 2, "updated": 0, "elsewhere": 1, "linked": 1}
    rows = {r["id"]: (r["player_id"], r["link"]) for r in fnb.execute("SELECT * FROM square_payment")}
    assert rows == {"P1": ("u2", "manual"), "P9": ("u2", "auto"), "P10": (None, "none")}


def test_a_second_tap_from_the_same_card_stays_unlinked(fnb):
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000, "fp-bob")]), "t1")
    square.link_payment(fnb, "P1", "u2")
    # Bob pays for himself and then for a friend with the same card
    night2 = FakeSquare([pay("P9", 1000, "fp-bob", "2026-01-08T09:00:00Z"),
                         pay("P10", 1000, "fp-bob", "2026-01-08T09:01:00Z")])
    assert square.pull_tournament(fnb, night2, "t3")["linked"] == 1
    rows = {r["id"]: (r["player_id"], r["link"]) for r in fnb.execute("SELECT * FROM square_payment WHERE tournament_id='t3'")}
    assert rows == {"P9": ("u2", "auto"), "P10": (None, "none")}
    assert square.reconcile(fnb, "t3")["unlinked"] == 1


def test_a_card_that_paid_for_different_players_is_never_auto_linked(fnb):
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000, "fp-shared"), pay("P2", 1000, "fp-shared")]), "t1")
    square.link_payment(fnb, "P1", "u2")
    square.link_payment(fnb, "P2", "u1")  # the same card paid for Bob and for Alice
    night2 = FakeSquare([pay("P9", 1000, "fp-shared", "2026-01-08T09:00:00Z")])
    assert square.pull_tournament(fnb, night2, "t3")["linked"] == 0
    assert fnb.execute("SELECT player_id FROM square_payment WHERE id='P9'").fetchone()[0] is None


def test_payments_follow_a_player_merge(fnb):
    square.pull_tournament(fnb, FakeSquare([pay("P9", 1000, "fp", "2026-01-08T09:00:00Z")]), "t3")
    square.link_payment(fnb, "P9", "u5")
    sync.merge_players(fnb, "u1", "u5")
    states = {p["id"]: p["state"] for p in square.reconcile(fnb, "t3")["players"]}
    assert states["u1"] == "ok" and "u5" not in states


def test_square_pages(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/periods/1", data={"entry_fee": "5"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    monkeypatch.delenv("SQUARE_API_KEY", raising=False)
    page = client.get("/tournaments/t1").text
    assert "Set <code>SQUARE_API_KEY</code>" in page and "$20.00</b> expected from 4 attendees" in page
    assert "No Square API key" in client.post("/tournaments/t1/square/pull").text

    # a key without SQUARE_ENV (how production first broke) is refused up front, not sent to the wrong host
    monkeypatch.setenv("SQUARE_API_KEY", "test-token")
    monkeypatch.delenv("SQUARE_ENV", raising=False)
    page = client.get("/tournaments/t1").text
    assert "Set SQUARE_ENV to sandbox or production" in page and "Pull payments from Square</button>" in page
    assert '<button class="sec" disabled>Pull payments' in page
    page = client.post("/tournaments/t1/square/pull").text
    assert "Set SQUARE_ENV to sandbox or production" in page and "SquareConfigError" not in page
    monkeypatch.setenv("SQUARE_ENV", "prod")
    assert "not &#39;prod&#39;" in client.get("/tournaments/t1").text
    monkeypatch.setenv("SQUARE_ENV", "Production ")
    assert "Square: production" in client.get("/tournaments/t1").text

    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    assert "Square: sandbox" in client.get("/tournaments/t1").text
    fake = FakeSquare([pay("P1", 500, "fp-a"), pay("P2", 500, "fp-b")])
    monkeypatch.setattr(main, "HttpSquareClient", lambda: fake)
    page = client.post("/tournaments/t1/square/pull").text
    assert "$10.00</b> taken in 2 payments" in page and "2</b> not linked" in page
    page = client.post("/square-payments/P1/link", data={"player_id": "u1"}).text
    assert "1</b> not linked" in page
    page = client.post("/square-payments/P2/ignore").text
    assert "$5.00</b> taken in 1 payments" in page and "Count" in page
    assert "Square payments" in client.get("/players/u1").text
    assert client.post("/square-payments/nope/ignore").status_code == 404


def test_http_client_pages_and_reports_errors(monkeypatch):
    import httpx
    from app.square_client import HttpSquareClient, MissingSquareToken, SquareConfigError
    monkeypatch.delenv("SQUARE_API_KEY", raising=False)
    with pytest.raises(MissingSquareToken):
        HttpSquareClient()
    monkeypatch.setenv("SQUARE_API_KEY", "tok")
    for env, problem in (("", "Set SQUARE_ENV"), ("prod", "not 'prod'")):
        monkeypatch.setenv("SQUARE_ENV", env)
        with pytest.raises(SquareConfigError, match=problem):
            HttpSquareClient()
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("location_id") == "BAD":
            return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED", "detail": "bad token"}]})
        page2 = request.url.params.get("cursor") == "next"
        body = {"payments": [{"id": "B" if page2 else "A", "status": "COMPLETED", "amount_money": {"amount": 100}}]}
        return httpx.Response(200, json=body if page2 else {**body, "cursor": "next"})

    c = HttpSquareClient(httpx.MockTransport(handler))
    assert [p.id for p in c.list_payments("2026-01-01T00:00:00Z", "2026-01-02T06:00:00Z", "LOC")] == ["A", "B"]
    first = seen[0]
    assert first.url.host == "connect.squareupsandbox.com" and first.url.path == "/v2/payments"
    assert first.headers["authorization"] == "Bearer tok" and first.headers["square-version"]
    assert first.url.params["begin_time"] == "2026-01-01T00:00:00Z" and first.url.params["location_id"] == "LOC"
    with pytest.raises(RuntimeError, match=r"Square \(sandbox\) returned 401: bad token\. Check that SQUARE_API_KEY is a "
                                           r"sandbox access token"):
        c.list_payments("a", "b", "BAD")

    monkeypatch.setenv("SQUARE_ENV", "production")
    seen.clear()
    HttpSquareClient(httpx.MockTransport(handler)).list_payments("a", "b")
    assert seen[0].url.host == "connect.squareup.com"
