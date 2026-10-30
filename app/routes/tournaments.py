"""A tournament: its settings, its Square payments and who paid."""

from fastapi import APIRouter, Form, HTTPException, Request

from .. import bank, db, ledger, square, sync
from ..payouts import RuleError, venue_breakdown
from ..square_client import SquareConfigError
from ..web import (EVENT_SUMS, NO_SQUARE, back, dollars_to_cents, one, parse_venue, pick_venue, render, square_client,
                   square_setup, venue_options)

router = APIRouter()


@router.post("/tournaments/{tournament_id}/exclude")
def toggle_exclude(tournament_id: str):
    with db.session() as conn:
        t = one(conn, "SELECT * FROM tournament WHERE id=?", tournament_id)
        if t["locked_by"]:
            return back(f"/tournaments/{tournament_id}", bank.locked_message(conn, t["locked_by"]))
        conn.execute("UPDATE tournament SET excluded = 1 - excluded WHERE id=?", (tournament_id,))
        for e in conn.execute("SELECT id FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
            sync.recompute_event(conn, e["id"])  # excluded tournaments drop their unpaid payouts
        sync.recompute_venue(conn, tournament_id)
    return back(f"/series/{t['series_id']}" if t["series_id"] else "/")


@router.post("/tournaments/{tournament_id}/entry-fee")
def tournament_entry_fee(tournament_id: str, entry_fee: str = Form("")):
    try:
        with db.session() as conn:
            t = one(conn, "SELECT * FROM tournament WHERE id=?", tournament_id)
            square.check_unlocked(conn, tournament_id)
            conn.execute("UPDATE tournament SET entry_fee_cents=? WHERE id=?",
                         (dollars_to_cents(entry_fee) if entry_fee.strip() else None, tournament_id))
            for e in conn.execute("SELECT id FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
                sync.recompute_event(conn, e["id"])
            sync.recompute_venue(conn, tournament_id)
    except RuleError as e:
        return back(f"/tournaments/{tournament_id}", str(e))
    return back(f"/tournaments/{tournament_id}")


@router.post("/tournaments/{tournament_id}/venue")
def tournament_venue(tournament_id: str, venue_mode: str = Form("series"), venue_id: str = Form(""),
                     venue_flat: str = Form("0"), venue_entry_pct: str = Form("0"), venue_bracket_pct: str = Form("0")):
    try:
        if venue_mode not in ("series", "custom", "none"):
            raise RuleError("Unknown venue setting")
        cut = parse_venue(venue_flat, venue_entry_pct, venue_bracket_pct)
        with db.session() as conn:
            one(conn, "SELECT id FROM tournament WHERE id=?", tournament_id)
            square.check_unlocked(conn, tournament_id)
            conn.execute("""UPDATE tournament SET venue_mode=?, venue_id=?, venue_flat_cents=?, venue_entry_pct=?,
                            venue_bracket_pct=? WHERE id=?""",
                         (venue_mode, pick_venue(conn, venue_id), cut.flat, str(cut.entry_pct), str(cut.bracket_pct),
                          tournament_id))
            sync.recompute_venue(conn, tournament_id)
    except RuleError as e:
        return back(f"/tournaments/{tournament_id}", str(e))
    return back(f"/tournaments/{tournament_id}")


@router.post("/tournaments/{tournament_id}/square/pull")
def square_pull(tournament_id: str):
    to = f"/tournaments/{tournament_id}#square"
    with db.session() as conn:
        one(conn, "SELECT id FROM tournament WHERE id=?", tournament_id)
    setup = square_setup()
    if not setup["env"]:
        return back(to, setup["error"] or NO_SQUARE)
    try:
        client = square_client()
        try:
            with db.session() as conn:
                square.pull_tournament(conn, client, tournament_id)
                square.lock_clear_tournaments(conn)
        finally:
            client.close()
    except (SquareConfigError, RuleError) as e:
        return back(f"/tournaments/{tournament_id}", str(e))
    except Exception as e:  # shown in the UI; never includes the token
        return back(f"/tournaments/{tournament_id}", f"{type(e).__name__}: {e}")
    return back(to)


@router.post("/square-payments/{payment_id}/link")
def square_link(payment_id: str, player_id: str = Form("")):
    try:
        with db.session() as conn:
            p = one(conn, "SELECT * FROM square_payment WHERE id=?", payment_id)
            square.link_payment(conn, payment_id, player_id.strip() or None)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(f"/tournaments/{p['tournament_id']}", str(e))
    return back(f"/tournaments/{p['tournament_id']}#square")


@router.post("/square-payments/{payment_id}/{flag}")
def square_flag(payment_id: str, flag: str):
    """Toggle "not a fee" (ignore: not counted as taken) or "no player needed" (counted, needn't be linked; #30)."""
    column = {"ignore": "ignored", "no-player": "no_player"}.get(flag)
    if column is None:
        raise HTTPException(404)
    try:
        with db.session() as conn:
            p = one(conn, "SELECT * FROM square_payment WHERE id=?", payment_id)
            square.check_unlocked(conn, p["tournament_id"])
            conn.execute(f"UPDATE square_payment SET {column} = 1 - {column} WHERE id=?", (payment_id,))
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(f"/tournaments/{p['tournament_id']}", str(e))
    return back(f"/tournaments/{p['tournament_id']}#square")


@router.post("/tournaments/{tournament_id}/entrants/{player_id}/settle")
def settle_entrant(tournament_id: str, player_id: str, method: str = Form(...), amount: str = Form(""),
                   reason: str = Form(""), paid_by: str = Form("")):
    """Settle what an entrant still owes some other way (#37): paid by another player, waived, written off or sorted
    out, or record a bank transfer or cash they paid for it. A blank amount is all they owe."""
    to = f"/tournaments/{tournament_id}#paid"
    try:
        with db.session() as conn:
            one(conn, "SELECT id FROM tournament WHERE id=?", tournament_id)
            c = ledger.books(conn)["charges"].get((tournament_id, ledger.canonical(conn, player_id)))
            cents = dollars_to_cents(amount) if amount.strip() else (c["balance"] if c else 0)
            if method in ledger.RECEIPT_METHODS:
                ledger.add_receipt(conn, player_id, method, cents, bank.today(), tournament_id, note=reason)
            else:
                ledger.settle(conn, tournament_id, player_id, method, cents, reason, paid_by or None)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(to, str(e))
    return back(to)


@router.post("/settlements/{settlement_id}/delete")
def delete_settlement(settlement_id: int):
    with db.session() as conn:
        s = one(conn, "SELECT * FROM settlement WHERE id=?", settlement_id)
        to = f"/tournaments/{s['tournament_id']}#paid" if s["tournament_id"] else "/"
        try:
            ledger.delete_settlement(conn, settlement_id)
        except RuleError as e:
            return back(to, str(e))
    return back(to)


@router.post("/tournaments/{tournament_id}/gap")
def write_off_gap(tournament_id: str, reason: str = Form("")):
    """Write off what's missing from a tournament that nobody named owes (#37; was "I'll cover it", #32)."""
    try:
        with db.session() as conn:
            one(conn, "SELECT id FROM tournament WHERE id=?", tournament_id)
            ledger.write_off_gap(conn, tournament_id, reason)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back(f"/tournaments/{tournament_id}#square", str(e))
    return back(f"/tournaments/{tournament_id}#square")


@router.get("/tournaments/{tournament_id}")
def tournament_page(request: Request, tournament_id: str):
    with db.session() as conn:
        t = one(conn, """SELECT t.*, s.name AS series_name, s.code AS series_code, p.entry_fee_cents AS series_fee,
                                p.starts_on AS period_start, p.venue_id AS series_venue_id, v.name AS series_venue,
                                r.reconciled_on AS locked_on
                         FROM tournament t LEFT JOIN series s ON s.id=t.series_id
                         LEFT JOIN series_period p ON p.id=t.period_id LEFT JOIN venue v ON v.id=p.venue_id
                         LEFT JOIN reconciliation r ON r.id=t.locked_by WHERE t.id=?""", tournament_id)
        flagged = {i["id"]: i["reason"] for i in sync.event_issues(conn, tournament_id=tournament_id)}
        events = []
        for e in conn.execute(f"SELECT e.*, {EVENT_SUMS} FROM event e WHERE e.tournament_id=? ORDER BY e.name",
                              (tournament_id,)):
            info = sync.event_pool(conn, e["id"])
            events.append({"e": e, "pool": info[1] if info else None, "has_rule": info is not None,
                           "issue": flagged.get(e["id"])})
        attendees = {r["id"]: {"id": r["id"], "tag": r["gamer_tag"], "entries": [], "owed": 0, "paid": 0}
                     for r in conn.execute("""SELECT p.id, p.gamer_tag FROM tournament_entry_c te
                                              JOIN player p ON p.id=te.player_id WHERE te.tournament_id=?
                                              ORDER BY p.gamer_tag COLLATE NOCASE""", (tournament_id,))}
        for r in conn.execute("""SELECT ee.player_id, e.id AS eid, e.name AS ename, e.game_name, ee.placement,
                                        po.id AS payout_id, po.amount_cents, po.status
                                 FROM event_entry_c ee JOIN event e ON e.id=ee.event_id
                                 LEFT JOIN payout po ON po.event_id=e.id AND po.player_id=ee.player_id
                                 WHERE e.tournament_id=? ORDER BY e.name""", (tournament_id,)):
            a = attendees.get(r["player_id"])
            if a is None:
                continue
            a["entries"].append(r)
            if r["status"]:
                a[r["status"]] += r["amount_cents"]
        venue = None
        info = sync.venue_inputs(conn, tournament_id)
        if info is not None:
            venue = {"info": info, "parts": venue_breakdown(info["cut"], info["entry_collected"],
                                                            info["bracket_collected"]),
                     "payment": conn.execute("SELECT * FROM venue_payment WHERE tournament_id=?",
                                             (tournament_id,)).fetchone()}
        venues = venue_options(conn)
        recon = square.reconcile(conn, tournament_id)
        players = conn.execute("""SELECT id, gamer_tag FROM player WHERE merged_into IS NULL
                                  ORDER BY gamer_tag COLLATE NOCASE""").fetchall()
    return render(request, "tournament.html", t=t, events=events, attendees=list(attendees.values()), venue=venue,
                  venues=venues, recon=recon, square_setup=square_setup(), meanings=square.MEANINGS, players=players,
                  states=ledger.STATES, settle_methods={**ledger.RECEIPT_METHODS, **ledger.CHARGE_METHODS})
