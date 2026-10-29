import pytest

from app import config, db, sync
from app.parry_client import Attendee, EventInfo, PlacementInfo, TournamentInfo, matches_pattern
from app.payouts import splits_to_json
from decimal import Decimal as D


class FakeClient:
    def __init__(self):
        self.searches = []
        self.tournaments = {
            "t1": TournamentInfo("t1", "Friday Night Brawl #90", "friday-night-brawl-90-abc", "2026-01-01", 4,
                                 [EventInfo("e1", "Ultimate Singles", "Smash Ultimate", 4, True, "ultimate-singles")]),
            "t2": TournamentInfo("t2", "Somebody Else's Event", "other-xyz", "2026-01-02", 1, []),
        }
        self.placements = [PlacementInfo("u1", "Alice", 1), PlacementInfo("u2", "Bob", 2),
                           PlacementInfo("u3", "Cat", 3), PlacementInfo("u4", "Dan", 3)]

    # custom slug -> tournaments carrying it ("all" deliberately includes one whose name doesn't match)
    by_slug = {"fnb": ["t1"], "all": ["t1", "t2"]}

    def search_tournaments(self, name="", custom_slug=""):
        self.searches.append((name, custom_slug))
        if custom_slug:
            return [self.tournaments[i] for i in self.by_slug.get(custom_slug, [])]
        return list(self.tournaments.values())

    def get_tournament(self, identifier, is_id=False):
        return self.tournaments[identifier]

    def get_attendees(self, tid):
        if tid != "t1":
            return []
        return [Attendee(p.user_id, p.gamer_tag, ["e1"]) for p in self.placements]

    def get_event_placements(self, eid):
        return self.placements


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pot.db")
    db.init()
    c = db.connect()
    c.execute("INSERT INTO series (id, name, pattern, tracked, entry_fee_cents) VALUES (1,'FNB','friday night brawl',1,0)")
    c.execute("INSERT INTO rule (series_id, game_name, bracket_fee_cents, payout_places, fixed_pool_cents,"
              " bracket_fee_pool_pct, entry_fee_share_pct, splits) VALUES (1,'Smash Ultimate',500,3,1000,'80','0',?)",
              (splits_to_json((D(50), D(30), D(20))),))
    c.commit()
    yield c
    c.close()


def test_matches_pattern():
    assert matches_pattern("Friday Night Brawl", "Friday Night Brawl #90", "")
    assert matches_pattern("friday-night-brawl", "x", "friday-night-brawl-90-abc")
    assert not matches_pattern("Friday Night Brawl", "Other", "other")
    assert not matches_pattern("", "anything", "anything")


def test_sync_imports_matching_tournament_players_and_payouts(conn):
    result = sync.sync_series(conn, FakeClient(), 1)
    conn.commit()
    assert result["tournaments"] == 1
    assert conn.execute("SELECT COUNT(*) FROM player").fetchone()[0] == 4
    # pool = 1000 fixed + 80% of 500*4 = 2600; 3rd/4th tie shares 20%
    rows = {r["player_id"]: r["amount_cents"] for r in conn.execute("SELECT * FROM payout")}
    assert rows == {"u1": 1300, "u2": 780, "u3": 260, "u4": 260}


def test_resync_is_idempotent_and_keeps_paid_amounts(conn):
    client = FakeClient()
    sync.sync_series(conn, client, 1)
    conn.execute("UPDATE payout SET status='paid' WHERE player_id='u1'")
    conn.execute("UPDATE rule SET fixed_pool_cents=2000")
    conn.commit()
    sync.sync_series(conn, client, 1)
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 4
    paid = conn.execute("SELECT * FROM payout WHERE player_id='u1'").fetchone()
    assert paid["amount_cents"] == 1300 and paid["computed_cents"] > 1300  # discrepancy surfaced, not overwritten
    assert conn.execute("SELECT amount_cents FROM payout WHERE player_id='u2'").fetchone()[0] > 780


def test_event_override_beats_series_rule_and_missing_rule_means_no_payouts(conn):
    sync.sync_series(conn, FakeClient(), 1)
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 4
    conn.execute("INSERT INTO rule (series_id, game_name, event_id, bracket_fee_cents, payout_places, splits)"
                 " VALUES (1,'Smash Ultimate','e1',0,1,'[\"100\"]')")
    sync.recompute_series(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 0  # override pool is 0 -> nothing to pay
    conn.execute("DELETE FROM rule")
    sync.recompute_series(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 0


def test_excluded_tournament_is_skipped(conn):
    client = FakeClient()
    conn.execute("INSERT INTO tournament (id, name, excluded) VALUES ('t1','x',1)")
    assert sync.sync_series(conn, client, 1)["tournaments"] == 0


def test_custom_slug_discovers_by_slug_without_pattern_filter(conn):
    conn.execute("UPDATE series SET custom_slug='all', pattern='' WHERE id=1")
    client = FakeClient()
    result = sync.sync_series(conn, client, 1)
    assert client.searches == [("", "all")]
    assert result["tournaments"] == 2  # slug search is trusted, no name matching


def test_pattern_search_used_without_custom_slug(conn):
    client = FakeClient()
    sync.sync_series(conn, client, 1)
    assert client.searches == [("friday night brawl", "")]


def test_init_adds_custom_slug_to_existing_db(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pot.db")
    old = sqlite3.connect(tmp_path / "pot.db")
    old.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pattern TEXT NOT NULL,"
                " tracked INTEGER NOT NULL DEFAULT 0, entry_fee_cents INTEGER NOT NULL DEFAULT 0, last_synced TEXT)")
    old.execute("INSERT INTO series (name, pattern) VALUES ('FNB','x')")
    old.commit()
    old.close()
    db.init()
    c = db.connect()
    assert c.execute("SELECT custom_slug FROM series").fetchone()[0] == ""
    c.close()


def test_placement_issue_reasons():
    assert sync.placement_issue(False, 5, 1) == "Not marked complete on parry.gg"
    assert sync.placement_issue(True, 0, 0) == "No placements recorded"
    assert sync.placement_issue(True, 5, 0) == "No 1st place"
    assert sync.placement_issue(True, 5, 2) == "Tied for 1st place"
    assert sync.placement_issue(True, 5, 1) is None


def test_unfinalised_event_is_flagged_and_gets_no_payouts(conn):
    client = FakeClient()
    sync.sync_series(conn, client, 1)
    assert sync.event_issues(conn, series_id=1) == []
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 4
    # parry.gg drops 1st place (bracket not finished): flagged, unpaid payouts withdrawn
    client.placements = [PlacementInfo("u2", "Bob", 2), PlacementInfo("u3", "Cat", 3)]
    conn.execute("DELETE FROM event_entry WHERE placement=1")
    conn.execute("UPDATE event_entry SET placement=NULL WHERE player_id='u1'")
    sync.recompute_series(conn, 1)
    issues = sync.event_issues(conn, series_id=1)
    assert [i["reason"] for i in issues] == ["No 1st place"]
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 0


def test_incomplete_event_is_flagged(conn):
    client = FakeClient()
    client.tournaments["t1"].events[0].completed = False
    sync.sync_series(conn, client, 1)
    assert [i["reason"] for i in sync.event_issues(conn)] == ["Not marked complete on parry.gg"]
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 0


def set_venue(conn, flat=2000, entry="10", bracket="10", fee=500):
    conn.execute("UPDATE series SET venue_name='The Hall', venue_flat_cents=?, venue_entry_pct=?, "
                 "venue_bracket_pct=?, entry_fee_cents=? WHERE id=1", (flat, entry, bracket, fee))


def venue_row(conn, tid="t1"):
    return conn.execute("SELECT * FROM venue_payment WHERE tournament_id=?", (tid,)).fetchone()


def test_venue_payable_is_created_from_series_settings(conn):
    set_venue(conn)
    sync.sync_series(conn, FakeClient(), 1)
    # flat 2000 + 10% of (500 fee * 4 attendees) + 10% of (500 bracket fee * 4 entrants)
    row = venue_row(conn)
    assert (row["amount_cents"], row["computed_cents"], row["status"]) == (2400, 2400, "owed")
    info = sync.venue_inputs(conn, "t1")
    assert (info["entry_collected"], info["bracket_collected"], info["venue_name"]) == (2000, 2000, "The Hall")
    sync.sync_series(conn, FakeClient(), 1)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM venue_payment").fetchone()[0] == 1


def test_no_venue_cut_means_no_payable(conn):
    sync.sync_series(conn, FakeClient(), 1)
    assert venue_row(conn) is None


def test_paid_venue_amount_survives_changes_and_shows_discrepancy(conn):
    set_venue(conn)
    sync.sync_series(conn, FakeClient(), 1)
    conn.execute("UPDATE venue_payment SET status='paid'")
    set_venue(conn, flat=3000)
    sync.recompute_series(conn, 1)
    row = venue_row(conn)
    assert (row["amount_cents"], row["computed_cents"]) == (2400, 3400)
    conn.execute("UPDATE venue_payment SET status='owed', amount_cents=computed_cents")  # what Undo does
    assert venue_row(conn)["amount_cents"] == 3400


def test_unpaid_venue_row_follows_changes_and_disappears_at_zero(conn):
    set_venue(conn)
    sync.sync_series(conn, FakeClient(), 1)
    set_venue(conn, flat=0, entry="0", bracket="0")
    sync.recompute_series(conn, 1)
    assert venue_row(conn) is None


def test_tournament_override_none_custom_and_excluded(conn):
    set_venue(conn)
    sync.sync_series(conn, FakeClient(), 1)
    conn.execute("UPDATE tournament SET venue_mode='custom', venue_flat_cents=500, venue_entry_pct='0', "
                 "venue_bracket_pct='0' WHERE id='t1'")
    sync.recompute_venue(conn, "t1")
    assert venue_row(conn)["amount_cents"] == 500
    conn.execute("UPDATE tournament SET venue_mode='none' WHERE id='t1'")
    sync.recompute_venue(conn, "t1")
    assert venue_row(conn) is None
    conn.execute("UPDATE tournament SET venue_mode='series' WHERE id='t1'")
    sync.recompute_venue(conn, "t1")
    assert venue_row(conn)["amount_cents"] == 2400
    conn.execute("UPDATE tournament SET excluded=1 WHERE id='t1'")
    sync.recompute_venue(conn, "t1")
    assert venue_row(conn) is None


def test_event_without_rule_adds_no_bracket_fees(conn):
    set_venue(conn)
    conn.execute("DELETE FROM rule")
    sync.sync_series(conn, FakeClient(), 1)
    info = sync.venue_inputs(conn, "t1")
    assert info["bracket_collected"] == 0 and info["unruled"] == ["Ultimate Singles"]
    assert venue_row(conn)["amount_cents"] == 2200  # flat + 10% of entry fees only


def test_event_removed_on_parry_gg_is_pruned(conn):
    client = FakeClient()
    client.tournaments["t1"].events.append(EventInfo("e2", "Ultimate Doubles", "Smash Ultimate", 0, False))
    sync.sync_series(conn, client, 1)
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 2
    client.tournaments["t1"].events.pop()  # the organiser deleted it
    sync.sync_series(conn, client, 1)
    assert [r["id"] for r in conn.execute("SELECT id FROM event")] == ["e1"]
    assert conn.execute("SELECT COUNT(*) FROM payout").fetchone()[0] == 4  # the other event is untouched


def test_removed_event_with_a_paid_payout_is_kept(conn):
    client = FakeClient()
    client.tournaments["t1"].events.append(EventInfo("e2", "Ultimate Doubles", "Smash Ultimate", 4, True))
    sync.sync_series(conn, client, 1)
    assert conn.execute("UPDATE payout SET status='paid' WHERE event_id='e2' AND player_id='u1'").rowcount == 1
    client.tournaments["t1"].events.pop()
    sync.sync_series(conn, client, 1)
    assert {r["id"] for r in conn.execute("SELECT id FROM event")} == {"e1", "e2"}


def test_empty_event_list_from_parry_gg_prunes_nothing(conn):
    client = FakeClient()
    sync.sync_series(conn, client, 1)
    client.tournaments["t1"].events = []
    sync.sync_series(conn, client, 1)
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1


class TwoNightsClient(FakeClient):
    """t1 as usual, plus a second night where Alice played under another account ("alice " / u5)."""
    by_slug = {"fnb": ["t1", "t3"]}

    def __init__(self):
        super().__init__()
        self.tournaments["t3"] = TournamentInfo("t3", "Friday Night Brawl #91", "friday-night-brawl-91-abc",
                                                "2026-01-08", 2, [EventInfo("e3", "Ultimate Singles", "Smash Ultimate", 2, True)])
        self.night2 = [PlacementInfo("u5", "alice ", 1), PlacementInfo("u2", "Bob", 2)]

    def get_attendees(self, tid):
        if tid == "t3":
            return [Attendee(p.user_id, p.gamer_tag, ["e3"]) for p in self.night2]
        return super().get_attendees(tid)

    def get_event_placements(self, eid):
        return self.night2 if eid == "e3" else self.placements


def payouts(conn, event_id):
    return {r["player_id"]: (r["amount_cents"], r["status"])
            for r in conn.execute("SELECT * FROM payout WHERE event_id=?", (event_id,))}


def test_merge_players_combines_history_survives_resync_and_unmerges(conn):
    conn.execute("UPDATE series SET custom_slug='fnb' WHERE id=1")
    client = TwoNightsClient()
    sync.sync_series(conn, client, 1)
    assert payouts(conn, "e3") == {"u5": (900, "owed"), "u2": (540, "owed")}  # 1000 + 80% of 500*2, 50/30 split
    conn.execute("UPDATE payout SET status='paid' WHERE player_id='u5'")

    sync.merge_players(conn, "u1", "u5")
    assert payouts(conn, "e3") == {"u1": (900, "paid"), "u2": (540, "owed")}
    assert payouts(conn, "e1")["u1"] == (1300, "owed")  # the first night is untouched
    assert conn.execute("SELECT COUNT(*) FROM tournament_entry_c WHERE player_id='u1'").fetchone()[0] == 2

    sync.sync_series(conn, client, 1)  # a re-sync imports u5 again but keeps reporting it as u1
    assert payouts(conn, "e3") == {"u1": (900, "paid"), "u2": (540, "owed")}
    assert conn.execute("SELECT merged_into FROM player WHERE id='u5'").fetchone()[0] == "u1"

    assert sync.unmerge_player(conn, "u5") == "u1"
    assert payouts(conn, "e3") == {"u5": (900, "paid"), "u2": (540, "owed")}
    assert payouts(conn, "e1")["u1"] == (1300, "owed")


def test_merge_refuses_players_from_the_same_event(conn):
    conn.execute("UPDATE series SET custom_slug='fnb' WHERE id=1")
    sync.sync_series(conn, TwoNightsClient(), 1)
    with pytest.raises(sync.RuleError, match="both entered"):
        sync.merge_players(conn, "u1", "u2")
    with pytest.raises(sync.RuleError, match="themselves"):
        sync.merge_players(conn, "u1", "u1")
    sync.merge_players(conn, "u1", "u5")
    with pytest.raises(sync.RuleError, match="both entered"):  # u5's history counts as u1's now
        sync.merge_players(conn, "u2", "u1")
    with pytest.raises(sync.RuleError, match="already merged"):
        sync.merge_players(conn, "u3", "u5")
    with pytest.raises(sync.RuleError, match="isn't merged"):
        sync.unmerge_player(conn, "u1")


def test_tag_key_ignores_case_and_punctuation():
    assert sync.tag_key("alice ") == sync.tag_key("Alice") == sync.tag_key("A.lice")
    assert sync.tag_key("Alice") != sync.tag_key("Alicia")
