from app import audit, bank, ledger
from tests.test_ledger import first_night
from tests.test_owed import night  # noqa: F401 (fixture)
from tests.test_square import fnb  # noqa: F401 (fixture)
from tests.test_sync import conn  # noqa: F401 (fixture)
from tests.test_web import client  # noqa: F401 (fixture)


def test_money_actions_are_logged_with_what_they_were_about(fnb):
    first_night(fnb)  # Bob owes $10
    ledger.add_receipt(fnb, "u2", "cash", 1500, "2026-01-05", reference="R1")
    ledger.use_credit(fnb, "u2", "refunded", 500, "gave it back")
    rid = fnb.execute("SELECT id FROM receipt").fetchone()[0]
    ledger.delete_settlement(fnb, fnb.execute("SELECT id FROM settlement").fetchone()[0])
    ledger.delete_receipt(fnb, rid)
    bank.add_adjustment(fnb, "2026-01-06", -250, "bank_fee", "monthly")
    bank.reconcile(fnb, "2026-01-06", 0, "start")
    bank.undo_latest(fnb)
    po = fnb.execute("SELECT id FROM payout LIMIT 1").fetchone()[0]
    bank.mark_paid(fnb, "payout", po, "2026-01-07", "transfer")
    bank.mark_owed(fnb, "payout", po)
    got = [(a["action"], a["cents"], a["player_id"]) for a in reversed(audit.entries(fnb))]
    assert [g[0] for g in got] == ["receipt", "credit_used", "settlement_undone", "receipt_undone", "adjustment",
                                   "reconciled", "reconcile_undone", "payout_paid", "payout_owed"]
    assert got[0] == ("receipt", 1500, "u2") and got[4][1] == -250
    receipt = audit.entries(fnb, action="receipt")[0]
    assert receipt["detail"] == "cash on 2026-01-05, R1" and receipt["tournament_id"] is None
    assert "sent 2026-01-07: transfer" in audit.entries(fnb, action="payout_paid")[0]["detail"]
    assert len(audit.entries(fnb, player_id="u2")) == 4


def test_history_shows_where_actions_came_from(night):
    night.post("/owed/u2/receipt", data={"method": "bank", "reference": "BOB"})
    page = night.get("/history").text
    assert "payment recorded" in page and "testclient" in page and 'href="/players/u2"' in page
    assert "payment recorded" in night.get("/players/u2").text.split('id="log"')[1]
    assert "Nothing recorded yet" in night.get("/history?action=deposit").text
