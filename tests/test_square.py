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
        """Like Square: only payments created in the range (created_at is compared at second precision)."""
        self.calls.append((begin_time, end_time, location_id))
        return [p for p in self.payments if begin_time <= p.created_at[:19] + "Z" <= end_time]

    payouts: list = []
    entries: dict = {}

    def list_payouts(self, begin_time, end_time, location_id=""):
        self.calls.append(("payouts", begin_time, end_time, location_id))
        return list(self.payouts)

    def list_payout_entries(self, payout_id):
        self.calls.append(("entries", payout_id))
        return list(self.entries.get(payout_id, []))

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
    assert square.pull_tournament(fnb, night2, "t3") == {"new": 2, "updated": 0, "elsewhere": 0, "linked": 1}
    rows = {r["id"]: (r["player_id"], r["link"]) for r in fnb.execute("SELECT * FROM square_payment")}
    assert rows == {"P1": ("u2", "manual"), "P9": ("u2", "auto"), "P10": (None, "none")}


def test_a_payment_counted_for_one_tournament_stays_there(fnb):
    fnb.execute("UPDATE tournament SET start_date='2026-01-01' WHERE id='t3'")  # two events the same day
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000)]), "t1")
    assert square.pull_tournament(fnb, FakeSquare([pay("P1", 1000)]), "t3")["elsewhere"] == 1
    assert fnb.execute("SELECT tournament_id FROM square_payment").fetchone()[0] == "t1"


def test_a_stored_payment_is_refreshed_even_outside_its_window(fnb, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    square.pull_tournament(fnb, FakeSquare([pay("P1", 1000, when="2026-01-01T20:00:00Z")]), "t1")
    fnb.execute("UPDATE tournament SET start_date='2025-12-25' WHERE id='t1'")  # t1's date changed on parry.gg
    later = replace(pay("P1", 1000, when="2026-01-01T20:00:00Z"), fee_cents=22)
    square.pull_series(fnb, FakeSquare([later]), 1)
    assert tuple(fnb.execute("SELECT tournament_id, fee_cents FROM square_payment").fetchone()) == ("t1", 22)


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


class Clock:
    """A fake clock: sleeping just moves time on, and records the wait."""
    def __init__(self):
        self.now, self.waits = 0.0, []

    def sleep(self, seconds):
        self.waits.append(round(seconds, 3))
        self.now += seconds

    def __call__(self):
        return self.now


def square_with(monkeypatch, responses):
    """An HttpSquareClient whose requests get `responses` in turn (a status code, an exception, or a JSON body)."""
    import httpx
    from app.square_client import HttpSquareClient
    monkeypatch.setenv("SQUARE_API_KEY", "tok")
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    queue, clock, calls = list(responses), Clock(), []

    def handler(request):
        calls.append(clock.now)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            status, headers = item
            return httpx.Response(status, headers=headers, json={"errors": [{"code": "RATE_LIMITED"}]})
        if isinstance(item, int):
            return httpx.Response(item, json={"errors": [{"code": "RATE_LIMITED", "detail": "slow down"}]})
        return httpx.Response(200, json=item)

    return HttpSquareClient(httpx.MockTransport(handler), sleep=clock.sleep, clock=clock), clock, calls


def test_rate_limited_requests_back_off_and_retry(monkeypatch, caplog):
    import httpx
    monkeypatch.setattr("random.uniform", lambda a, b: 1.0)  # no jitter, so the waits are exact
    ok = {"payments": [{"id": "A", "status": "COMPLETED", "amount_money": {"amount": 100}}]}
    c, clock, calls = square_with(monkeypatch, [429, 503, httpx.ConnectError("down"), ok])
    with caplog.at_level("WARNING", logger="the_pot.square"):
        assert [p.id for p in c.list_payments("a", "b")] == ["A"]
    assert clock.waits == [1.0, 2.0, 4.0]  # exponential backoff between the four attempts
    assert len(caplog.records) == 3 and "returned 429, retry 1 of 4" in caplog.text and "tok" not in caplog.text


def test_retry_after_is_honoured(monkeypatch):
    ok = {"payments": []}
    c, clock, _ = square_with(monkeypatch, [(429, {"Retry-After": "7"}), ok])
    c.list_payments("a", "b")
    assert clock.waits == [7.0]


def test_gives_up_after_five_rate_limited_attempts(monkeypatch):
    from app.square_client import SquareRateLimited
    c, clock, calls = square_with(monkeypatch, [429] * 5)
    with pytest.raises(SquareRateLimited, match="rate limiting requests; try again in a few minutes"):
        c.list_payments("a", "b")
    assert len(calls) == 5 and len(clock.waits) == 4
    assert all(0.5 * 2 ** i <= w <= 1.5 * 2 ** i for i, w in enumerate(clock.waits))  # backoff with ±50% jitter


def test_persistent_server_errors_and_auth_errors_are_not_hidden(monkeypatch):
    c, _, calls = square_with(monkeypatch, [503] * 5)
    with pytest.raises(RuntimeError, match=r"Square \(sandbox\) returned 503"):
        c.list_payments("a", "b")
    c, _, calls = square_with(monkeypatch, [401])
    with pytest.raises(RuntimeError, match="returned 401"):
        c.list_payments("a", "b")
    assert len(calls) == 1  # a bad token isn't retried


def test_pages_are_paced(monkeypatch):
    page = lambda pid, cursor=None: {"payments": [{"id": pid, "status": "COMPLETED"}], **({"cursor": cursor} if cursor else {})}
    c, clock, calls = square_with(monkeypatch, [page("A", "2"), page("B", "3"), page("C")])
    assert [p.id for p in c.list_payments("a", "b")] == ["A", "B", "C"]
    assert [b - a for a, b in zip(calls, calls[1:])] == [0.25, 0.25]


def test_series_pull_makes_one_query_and_files_payments_by_night(fnb, monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    fnb.execute("INSERT INTO tournament (id, series_id, name, start_date) VALUES ('t4', 1, 'Next day', '2026-01-09')")
    client = FakeSquare([pay("A", 1000, when="2026-01-01T20:00:00Z"),   # night 1
                         pay("B", 1000, when="2026-01-04T12:00:00Z"),   # mid-week: no tournament
                         pay("C", 1000, when="2026-01-08T19:00:00Z"),   # night 2
                         pay("D", 1000, when="2026-01-09T01:00:00Z"),   # small hours after night 2, also t4's day
                         pay("E", 1000, when="2026-01-09T18:00:00Z")])  # t4
    r = square.pull_series(fnb, client, 1)
    assert (r["tournaments"], r["new"]) == (3, 4) and len(client.calls) == 1  # one query for the whole series
    assert client.calls[0][:2] == ("2026-01-01T00:00:00Z", "2026-01-10T06:00:00Z")
    filed = {row[0]: row[1] for row in fnb.execute("SELECT id, tournament_id FROM square_payment")}
    assert filed == {"A": "t1", "C": "t3", "D": "t3", "E": "t4"}
    assert square.pull_series(fnb, client, 1)["updated"] == 4  # idempotent

    client.calls.clear()
    assert square.pull_series(fnb, client, 1, since="2026-01-08")["tournaments"] == 2
    assert client.calls[0][0] == "2026-01-08T00:00:00Z"


def test_series_pull_queries_each_square_location_once(fnb, monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    fnb.execute("INSERT INTO venue (id, name, square_location_id) VALUES (1, 'Hall', 'LOC-HALL'), (2, 'Bar', 'LOC-BAR')")
    fnb.execute("UPDATE series_period SET venue_id=1")
    fnb.execute("UPDATE tournament SET venue_id=2 WHERE id='t3'")  # night 2 held at the bar
    sync.recompute_series(fnb, 1)
    client = FakeSquare()
    square.pull_series(fnb, client, 1)
    assert sorted(c[2] for c in client.calls) == ["LOC-BAR", "LOC-HALL"]


def test_only_one_square_pull_runs_at_a_time(fnb, monkeypatch):
    import threading
    gate, started = threading.Event(), threading.Event()

    class Slow(FakeSquare):
        def list_payments(self, *a):
            started.set()
            gate.wait(5)
            return []

    fnb.commit()  # the worker thread opens its own connection to the test database
    assert square.start_series_pull(1, Slow)
    started.wait(5)
    assert not square.start_series_pull(1, FakeSquare)  # refused while the first is running
    assert square.status[1]["state"] == "running"
    gate.set()
    for _ in range(100):
        if square.status[1]["state"] != "running":
            break
        time.sleep(0.02)
    assert square.status[1]["state"] == "done" and "across 2 tournaments" in square.status[1]["message"]
    assert square.start_series_pull(1, FakeSquare)  # free again


class _session:
    """db.session() stand-in that reuses the test connection."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def test_automatic_pull_covers_recent_tournaments_of_tracked_series(fnb, monkeypatch):
    monkeypatch.setattr(square.db, "session", lambda: _session(fnb))
    monkeypatch.setenv("SQUARE_API_KEY", "tok")
    monkeypatch.delenv("SQUARE_ENV", raising=False)
    client = FakeSquare()
    square.auto_pull_once(lambda: client)
    assert client.calls == []  # Square isn't set up properly: skipped
    monkeypatch.setenv("SQUARE_ENV", "sandbox")

    today = square.datetime.now(square.ZoneInfo(config.TIMEZONE)).date()
    fnb.execute("UPDATE tournament SET start_date=? WHERE id='t3'", ((today - square.timedelta(days=3)).isoformat(),))
    square.auto_pull_once(lambda: client)
    payment_calls = [c for c in client.calls if c[0] not in ("payouts", "entries")]
    assert len(payment_calls) == 1 and payment_calls[0][0].startswith((today - square.timedelta(days=3)).isoformat()[:8])
    assert any(c[0] == "payouts" for c in client.calls)  # then the payouts that paid them out
    assert square.status[1]["auto"] and square.status[1]["message"].endswith("across 1 tournaments")  # t1 is too old

    client.calls.clear()
    fnb.execute("UPDATE series SET tracked=0")
    square.auto_pull_once(lambda: client)
    assert [c for c in client.calls if c[0] not in ("payouts", "entries")] == []


def test_auto_pull_hours_setting(monkeypatch):
    for value, hours in (("", 0.0), ("6", 6.0), ("0.5", 0.5), ("-2", 0.0), ("often", 0.0)):
        monkeypatch.setenv("SQUARE_AUTO_PULL_HOURS", value)
        assert config.square_auto_pull_hours() == hours


def test_series_square_pull_button(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    monkeypatch.delenv("SQUARE_API_KEY", raising=False)
    assert "No Square API key" in client.post("/series/1/square/pull").text
    monkeypatch.setenv("SQUARE_API_KEY", "tok")
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    monkeypatch.setenv("SQUARE_AUTO_PULL_HOURS", "6")
    fake = FakeSquare([pay("P1", 500, when="2026-01-01T09:00:00Z")])
    monkeypatch.setattr(main, "HttpSquareClient", lambda: fake)
    assert "auto-pulls the last 14 days every 6h" in client.get("/series/FNB").text
    client.post("/series/1/square/pull")
    for _ in range(100):
        if square.status.get(1, {}).get("state") not in (None, "running"):
            break
        time.sleep(0.02)
    page = client.get("/series/FNB").text
    assert "Square, " in page and "across 2 tournaments" in page
    assert "Square payments" in client.get("/tournaments/t1").text


def test_parse_payment_sums_processing_fees_or_leaves_them_pending():
    base = {"id": "P", "status": "COMPLETED", "amount_money": {"amount": 1000}}
    fees = [{"type": "INITIAL", "amount_money": {"amount": 20}}, {"type": "ADJUSTMENT", "amount_money": {"amount": 2}}]
    assert parse_payment({**base, "processing_fee": fees}).fee_cents == 22  # as Square's sandbox reports it
    assert parse_payment(base).fee_cents is None and parse_payment({**base, "processing_fee": []}).fee_cents is None


def test_fees_are_filled_in_on_a_later_pull_and_reported_net(fnb):
    from dataclasses import replace
    first = [pay("P1", 1000, "fp-a"), pay("P2", 1000, "fp-b"), pay("P3", 600)]
    square.pull_tournament(fnb, FakeSquare(first), "t1")
    r = square.reconcile(fnb, "t1")
    assert (r["taken"], r["fees"], r["net"], r["fees_pending"]) == (2600, 0, 2600, 3)

    later = [replace(first[0], fee_cents=22), replace(first[1], fee_cents=22), replace(first[2], fee_cents=13)]
    square.pull_tournament(fnb, FakeSquare(later), "t1")  # Square has added its fees since
    fnb.execute("UPDATE square_payment SET ignored=1 WHERE id='P3'")  # not a fee: its card fee isn't counted either
    r = square.reconcile(fnb, "t1")
    assert (r["taken"], r["fees"], r["net"], r["fees_pending"]) == (2000, 44, 1956, 0)


def test_fee_pages(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    monkeypatch.setenv("SQUARE_API_KEY", "tok")
    monkeypatch.setenv("SQUARE_ENV", "sandbox")
    from dataclasses import replace
    fake = FakeSquare([replace(pay("P1", 1000, "fp-a"), fee_cents=22), pay("P2", 1000, "fp-b")])
    monkeypatch.setattr(main, "HttpSquareClient", lambda: fake)
    page = client.post("/tournaments/t1/square/pull").text
    assert "$0.22</b> card fees" in page and "(1 pending)" in page and "$19.78</b> net" in page
    assert ">pending<" in page and "$0.22" in page
    page = client.get("/series/FNB").text
    assert "$20.00</b> taken by card, <b>$0.22</b> fees" in page and "Card fees" in page
