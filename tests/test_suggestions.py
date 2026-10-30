import time

from app import ledger, square, sync, web
from tests.test_ledger import NIGHT1, first_night, pull
from tests.test_square import FakeSquare, TwoNightsClient, fnb, oauth_env, pay  # noqa: F401 (fixture)
from tests.test_sync import conn  # noqa: F401 (fixture)
from tests.test_web import client  # noqa: F401 (fixture)


def suggest(c, tid="t1"):
    return [(s["payer"], [p["id"] for p in s["players"]], s["exact"])
            for s in square.suggestions(c, tid, square.reconcile(c, tid))]


def test_extra_money_suggests_who_it_paid_for_exact_first(fnb):
    first_night(fnb, u1=2000)  # Alice paid $20; Bob owes $10
    assert suggest(fnb) == [("u1", ["u2"], True)]

    ledger.settle(fnb, "t1", "u2", "other_player", 1000, paid_by="u1")
    assert suggest(fnb) == [] and ledger.books(fnb)["credit"] == {}


def test_without_an_exact_match_the_closest_under_the_extra_is_offered(fnb):
    first_night(fnb, u1=1500, u3=0)  # Alice paid $5 extra; Bob and Cat owe $10 each
    assert suggest(fnb) == []  # nobody's $10 fits in $5
    first_night(fnb, u1=2500, u3=0)
    assert suggest(fnb) == [("u1", ["u2"], False)]  # Bob ($10) of her $15; Cat too would be $20


def test_someone_paid_for_before_comes_first(fnb):
    first_night(fnb, u1=2000, u3=0)  # Alice $10 extra; Bob and Cat owe $10 each
    assert [s[1] for s in suggest(fnb)] == [["u2"], ["u3"]]  # both exact
    fnb.execute("""INSERT INTO settlement (tournament_id, player_id, method, amount_cents, paid_by, reason, settled_on,
                   created_at) VALUES ('t3','u3','other_player',100,'u1','','2026-01-08','x')""")
    assert [s[1] for s in suggest(fnb)] == [["u3"], ["u2"]]  # Alice has paid for Cat before


def test_confirming_a_suggestion_on_the_page(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/periods/1", data={"entry_fee": "5"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    oauth_env(monkeypatch, "sandbox")
    monkeypatch.setattr(web, "HttpSquareClient", lambda: FakeSquare([pay("P1", 1000, "fp-a", NIGHT1)]))
    client.post("/tournaments/t1/square/pull")
    client.post("/square-payments/P1/link", data={"player_id": "u1"})  # Alice paid $10: $5 for her, $5 extra
    page = client.get("/tournaments/t1").text
    assert "Paid for someone?" in page and "paid $5.00 more than they owed" in page
    form = page.split('action="/tournaments/t1/paid-for"')[1].split("</form>")[0]
    pid = form.split('name="player_id" value="')[1].split('"')[0]
    r = client.post("/tournaments/t1/paid-for", data={"paid_by": "u1", "player_id": pid}, follow_redirects=False)
    assert r.headers["location"] == "/tournaments/t1#takings"
    page = client.get("/tournaments/t1").text
    assert "Paid for someone?" not in page and "paid by another player $5.00 by Alice" in page
    page = client.post("/tournaments/t1/paid-for", data={"paid_by": "u1", "player_id": pid}).text
    assert "no longer a suggestion" in page


def test_a_locked_tournament_shows_only_what_can_still_be_done(client, monkeypatch):  # noqa: F811
    from app import bank, db
    monkeypatch.setattr(web, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/periods/1", data={"entry_fee": "5"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    oauth_env(monkeypatch, "sandbox")
    monkeypatch.setattr(web, "HttpSquareClient", lambda: FakeSquare([pay("P1", 1000, "fp-a", NIGHT1)]))
    client.post("/tournaments/t1/square/pull")
    client.post("/square-payments/P1/link", data={"player_id": "u1"})
    with db.session() as c:
        rid = bank.reconcile(c, "2026-01-05", 0, "")
        c.execute("UPDATE tournament SET locked_by=? WHERE id='t1'", (rid,))
    page = client.get("/tournaments/t1").text
    for gone in ("Pull payments from Square", 'action="/square-payments/', 'action="/tournaments/t1/entry-fee"',
                 'action="/tournaments/t1/exclude"'):
        assert gone not in page, gone
    assert '<a href="/players/u1">Alice</a>' in page.split("Square payments")[1]
    assert "These settings are locked by the" in page
    assert 'action="/tournaments/t1/paid-for"' in page and "Settle</summary>" in page  # still possible
