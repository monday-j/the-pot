import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, main, sync, web
from tests.conftest import PASSWORD
from tests.test_sync import FakeClient, TwoNightsClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pot.db")
    monkeypatch.setenv("PARRY_API_KEY", "test-key")
    monkeypatch.setattr(web, "GrpcParryClient", FakeClient)
    sync.status.clear()
    with TestClient(main.app, follow_redirects=True) as c:
        assert c.post("/login", data={"password": PASSWORD}).status_code == 200
        yield c


def test_end_to_end_flow(client):
    r = client.post("/series", data={"name": "FNB", "pattern": "friday night brawl"})
    assert r.status_code == 200 and "FNB" in r.text
    r = client.post("/series", data={"name": "Empty"})
    assert "Enter a custom slug or a match pattern" in r.text
    assert client.post("/series", data={"name": "Slug", "custom_slug": "fridaybrawl"}).status_code == 200
    assert client.post("/series/1/track").status_code == 200
    r = client.post("/series/1/rules", data={
        "game_name": "", "event_id": "", "payout_places": 3, "splits": "50,30,20"})
    assert "Pick a game" in r.text  # validation error is shown, not a 500

    assert client.post("/series/1/sync").status_code == 200
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    assert sync.status[1]["state"] == "done", sync.status[1]

    r = client.post("/series/1/rules", data={
        "game_name": "Smash Ultimate", "bracket_fee": "5", "payout_places": 3, "fixed_pool": "10",
        "bracket_fee_pool_pct": "80", "splits": "50,30,20"})
    assert "Smash Ultimate" in r.text

    assert "Alice" in client.get("/players").text
    page = client.get("/players/u1").text
    assert "$13.00" in page and "owed" in page  # 1st of a $26 pool
    client.post("/players/u1/settle", data={"note": "cash"})
    page = client.get("/players/u1").text
    assert "paid" in page and "Mark all owed" not in page
    assert "$26.00" in client.get("/events/e1").text
    assert client.get("/").status_code == 200


def test_bad_rule_splits_show_error(client):
    client.post("/series", data={"name": "FNB", "pattern": "x"})
    r = client.post("/series/1/rules", data={"game_name": "G", "payout_places": 3, "splits": "50,30"})
    assert "split percentages" in r.text


@pytest.fixture
def seeded(client):
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/series/1/rules", data={
        "game_name": "Smash Ultimate", "bracket_fee": "5", "payout_places": 3, "fixed_pool": "10",
        "bracket_fee_pool_pct": "80", "splits": "50,30,20"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    assert sync.status[1]["state"] == "done", sync.status[1]
    return client


@pytest.mark.parametrize("url", ["/", "/series", "/series/1", "/tournaments/t1", "/events/e1", "/events", "/games",
                                 "/games/Smash%20Ultimate", "/players", "/players/u1", "/payouts", "/venues"])
def test_every_page_renders(seeded, url):
    assert seeded.get(url).status_code == 200


def test_records_link_to_each_other(seeded):
    page = seeded.get("/events/e1").text
    for href in ('/players/u1', '/tournaments/t1', '/series/FNB', '/games/Smash%20Ultimate'):
        assert f'href="{href}"' in page
    assert 'href="/events/e1"' in seeded.get("/players/u1").text
    assert 'href="/games/Smash%20Ultimate"' in seeded.get("/payouts").text
    assert 'href="/players/u2"' in seeded.get("/tournaments/t1").text
    assert 'href="/events/e1"' in seeded.get("/games/Smash%20Ultimate").text
    assert 'href="/events/e1"' in seeded.get("/events").text


def test_unknown_game_is_404(seeded):
    assert seeded.get("/games/Nope").status_code == 404


def test_payout_ledger_filters(seeded):
    assert seeded.get("/payouts").text.count("Mark paid") == 4
    seeded.post("/payouts/1/paid", data={"next": "/payouts"})
    owed = seeded.get("/payouts?status=owed").text
    paid = seeded.get("/payouts?status=paid").text
    assert owed.count("Mark paid") == 3 and "Mark paid" not in paid and paid.count("Undo") == 1
    assert "Alice" in seeded.get("/payouts?q=ali").text and "Bob" not in seeded.get("/payouts?q=ali").text
    assert "No payouts match" in seeded.get("/payouts?game=Other").text


def test_inline_payment_returns_to_page_but_only_locally(seeded):
    r = seeded.post("/payouts/1/paid", data={"next": "/events/e1"}, follow_redirects=False)
    assert r.headers["location"] == "/events/e1"
    for bad in ("//evil.example/x", "https://evil.example", "/\\evil.example", ""):
        r = seeded.post("/payouts/2/paid", data={"next": bad, "undo": "1"}, follow_redirects=False)
        assert r.headers["location"].startswith("/players/"), bad



def settings(**kw):
    base = {"name": "FNB", "custom_slug": "fnb", "pattern": ""}
    return {**base, **kw}


def set_terms(client, period_id=1, **kw):
    """Venue and venue fee of a series' terms (the first ones by default)."""
    return client.post(f"/periods/{period_id}", data={"venue_id": "1", "entry_fee": "5", **kw})


def add_venue(client, **kw):
    return client.post("/venues", data={"name": "The Hall", "flat": "20", "entry_pct": "10", "bracket_pct": "10", **kw})


def test_venue_flow(seeded):
    assert "Venue&#39;s % of venue fees must be between 0 and 100" in add_venue(seeded, entry_pct="150").text
    assert "Unknown venue" in set_terms(seeded).text
    add_venue(seeded)
    assert seeded.get("/venue").text.count("Mark paid") == 0
    assert set_terms(seeded).status_code == 200
    page = seeded.get("/venue").text
    assert "The Hall" in page and "$24.00" in page and page.count("Mark paid") == 1
    for url in ("/", "/series/1", "/tournaments/t1", "/venue?status=owed&series=1"):
        assert seeded.get(url).status_code == 200
    assert "owed to venues" in seeded.get("/").text
    assert 'href="/tournaments/t1"' in page

    r = seeded.post("/venue/t1/paid", data={"next": "/venue", "note": "bank"}, follow_redirects=False)
    assert r.headers["location"] == "/venue"
    assert "Undo" in seeded.get("/venue").text and "bank" in seeded.get("/venue?status=paid").text
    assert "Mark paid" not in seeded.get("/venue?status=paid").text
    for bad in ("//evil.example", "https://evil.example", ""):
        r = seeded.post("/venue/t1/paid", data={"next": bad, "undo": "1"}, follow_redirects=False)
        assert r.headers["location"] == "/tournaments/t1", bad
    assert seeded.post("/venue/nope/paid").status_code == 404


def test_venues_show_in_who_is_owed(seeded):
    add_venue(seeded)
    set_terms(seeded)
    page = seeded.get("/").text
    owing = page.split("Who is owed")[1].split("Recent tournaments")[0]
    assert '<a href="/venues/1">The Hall</a> <span class="tag">venue</span>' in owing and "$24.00" in owing
    players = owing.count('href="/players/')
    assert f"owed to {players} players" in page and f'<span class="n">· {players + 1}</span>' in owing
    with db.session() as conn:  # a venue payment whose venue has gone
        conn.execute("UPDATE venue_payment SET venue_id=NULL")
    owing = seeded.get("/").text.split("Who is owed")[1]
    assert '<a href="/venue?status=owed">(no venue)</a> <span class="tag">venue</span>' in owing
    seeded.post("/venue/t1/paid")
    assert "(no venue)" not in seeded.get("/").text


def test_tournament_venue_override(seeded):
    add_venue(seeded)
    set_terms(seeded)
    seeded.post("/tournaments/t1/venue", data={"venue_mode": "none"})
    assert "No venue payments" in seeded.get("/venue").text
    seeded.post("/tournaments/t1/venue", data={"venue_mode": "custom", "venue_flat": "7"})
    assert "$7.00" in seeded.get("/venue").text
    assert "Venue cut for this tournament" in seeded.get("/tournaments/t1").text
    r = seeded.post("/tournaments/t1/venue", data={"venue_mode": "custom", "venue_flat": "-1"})
    assert "cannot be negative" in r.text
    assert "Unknown venue setting" in seeded.post("/tournaments/t1/venue", data={"venue_mode": "x"}).text


def test_init_upgrades_an_old_schema(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pot.db")
    old = sqlite3.connect(tmp_path / "pot.db")
    old.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pattern TEXT NOT NULL,"
                " tracked INTEGER NOT NULL DEFAULT 0, entry_fee_cents INTEGER NOT NULL DEFAULT 0, last_synced TEXT)")
    old.execute("CREATE TABLE tournament (id TEXT PRIMARY KEY, series_id INTEGER, name TEXT NOT NULL, slug TEXT,"
                " start_date TEXT, num_attendees INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0,"
                " manual INTEGER NOT NULL DEFAULT 0, entry_fee_cents INTEGER)")
    old.execute("INSERT INTO series (name, pattern) VALUES ('FNB','x')")
    old.commit()
    old.close()
    db.init()
    db.init()  # running twice is harmless
    c = db.connect()
    row = c.execute("SELECT * FROM series").fetchone()
    assert (row["custom_slug"], row["venue_name"], row["venue_flat_cents"], row["venue_entry_pct"]) == ("", "", 0, "0")
    assert "venue_mode" in {r["name"] for r in c.execute("PRAGMA table_info(tournament)")}
    c.close()


def test_dashboard_shows_total_owed_to_players_and_venues(seeded):
    add_venue(seeded)
    set_terms(seeded)
    page = seeded.get("/").text
    # 4 player payouts ($26 pool) + venue payable ($24.00)
    assert "Total owed" in page and "$50.00" in page and "$26.00 to players + $24.00 to venues" in page


def test_pages_link_out_to_parry_gg(seeded):
    page = seeded.get("/tournaments/t1").text
    assert 'href="https://parry.gg/friday-night-brawl-90-abc"' in page and 'target="_blank"' in page
    page = seeded.get("/events/e1").text
    assert 'href="https://parry.gg/friday-night-brawl-90-abc/ultimate-singles"' in page
    # an event imported before slugs were stored still links to its tournament
    sync_db = db.connect()
    sync_db.execute("UPDATE event SET slug='' WHERE id='e1'")
    sync_db.commit()
    sync_db.close()
    page = seeded.get("/events/e1").text
    assert 'href="https://parry.gg/friday-night-brawl-90-abc"' in page and "ultimate-singles" not in page


def test_series_codes_in_urls_and_links(seeded):
    assert seeded.get("/series/FNB").status_code == 200
    assert seeded.get("/series/fnb").status_code == 200  # case-insensitive
    r = seeded.get("/series/1?error=boom", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/series/FNB?error=boom"
    assert "boom" in seeded.get("/series/1?error=boom").text  # message survives the redirect
    assert seeded.get("/series/ZZZ").status_code == 404
    for url in ("/", "/series", "/events", "/payouts", "/venue", "/players/u1", "/tournaments/t1", "/events/e1"):
        page = seeded.get(url).text
        assert 'href="/series/1"' not in page, url
    assert 'href="/series/FNB"' in seeded.get("/events").text
    assert 'title="FNB"' in seeded.get("/events").text


def test_series_code_rules(seeded):
    assert "exactly three letters" in seeded.post("/series", data={"name": "X", "custom_slug": "x", "code": "AB"}).text
    r = seeded.post("/series", data={"name": "Other Club", "custom_slug": "oc", "code": "oth"})
    assert 'href="/series/OTH"' in seeded.get("/series").text
    assert "already used by Other Club" in seeded.post("/series", data={"name": "Y", "custom_slug": "y", "code": "OTH"}).text
    assert "already used by Other Club" in seeded.post("/series/1/settings", data=settings(code="OTH")).text
    seeded.post("/series/1/settings", data=settings(code="abc"))
    assert seeded.get("/series/ABC").status_code == 200
    seeded.post("/series/1/settings", data=settings())  # blank code keeps the current one
    assert seeded.get("/series/ABC").status_code == 200
    # auto code when left blank, never colliding
    seeded.post("/series", data={"name": "Other Club", "custom_slug": "oc2"})
    codes = [r["code"] for r in db.connect().execute("SELECT code FROM series")]
    assert len(codes) == len(set(codes)) and all(len(c) == 3 for c in codes)


def test_init_backfills_series_codes(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pot.db")
    old = sqlite3.connect(tmp_path / "pot.db")
    old.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pattern TEXT NOT NULL,"
                " tracked INTEGER NOT NULL DEFAULT 0, entry_fee_cents INTEGER NOT NULL DEFAULT 0, last_synced TEXT)")
    for name in ("Friday Night Brawl", "Friday Night Bistro", "The Weekend Warriors"):
        old.execute("INSERT INTO series (name, pattern) VALUES (?, 'x')", (name,))
    old.commit()
    old.close()
    db.init()
    db.init()
    c = db.connect()
    got = [r["code"] for r in c.execute("SELECT code FROM series ORDER BY id")]
    assert got[0] == "FNB" and got[2] == "WWA" and len(set(got)) == 3 and all(len(x) == 3 for x in got)
    c.close()


def test_merge_players_pages(client, monkeypatch):
    monkeypatch.setattr(web, "GrpcParryClient", TwoNightsClient)
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    page = client.get("/players").text
    assert "Possible duplicates" in page and "Merge alice  into Alice" in page

    assert "both entered" in client.post("/players/u1/merge", data={"other": "u2"}).text
    page = client.post("/players/u1/merge", data={"other": "u5"}).text
    assert "Also known as" in page and "Unmerge" in page and "Friday Night Brawl #91" in page
    r = client.get("/players/u5", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/players/u1"
    page = client.get("/players").text
    assert 'href="/players/u5"' not in page and "aka alice" in page and "Possible duplicates" not in page
    assert 'href="/players/u1"' in client.get("/tournaments/t3").text

    page = client.post("/players/u5/unmerge").text
    assert "No other players merged" in page
    assert client.get("/players/u5").status_code == 200


def test_venues_pages_and_one_off_venue(seeded):
    add_venue(seeded)
    assert "already a venue called" in add_venue(seeded, name="the hall").text
    add_venue(seeded, name="Pop-up Bar", flat="50", entry_pct="0", bracket_pct="0")
    set_terms(seeded)
    page = seeded.get("/venues").text
    assert 'href="/venues/1"' in page and 'href="/venues/2"' in page and "$24.00" in page
    page = seeded.get("/venues/1").text
    assert 'href="/tournaments/t1"' in page and 'href="/series/FNB"' in page

    # this week's tournament is held somewhere else
    seeded.post("/tournaments/t1/venue", data={"venue_mode": "series", "venue_id": "2"})
    assert "$50.00" in seeded.get("/venues/2").text and "one-off" in seeded.get("/venues/2").text
    assert "No tournaments held here" in seeded.get("/venues/1").text
    page = seeded.get("/venue?venue=2").text
    assert "Pop-up Bar" in page and "$50.00" in page and "$24.00" not in seeded.get("/venue?venue=2").text


def test_editing_a_venue_reprices_unpaid_but_not_paid(seeded):
    add_venue(seeded)
    set_terms(seeded)
    seeded.post("/venue/t1/paid")
    seeded.post("/venues/1", data={"name": "The Hall", "flat": "30", "entry_pct": "10", "bracket_pct": "10"})
    page = seeded.get("/venues/1").text
    assert "$24.00" in page and "≠ $34.00" in page  # paid amount kept, new maths flagged
    seeded.post("/venue/t1/paid", data={"undo": "1"})
    assert "$34.00" in seeded.get("/venue?status=owed").text
    assert "Enter a venue name" in seeded.post("/venues/1", data={"name": " "}).text
    assert seeded.get("/venues/99").status_code == 404


def test_series_terms_over_time(seeded):
    add_venue(seeded)
    set_terms(seeded)  # The Hall from the start
    add_venue(seeded, name="New Place", flat="30", entry_pct="0", bracket_pct="0")
    assert "is not a date" in seeded.post("/series/1/periods", data={"starts_on": "next week"}).text
    assert "first terms can&#39;t be deleted" in seeded.post("/periods/1/delete").text

    # the series moves before t1 (2026-01-01): t1 now falls under the new terms, with copied rules
    r = seeded.post("/series/1/periods", data={"starts_on": "2025-12-01", "venue_id": "2", "entry_fee": "8",
                                               "note": "moved"})
    assert str(r.url).endswith("/series/FNB?period=2") and "terms from 2025-12-01" in r.text
    assert "Terms already start on 2025-12-01" in seeded.post(
        "/series/1/periods", data={"starts_on": "2025-12-01"}).text
    assert "New Place" in seeded.get("/tournaments/t1").text and "$30.00" in seeded.get("/venue").text
    assert "$26.00" in seeded.get("/events/e1").text  # same rule as before, copied into the new terms

    # change only the new terms' rule; the first terms keep theirs
    seeded.post("/series/1/rules", data={"period_id": "2", "game_name": "Smash Ultimate", "bracket_fee": "5",
                                         "payout_places": 1, "fixed_pool": "0", "splits": "100"})
    assert seeded.get("/payouts").text.count("Mark paid") == 0  # the new rule's pool is $0
    page = seeded.get("/series/FNB?period=1").text
    assert "terms from the start" in page and "50 / 30 / 20" in page
    assert "1 paid (100)" in seeded.get("/games/Smash%20Ultimate").text

    # moving the new terms after t1's date gives t1 its old venue and rule back
    seeded.post("/periods/2", data={"starts_on": "2026-06-01", "venue_id": "2", "entry_fee": "8"})
    assert "The Hall" in seeded.get("/tournaments/t1").text and "$26.00" in seeded.get("/events/e1").text
    assert seeded.get("/payouts").text.count("Mark paid") == 4
    assert "is not a date" in seeded.post("/periods/2", data={"starts_on": ""}).text
    seeded.post("/periods/2/delete")
    assert 'action="/periods/2"' not in seeded.get("/series/FNB").text
    assert seeded.get("/periods/2/delete").status_code == 405
    assert seeded.post("/periods/2/delete").status_code == 404


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon" and r.content[:4] == b"\x00\x00\x01\x00"
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200 and "<svg" in r.text
    page = client.get("/").text
    assert 'href="/static/favicon.svg"' in page and 'href="/favicon.ico"' in page
    assert '<a class="brand" href="/"><img src="/static/favicon.svg" alt=""' in page  # the icon is part of the home link
