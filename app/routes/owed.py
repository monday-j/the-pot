"""Owed to us (#43): who owes for which nights, how old each debt is, and their credits."""

from fastapi import APIRouter, Form, Request

from .. import bank, db, ledger, runsheet, square
from ..payouts import RuleError
from ..web import back, dollars_to_cents, one, parse_date, render, safe_next

router = APIRouter()

RECENT = 20  # how many recent payments, late payments and credit decisions to list


@router.get("/owed")
def owed_page(request: Request):
    with db.session() as conn:
        b = ledger.books(conn)
        owed = runsheet.owed_to_us(conn, books=b)
        names = {r[0]: r[1] for r in conn.execute("SELECT id, gamer_tag FROM player")}
        tnames = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM tournament")}
        credits = sorted(({"id": pid, "name": names.get(pid, pid), "cents": cents}
                          for pid, cents in b["credit"].items() if cents > 0), key=lambda c: (-c["cents"], c["name"]))
        late = sorted((c for c in b["charges"].values() if c["state"] == "late"),
                      key=lambda c: (c["date"], c["tournament_id"]), reverse=True)[:RECENT]
        late = [{**c, "name": names.get(c["player_id"]), "tournament_name": tnames.get(c["tournament_id"])}
                for c in late]
        receipts = [{**dict(r), "went": [{"tournament_id": tid, "tournament_name": tnames.get(tid), "cents": cents}
                                         for tid, cents, _ in b["receipts"].get(r["id"], [])]}
                    for r in conn.execute("""SELECT r.*, p.gamer_tag FROM receipt r JOIN player p ON p.id=r.player_id
                                             ORDER BY r.received_on DESC, r.id DESC LIMIT ?""", (RECENT,))]
        used = conn.execute("""SELECT s.*, p.gamer_tag FROM settlement s JOIN player p ON p.id=s.player_id
                               WHERE s.method IN ('income', 'refunded') ORDER BY s.settled_on DESC, s.id DESC
                               LIMIT ?""", (RECENT,)).fetchall()
        last = bank.latest(conn)
    return render(request, "owed.html", owed=owed, ages=runsheet.AGES, credits=credits, late=late, receipts=receipts,
                  used=used, locked_through=last["reconciled_on"] if last else None, today=bank.today(),
                  receipt_methods=ledger.RECEIPT_METHODS, credit_methods=ledger.CREDIT_METHODS)


def _day(text: str) -> str:
    return parse_date(text) if text.strip() else bank.today()


@router.post("/owed/{player_id}/receipt")
def record_receipt(player_id: str, method: str = Form(...), amount: str = Form(""), received_on: str = Form(""),
                   reference: str = Form(""), note: str = Form(""), next: str = Form("")):
    """A bank transfer or cash from a player: it pays their oldest debts first, and anything over is credit. A blank
    amount is all they owe."""
    to = safe_next(next, "/owed")
    try:
        with db.session() as conn:
            pid = ledger.canonical(conn, player_id)
            one(conn, "SELECT id FROM player WHERE id=?", pid)
            cents = (dollars_to_cents(amount) if amount.strip()
                     else sum(c["balance"] for c in ledger.owing(ledger.books(conn)).get(pid, [])))
            ledger.add_receipt(conn, pid, method, cents, _day(received_on), reference=reference, note=note)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(to, str(e))
    return back(to)


@router.post("/owed/{player_id}/write-off")
def write_off(player_id: str, reason: str = Form(""), next: str = Form("")):
    """Write off everything a player still owes, with a reason."""
    to = safe_next(next, "/owed")
    try:
        with db.session() as conn:
            pid = ledger.canonical(conn, player_id)
            cs = ledger.owing(ledger.books(conn)).get(pid)
            if not cs:
                raise RuleError("They don't owe anything")
            if not reason.strip():
                raise RuleError("Say why it's written off")
            for c in cs:
                ledger.settle(conn, c["tournament_id"], pid, "written_off", c["balance"], reason)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(to, str(e))
    return back(to)


@router.post("/owed/{player_id}/credit")
def use_credit(player_id: str, method: str = Form(...), amount: str = Form(""), reason: str = Form(""),
               next: str = Form("")):
    """Take a player's credit off them: kept as income, or refunded. A blank amount is all of it."""
    to = safe_next(next, "/owed#credit")
    try:
        with db.session() as conn:
            pid = ledger.canonical(conn, player_id)
            cents = dollars_to_cents(amount) if amount.strip() else ledger.books(conn)["credit"].get(pid, 0)
            ledger.use_credit(conn, pid, method, cents, reason)
    except RuleError as e:
        return back(to, str(e))
    return back(to)


@router.post("/receipts/{receipt_id}/delete")
def delete_receipt(receipt_id: int, next: str = Form("")):
    to = safe_next(next, "/owed#received")
    try:
        with db.session() as conn:
            ledger.delete_receipt(conn, receipt_id)
    except RuleError as e:
        return back(to, str(e))
    return back(to)
