"""To pay (#44): what we owe each player and venue, one bank transfer each, and what's been paid."""
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import bank, db, runsheet
from ..payouts import RuleError
from ..web import back, parse_date, render, safe_next, venue_options

router = APIRouter()

HISTORY_LIMIT = 300

# Every payable as one kind of line: a player's payout or a venue's cut of a tournament.
LINES = """SELECT 'player' AS kind, CAST(po.id AS TEXT) AS key, po.player_id AS recipient_id, p.gamer_tag AS recipient,
                  t.id AS tid, t.name AS tname, t.start_date, t.series_id, e.name AS ename, e.game_name, po.placement,
                  po.amount_cents, po.computed_cents, po.status, po.paid_at, po.paid_on, po.note, po.reconciliation_id
           FROM payout po JOIN player p ON p.id=po.player_id JOIN event e ON e.id=po.event_id
           JOIN tournament t ON t.id=e.tournament_id
           UNION ALL
           SELECT 'venue', vp.tournament_id, CAST(vp.venue_id AS TEXT), COALESCE(v.name, '(no venue)'),
                  t.id, t.name, t.start_date, t.series_id, NULL, NULL, NULL,
                  vp.amount_cents, vp.computed_cents, vp.status, vp.paid_at, vp.paid_on, vp.note, vp.reconciliation_id
           FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id LEFT JOIN venue v ON v.id=vp.venue_id"""


def recipients(conn) -> list[dict]:
    """Everyone we owe, most first, each with the lines behind it (oldest first) and what they owe us, if anything."""
    owes_us = {p["id"]: p["cents"] for p in runsheet.owed_to_us(conn)["players"]}
    out = {}
    for r in conn.execute(f"SELECT * FROM ({LINES}) WHERE status='owed' ORDER BY start_date, tname, ename, placement"):
        rid = r["recipient_id"] or "none"
        x = out.setdefault((r["kind"], rid), {"kind": r["kind"], "id": rid, "name": r["recipient"], "lines": [],
                                              "cents": 0, "owes_us": owes_us.get(rid, 0) if r["kind"] == "player"
                                              else 0})
        x["lines"].append(dict(r))
        x["cents"] += r["amount_cents"]
    return sorted(out.values(), key=lambda x: (-x["cents"], x["name"].lower()))


@router.get("/to-pay")
def to_pay_page(request: Request, view: str = "", series: int | None = None, kind: str = "", q: str = "",
                venue: int | None = None, game: str = ""):
    with db.session() as conn:
        last = bank.latest(conn)
        ctx = {"view": "history" if view == "history" else "owed", "bank": runsheet.bank_summary(conn),
               "locked_through": last["reconciled_on"] if last else None, "today": bank.today()}
        if ctx["view"] == "owed":
            ctx["recipients"] = recipients(conn)
            ctx["total"] = sum(x["cents"] for x in ctx["recipients"])
        else:
            sql, args = f"SELECT * FROM ({LINES}) WHERE status='paid'", []
            for clause, value in ((" AND series_id=?", series), (" AND kind=?", kind if kind in ("player", "venue")
                                                                  else ""),
                                  (" AND recipient LIKE ?", f"%{q}%" if q else ""),
                                  (" AND kind='venue' AND recipient_id=?", str(venue) if venue else ""),
                                  (" AND game_name=?", game)):
                if value not in ("", None):
                    sql += clause
                    args.append(value)
            ctx["rows"] = conn.execute(sql + " ORDER BY COALESCE(paid_on, substr(paid_at, 1, 10)) DESC, recipient "
                                             "COLLATE NOCASE, tname LIMIT ?", (*args, HISTORY_LIMIT)).fetchall()
            ctx["paid"] = sum(r["amount_cents"] for r in ctx["rows"])
            ctx["series_opts"] = conn.execute("SELECT id, name, code FROM series ORDER BY name").fetchall()
            ctx["venue_opts"] = venue_options(conn)
            ctx["game_opts"] = [r[0] for r in conn.execute("SELECT DISTINCT game_name FROM event WHERE game_name != '' "
                                                           "ORDER BY 1")]
            ctx.update(f_series=series, f_kind=kind, f_q=q, f_venue=venue, f_game=game, limit=HISTORY_LIMIT)
    return render(request, "topay.html", **ctx)


@router.post("/to-pay/{kind}/{recipient_id}/paid")
def pay_recipient(kind: str, recipient_id: str, expect: int = Form(...), paid_on: str = Form(""),
                  note: str = Form(""), back_to: str = Form("", alias="next")):
    """Mark everything owed to one player or venue paid, sent in one transfer on `paid_on` (blank: today). `expect` is
    the total the page showed: if what's owed has changed since, nothing is marked."""
    to = safe_next(back_to, "/to-pay")
    try:
        with db.session() as conn:
            x = next((r for r in recipients(conn) if r["kind"] == kind and r["id"] == recipient_id), None)
            if x is None:
                raise RuleError("Nothing is owed to them")
            if x["cents"] != expect:
                raise RuleError(f"What's owed to {x['name']} is now {x['cents'] / 100:,.2f}: check it and pay again")
            day = parse_date(paid_on) if paid_on.strip() else bank.today()
            for line in x["lines"]:
                bank.mark_paid(conn, "payout" if kind == "player" else "venue_payment",
                               int(line["key"]) if kind == "player" else line["key"], day, note)
    except RuleError as e:
        return back(to, str(e))
    return back(to)


@router.get("/payouts", include_in_schema=False)
def old_payouts(request: Request, status: str = ""):
    """The Payouts list became To pay and its history (#44)."""
    keep = {k: v for k, v in request.query_params.items() if k in ("series", "game", "q") and v}
    if status == "owed":
        return RedirectResponse("/to-pay", 301)
    return RedirectResponse("/to-pay?" + urlencode({"view": "history", "kind": "player", **keep}), 301)


@router.get("/venue", include_in_schema=False)
def old_venue_payments(request: Request, status: str = ""):
    """The venue payments ledger became To pay and its history (#44)."""
    keep = {k: v for k, v in request.query_params.items() if k in ("series", "venue") and v}
    if status == "owed":
        return RedirectResponse("/to-pay", 301)
    return RedirectResponse("/to-pay?" + urlencode({"view": "history", "kind": "venue", **keep}), 301)
