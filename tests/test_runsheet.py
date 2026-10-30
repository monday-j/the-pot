from app import bank, runsheet
from tests.test_ledger import NIGHT1, first_night, later_tournament, pull
from tests.test_square import fnb, pay  # noqa: F401 (fixture)
from tests.test_sync import conn  # noqa: F401 (fixture)


def where(c):
    return {t["id"]: stage for stage, cards in runsheet.stages(c).items() for t in cards}


def test_each_tournament_sits_in_the_first_stage_that_applies(fnb, monkeypatch):
    monkeypatch.setattr(bank, "today", lambda: "2026-01-10")
    assert where(fnb) == {"t1": "payout", "t3": "payout"}  # both final, prizes owed

    pull(fnb, "t1", pay("P-a", 1000, "fp-a", NIGHT1))  # a payment to link comes before paying out
    later_tournament(fnb, "t9", "2026-01-15", "u2")  # not happened yet
    fnb.execute("UPDATE payout SET status='paid' WHERE event_id IN (SELECT id FROM event WHERE tournament_id='t3')")
    fnb.execute("UPDATE venue_payment SET status='paid' WHERE tournament_id='t3'")
    lanes = runsheet.stages(fnb)
    assert where(fnb) == {"t1": "collect", "t3": "done", "t9": "waiting"}
    assert lanes["collect"][0]["takings"]["unlinked"] == 1 and lanes["waiting"][0]["upcoming"]

    fnb.execute("UPDATE tournament SET excluded=1 WHERE id='t9'")
    assert "t9" not in where(fnb)


def test_someone_owing_doesnt_hold_a_night_up_but_is_owed_to_us(fnb, monkeypatch):
    monkeypatch.setattr(bank, "today", lambda: "2026-01-11")
    first_night(fnb)  # Bob didn't pay his $10
    assert where(fnb)["t1"] == "payout"
    card = next(t for t in runsheet.stages(fnb)["payout"] if t["id"] == "t1")
    assert card["owed"] == {"cents": 1000, "players": 1}
    owed = runsheet.owed_to_us(fnb)
    assert (owed["cents"], owed["oldest_days"], [p["id"] for p in owed["players"]]) == (1000, 10, ["u2"])
    assert owed["players"][0]["nights"][0]["tournament_name"]


def test_the_bank_tile_estimates_the_balance_and_what_square_still_owes(fnb, monkeypatch):
    monkeypatch.setattr(bank, "today", lambda: "2026-01-11")
    assert runsheet.bank_summary(fnb)["estimate"] is None
    first_night(fnb)
    bank.reconcile(fnb, "2026-01-05", 10000, "opening")
    bank.add_adjustment(fnb, "2026-01-06", -250, "bank_fee")
    bank.add_adjustment(fnb, "2026-01-06", 100, "unexplained", "no idea")
    s = runsheet.bank_summary(fnb)
    assert (s["estimate"], s["days"], s["in_transit"], s["unexplained"]) == (9850, 6, 3000, 100)

    total = runsheet.to_pay(fnb)
    assert total["cents"] == total["players_cents"] + total["venues_cents"] > 0
