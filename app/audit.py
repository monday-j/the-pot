"""An audit log of money actions (#48): what was done, when, from where, and to which tournament and player.

Each change to money calls record() in the same transaction, so a change that's rolled back leaves no entry. The
client address comes from the request (set by the middleware in main.py); actions with no request, like the
command line, have none.
"""
from contextvars import ContextVar
from datetime import datetime, timezone

CLIENT: ContextVar[str] = ContextVar("client", default="")

ACTIONS = {
    "payout_paid": "prize marked paid", "payout_owed": "prize marked unpaid",
    "venue_paid": "venue marked paid", "venue_owed": "venue marked unpaid",
    "settled": "entrant settled", "gap_written_off": "missing money written off", "credit_used": "credit not kept",
    "settlement_undone": "settlement undone", "receipt": "payment recorded", "receipt_undone": "payment removed",
    "adjustment": "bank difference explained", "adjustment_changed": "explanation changed",
    "adjustment_removed": "explanation removed", "deposit": "deposit saved", "deposit_removed": "deposit removed",
    "reconciled": "bank reconciled", "reconcile_undone": "reconciliation undone",
}


def record(conn, action: str, cents: int | None = None, detail: str = "", tournament_id: str | None = None,
           player_id: str | None = None, ref: str = ""):
    conn.execute("""INSERT INTO audit_log (at, client, action, tournament_id, player_id, ref, cents, detail)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), CLIENT.get(), action, tournament_id,
                  player_id, ref, cents, detail.strip()))


def entries(conn, tournament_id: str | None = None, player_id: str | None = None, action: str = "",
            limit: int = 300) -> list:
    """The latest entries first, optionally only those about a tournament, a player or one kind of action."""
    return conn.execute("""SELECT a.*, t.name AS tname, p.gamer_tag FROM audit_log a
                           LEFT JOIN tournament t ON t.id=a.tournament_id LEFT JOIN player p ON p.id=a.player_id
                           WHERE (? IS NULL OR a.tournament_id=?) AND (? IS NULL OR a.player_id=?)
                           AND (?='' OR a.action=?) ORDER BY a.id DESC LIMIT ?""",
                        (tournament_id, tournament_id, player_id, player_id, action, action, limit)).fetchall()
