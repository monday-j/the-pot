import pytest

from app import bank, ledger, square, sync
from app.payouts import RuleError
from tests.test_square import FakeSquare, fnb, pay  # noqa: F401 (fixture)
from tests.test_sync import conn  # noqa: F401 (fixture)

NIGHT1, NIGHT2 = "2026-01-01T09:00:00Z", "2026-01-08T09:00:00Z"  # t1 and t3: everyone entered owes $10


def pull(c, tid, *payments):
    square.pull_tournament(c, FakeSquare(list(payments)), tid)


def state(c, tid, pid):
    ch = ledger.books(c)["charges"].get((tid, pid))
    return ch and (ch["state"], ch["balance"])


def first_night(c, **links):
    """t1: Alice, Cat and Dan pay their $10 on their own cards (unless given other amounts); Bob pays nothing."""
    amounts = {"u1": 1000, "u3": 1000, "u4": 1000, **links}
    pull(c, "t1", *(pay(f"P-{pid}", cents, f"fp-{pid}", NIGHT1) for pid, cents in amounts.items() if cents))
    for pid, cents in amounts.items():
        if cents:
            square.link_payment(c, f"P-{pid}", pid)


def later_tournament(c, tid, day, *players):
    """Another FNB night (venue fee only: $5 each), with its takings tracked though nothing was pulled."""
    c.execute("""INSERT INTO tournament (id, series_id, name, start_date, period_id, takings)
                 VALUES (?, 1, ?, ?, 1, 'on')""", (tid, f"FNB {day}", day))
    c.executemany("INSERT INTO tournament_entry (tournament_id, player_id) VALUES (?,?)", [(tid, p) for p in players])


def test_extra_pays_the_oldest_week_missed_then_becomes_credit_for_the_next(fnb):
    first_night(fnb)
    assert state(fnb, "t1", "u2") == ("owing", 1000)
    assert [c["tournament_id"] for c in ledger.owing(ledger.books(fnb))["u2"]] == ["t1"]

    # a week later Bob pays $30 on his card: tonight's $10, last week's $10, and $10 over
    pull(fnb, "t3", pay("P-bob", 3000, "fp-u2", NIGHT2), pay("P-u5", 1000, "fp-u5", NIGHT2))
    square.link_payment(fnb, "P-bob", "u2")
    square.link_payment(fnb, "P-u5", "u5")
    b = ledger.books(fnb)
    assert (b["charges"][("t3", "u2")]["state"], b["charges"][("t1", "u2")]["state"]) == ("paid", "late")
    assert b["credit"] == {"u2": 1000}
    assert b["payments"]["P-bob"] == [("t3", 1000, "night"), ("t1", 1000, "late"), (None, 1000, "credit")]
    assert ledger.owing(b) == {}

    later_tournament(fnb, "t9", "2026-01-15", "u2")  # his credit pays the next week's $5
    b = ledger.books(fnb)
    assert (b["charges"][("t9", "u2")]["state"], b["credit"]) == ("credit", {"u2": 500})
    assert ledger.check(fnb) == []


def test_a_payment_can_be_pointed_at_a_week_other_than_the_oldest(fnb):
    first_night(fnb)
    pull(fnb, "t3", pay("P-u5", 1000, "fp-u5", NIGHT2))  # Bob misses this week too
    square.link_payment(fnb, "P-u5", "u5")
    later_tournament(fnb, "t9", "2026-01-15", "u2")
    fnb.execute("UPDATE tournament SET takings='auto' WHERE id='t9'")
    pull(fnb, "t9", pay("P-bob", 1500, "fp-u2", "2026-01-15T09:00:00Z"))
    square.link_payment(fnb, "P-bob", "u2")
    assert (state(fnb, "t1", "u2"), state(fnb, "t3", "u2")) == (("late", 0), ("owing", 1000))  # oldest first

    ledger.point_payment(fnb, "P-bob", "t3", 1000)  # he says it was for last week
    assert (state(fnb, "t1", "u2"), state(fnb, "t3", "u2")) == (("owing", 1000), ("late", 0))
    with pytest.raises(RuleError, match="left to point"):
        ledger.point_payment(fnb, "P-bob", "t1", 1000)  # $10 of it is pointed at t3: only $5 is left

    square.link_payment(fnb, "P-bob", "u3")  # relinking the card doesn't move what was decided by hand
    assert state(fnb, "t3", "u2") == ("late", 0)
    fnb.execute("UPDATE square_payment SET amount_cents=500 WHERE id='P-bob'")  # a partial refund leaves too little
    assert ledger.check(fnb) == ["settlement 1: its payment now counts for less than this"]


def test_paid_for_by_another_player_moves_the_debt_to_them(fnb):
    first_night(fnb, u1=2000, u4=0)  # Alice pays $20: hers and Dan's
    b = ledger.books(fnb)
    assert (b["charges"][("t1", "u4")]["state"], b["credit"]) == ("owing", {"u1": 1000})

    ledger.settle(fnb, "t1", "u4", "other_player", 1000, paid_by="u1")
    b = ledger.books(fnb)
    assert b["charges"][("t1", "u4")]["state"] == "settled"
    assert (b["charges"][("t1", "u1")]["covers"], b["charges"][("t1", "u1")]["state"], b["credit"]) == (1000, "paid", {})
    with pytest.raises(RuleError, match="don't owe"):
        ledger.settle(fnb, "t1", "u4", "waived", 100, reason="again")
    with pytest.raises(RuleError, match="Say why"):
        ledger.settle(fnb, "t1", "u2", "waived", 1000)
    ledger.settle(fnb, "t1", "u2", "written_off", 1000, reason="left before paying")
    assert state(fnb, "t1", "u2") == ("written_off", 0)
    assert ledger.check(fnb) == []


def test_a_bank_transfer_pays_what_they_owe_and_the_rest_is_credit(fnb):
    first_night(fnb)
    ledger.add_receipt(fnb, "u2", "bank", 1500, "2026-01-05", reference="BOB FNB")
    b = ledger.books(fnb)
    assert (b["charges"][("t1", "u2")]["state"], b["charges"][("t1", "u2")]["receipt"]) == ("late", 1000)
    assert b["credit"] == {"u2": 500}
    ledger.use_credit(fnb, "u2", "refunded", 500, "sent back")
    assert ledger.books(fnb)["credit"] == {}
    with pytest.raises(RuleError, match="only have"):
        ledger.use_credit(fnb, "u2", "income", 1)
    assert ledger.check(fnb) == []


def test_payments_without_a_player_cover_the_tournament_as_a_whole(fnb):
    first_night(fnb, u4=0)
    pull(fnb, "t1", pay("P-anon", 1500, when=NIGHT1))  # a $15 tap nobody can be matched to
    b = ledger.books(fnb)
    assert (b["tournaments"]["t1"]["to_link"], state(fnb, "t1", "u2")) == (1, ("unmatched", 1000))
    fnb.execute("UPDATE square_payment SET no_player=1 WHERE id='P-anon'")  # no player needed: it pays $15 of $20 open
    b = ledger.books(fnb)
    assert (b["tournaments"]["t1"]["gap"], state(fnb, "t1", "u4")) == (500, ("unmatched", 1000))
    assert ledger.owing(b) == {}
    ledger.write_off_gap(fnb, "t1", "covered from my pocket")
    assert (ledger.books(fnb)["tournaments"]["t1"]["gap"], state(fnb, "t1", "u4")) == (0, ("covered", 1000))
    with pytest.raises(RuleError, match="Nothing is missing"):
        ledger.write_off_gap(fnb, "t1", "again")


def test_merged_players_share_their_debts_and_payments(fnb):
    first_night(fnb, u1=0)  # Alice misses the first night...
    pull(fnb, "t3", pay("P-u5", 2000, "fp-u5", NIGHT2), pay("P-bob", 1000, "fp-u2", NIGHT2))
    square.link_payment(fnb, "P-u5", "u5")  # ...and pays double the next week under her other account
    square.link_payment(fnb, "P-bob", "u2")
    assert (state(fnb, "t1", "u1"), ledger.books(fnb)["credit"]) == (("owing", 1000), {"u5": 1000})
    sync.merge_players(fnb, "u1", "u5")
    b = ledger.books(fnb)
    assert (b["charges"][("t1", "u1")]["state"], b["charges"][("t3", "u1")]["state"], b["credit"]) == ("late", "paid", {})
    sync.unmerge_player(fnb, "u5")
    assert state(fnb, "t1", "u1") == ("owing", 1000)
    assert ledger.check(fnb) == []


def test_a_locked_tournament_keeps_its_charges_and_its_debts_can_still_be_paid(fnb):
    first_night(fnb)
    bank.reconcile(fnb, "2026-01-02", 0, "")
    fnb.execute("UPDATE tournament SET locked_by=1 WHERE id='t1'")
    ledger.freeze(fnb, "t1")
    fnb.execute("UPDATE series_period SET entry_fee_cents=700")  # the venue fee goes up
    b = ledger.books(fnb)
    assert (b["charges"][("t1", "u2")]["due"], b["charges"][("t1", "u1")]["state"]) == (1000, "paid")
    pull(fnb, "t3", pay("P-bob", 2200, "fp-u2", NIGHT2))
    square.link_payment(fnb, "P-bob", "u2")
    b = ledger.books(fnb)
    assert (b["charges"][("t3", "u2")]["due"], b["charges"][("t1", "u2")]["state"]) == (1200, "late")
    with pytest.raises(RuleError, match="locked"):
        ledger.add_receipt(fnb, "u2", "cash", 100, "2026-01-02")
    assert ledger.check(fnb) == []


def test_tournaments_never_pulled_from_square_charge_nothing_unless_switched_on(fnb):
    assert ledger.books(fnb)["charges"] == {}
    fnb.execute("UPDATE tournament SET takings='on' WHERE id='t1'")
    assert state(fnb, "t1", "u1") == ("owing", 1000)
    first_night(fnb)
    fnb.execute("UPDATE tournament SET takings='off' WHERE id='t1'")
    assert ledger.books(fnb)["charges"] == {}


def test_check_from_the_command_line(fnb, capsys):
    first_night(fnb)
    fnb.commit()
    assert ledger.main(["check"]) == 0
    assert "Books add up." in capsys.readouterr().out
    assert ledger.main([]) == 2
