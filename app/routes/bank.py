"""Bank reconciliation and deposits."""

from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, Request

from .. import bank, db, square
from ..payouts import RuleError
from ..web import NO_SQUARE, back, dollars_to_cents, one, parse_date, render, square_client, square_setup

router = APIRouter()


@router.get("/bank")
def bank_page(request: Request, on: str = ""):
    today = bank.today()
    try:
        day = parse_date(on) if on else today
    except RuleError:
        day = today
    with db.session() as conn:
        last = bank.latest(conn)
        expected = bank.expected(conn, day)
        history = bank.history(conn)
        deposits = conn.execute("""SELECT d.*, r.reconciled_on FROM bank_deposit d
                                   LEFT JOIN reconciliation r ON r.id=d.reconciliation_id
                                   ORDER BY d.deposited_on DESC, d.id DESC LIMIT 60""").fetchall()
        later = sum(1 for _ in conn.execute("SELECT 1 FROM bank_deposit WHERE reconciliation_id IS NULL AND deposited_on > ?",
                                            (day,)))
    return render(request, "bank.html", last=last, day=day, x=expected, history=history, deposits=deposits,
                  later=later, today=today, categories=bank.CATEGORIES, import_since=(date.fromisoformat(today) - timedelta(days=30)).isoformat(),
                  square_setup=square_setup(), payouts_status=square.status.get("payouts"))


@router.post("/bank/reconcile")
def bank_reconcile(reconciled_on: str = Form(...), balance: str = Form(...), note: str = Form(""),
                   anyway: str = Form("")):
    try:
        with db.session() as conn:
            bank.reconcile(conn, parse_date(reconciled_on), dollars_to_cents(balance), note, anyway=bool(anyway))
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(f"/bank?on={quote(reconciled_on)}", str(e))
    return back("/bank")


@router.post("/bank/reconcile/undo")
def bank_undo():
    try:
        with db.session() as conn:
            bank.undo_latest(conn)
    except RuleError as e:
        return back("/bank", str(e))
    return back("/bank")


@router.post("/bank/adjustments")
def add_adjustment(occurred_on: str = Form(...), amount: str = Form(...), direction: str = Form("in"),
                   category: str = Form(...), note: str = Form("")):
    """Money in or out of the bank that nothing else records (#39)."""
    try:
        cents = abs(dollars_to_cents(amount)) * (-1 if direction == "out" else 1)
        with db.session() as conn:
            bank.add_adjustment(conn, parse_date(occurred_on), cents, category, note)
    except RuleError as e:
        return back(f"/bank?on={quote(occurred_on)}#adjust", str(e))
    return back(f"/bank?on={quote(occurred_on)}")


@router.post("/bank/adjustments/{adjustment_id}")
def update_adjustment(adjustment_id: int, category: str = Form(...), note: str = Form("")):
    try:
        with db.session() as conn:
            bank.update_adjustment(conn, adjustment_id, category, note)
    except RuleError as e:
        return back("/bank", str(e))
    return back("/bank")


@router.post("/bank/adjustments/{adjustment_id}/delete")
def delete_adjustment(adjustment_id: int):
    try:
        with db.session() as conn:
            bank.delete_adjustment(conn, adjustment_id)
    except RuleError as e:
        return back("/bank", str(e))
    return back("/bank")


@router.post("/bank/square/pull")
def import_square_payouts(since: str = Form(...)):
    """Import Square payouts sent since a date, in the background."""
    try:
        day = parse_date(since)
    except RuleError as e:
        return back("/bank", str(e))
    setup = square_setup()
    if not setup["env"]:
        return back("/bank", setup["error"] or NO_SQUARE)
    if not square.start_payouts_pull(square_client, day):
        return back("/bank", "A Square pull is already running; try again when it finishes")
    return back("/bank")


def _deposit_form(conn, deposit_id, deposited_on, amount, reference, note) -> int:
    return bank.save_deposit(conn, deposit_id, parse_date(deposited_on), dollars_to_cents(amount), reference, note)


@router.post("/bank")
def add_deposit(deposited_on: str = Form(...), amount: str = Form(...), reference: str = Form(""), note: str = Form("")):
    try:
        with db.session() as conn:
            deposit_id = _deposit_form(conn, None, deposited_on, amount, reference, note)
    except RuleError as e:
        return back("/bank", str(e))
    return back(f"/bank/{deposit_id}")


@router.get("/bank/{deposit_id}")
def deposit_page(request: Request, deposit_id: int):
    with db.session() as conn:
        d = one(conn, """SELECT d.*, r.reconciled_on FROM bank_deposit d
                         LEFT JOIN reconciliation r ON r.id=d.reconciliation_id WHERE d.id=?""", deposit_id)
        info = bank.summary(conn, d)
    return render(request, "deposit.html", x=info)


@router.post("/bank/{deposit_id}")
def update_deposit(deposit_id: int, deposited_on: str = Form(...), amount: str = Form(...), reference: str = Form(""),
                   note: str = Form("")):
    try:
        with db.session() as conn:
            d = one(conn, "SELECT * FROM bank_deposit WHERE id=?", deposit_id)
            if d["square_payout_id"] or d["reconciliation_id"]:  # Square's, or locked: only the reference and note
                conn.execute("UPDATE bank_deposit SET reference=?, note=? WHERE id=?",
                             (reference.strip(), note.strip(), deposit_id))
            else:
                _deposit_form(conn, deposit_id, deposited_on, amount, reference, note)
    except RuleError as e:
        return back(f"/bank/{deposit_id}", str(e))
    return back(f"/bank/{deposit_id}")


@router.post("/bank/{deposit_id}/delete")
def delete_deposit(deposit_id: int):
    with db.session() as conn:
        d = one(conn, "SELECT * FROM bank_deposit WHERE id=?", deposit_id)
        if d["reconciliation_id"]:
            return back(f"/bank/{deposit_id}", bank.locked_message(conn, d["reconciliation_id"]))
        conn.execute("DELETE FROM bank_deposit WHERE id=?", (deposit_id,))
    return back("/bank")
