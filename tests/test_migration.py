from app import bank, ledger, square
from tests.test_ledger import NIGHT1, NIGHT2, pull
from tests.test_square import fnb, pay  # noqa: F401 (fixture)
from tests.test_sync import conn  # noqa: F401 (fixture)


def legacy(c):
    """Decisions made by hand before the ledger, as #29 and #32 stored them."""
    pull(c, "t1", pay("P-alice", 2000, "fp-a", NIGHT1), pay("P-bob", 1000, "fp-b", NIGHT1))
    square.link_payment(c, "P-alice", "u1")  # Alice paid for herself and Dan
    square.link_payment(c, "P-bob", "u2")
    c.executemany("INSERT INTO entrant_override (tournament_id, player_id, note, created_at) VALUES ('t1',?,?,?)",
                  [("u4", "paid by Alice", "2026-01-02T01:00:00+00:00"),
                   ("u1", "paid for Dan", "2026-01-02T01:00:00+00:00")])
    c.execute("UPDATE tournament SET covered_cents=1000, covered_note='my shout' WHERE id='t1'")  # Cat's $10
    # the next week Cat pays what she missed, and a $5 tap nobody can be matched to leaves $5 missing
    pull(c, "t3", pay("P-cat", 1000, "fp-c", NIGHT2), pay("P-bob3", 1000, "fp-b", NIGHT2),
         pay("P-anon", 500, when=NIGHT2))
    square.link_payment(c, "P-cat", "u3")
    c.execute("UPDATE square_payment SET no_player=1 WHERE id='P-anon'")
    c.execute("UPDATE tournament SET covered_cents=500, covered_note='' WHERE id='t3'")
    bank.reconcile(c, "2026-01-05", 0, "")
    c.execute("UPDATE tournament SET locked_by=1 WHERE id='t1'")
    c.execute("DELETE FROM app_state WHERE key=?", (ledger.MIGRATED,))


def test_older_decisions_move_into_the_ledger_once(fnb):
    legacy(fnb)
    ledger.migrate(fnb)
    rows = [tuple(r) for r in fnb.execute(
        "SELECT tournament_id, player_id, method, amount_cents, reason FROM settlement ORDER BY id")]
    assert rows == [("t1", "u4", "other_player", 1000, "paid by Alice"),  # paired with Alice's "paid for Dan"
                    ("t3", None, "written_off", 500, "Organiser covered")]  # Cat paid late, so t1 needs no write-off
    assert fnb.execute("SELECT COUNT(*) FROM charge WHERE tournament_id='t1'").fetchone()[0] == 4  # frozen
    b = ledger.books(fnb)
    assert (b["charges"][("t1", "u3")]["state"], b["charges"][("t1", "u4")]["state"]) == ("late", "settled")
    assert ledger.owing(b) == {} and b["tournaments"]["t3"]["gap"] == 0
    assert ledger.check(fnb) == []

    ledger.migrate(fnb)  # only ever once
    assert fnb.execute("SELECT COUNT(*) FROM settlement").fetchone()[0] == 2
    lines = ledger.report(fnb)
    assert lines[0].endswith("2 settled, 2 not needed.")
    assert "  Friday Night Brawl #90 (2026-01-01): Dan paid by another player (Alice) $10.00 (paid by Alice)" in lines
    assert "  Friday Night Brawl #90 (2026-01-01): (nobody named) not needed, nothing left to write off " \
           "(Organiser covered: my shout)" in lines
    assert "Credit: $0.00 held by 0 player(s)." in lines  # Alice's extra $10 paid for Dan


def test_a_covered_shortfall_writes_off_what_named_players_still_owe(fnb):
    pull(fnb, "t1", pay("P-alice", 1000, "fp-a", NIGHT1))
    square.link_payment(fnb, "P-alice", "u1")  # Bob, Cat and Dan owe $10 each; the organiser covered $25
    fnb.execute("UPDATE tournament SET covered_cents=2500, covered_note='first night' WHERE id='t1'")
    fnb.execute("DELETE FROM app_state WHERE key=?", (ledger.MIGRATED,))
    ledger.migrate(fnb)
    b = ledger.books(fnb)
    assert [(pid, c["balance"]) for pid, cs in ledger.owing(b).items() for c in cs] == [("u4", 500)]
    assert b["charges"][("t1", "u2")]["settled"] == {"written_off": 1000}  # by tag: Bob, Cat, then half of Dan
    fnb.commit()
    assert ledger.main(["report"]) == 0
