"""Events, games, players, payouts and venues."""

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import bank, db, sync
from ..payouts import RuleError, compute_payouts, pool_breakdown
from ..web import (EVENT_SUMS, SERIES_SUMS, TOURNAMENT_SUMS, back, one, parse_date, parse_venue, render, safe_next,
                   venue_options)

router = APIRouter()


@router.get("/events")
def events_page(request: Request, series: int | None = None, game: str = "", flagged: str = ""):
    with db.session() as conn:
        sql = f"""SELECT e.*, t.name AS tname, t.start_date, t.series_id, s.name AS sname, s.code AS scode, {EVENT_SUMS}
                  FROM event e JOIN tournament t ON t.id=e.tournament_id LEFT JOIN series s ON s.id=t.series_id
                  WHERE t.excluded=0"""
        args = []
        if series is not None:
            sql += " AND t.series_id=?"
            args.append(series)
        if game:
            sql += " AND e.game_name=?"
            args.append(game)
        flags = {i["id"]: i["reason"] for i in sync.event_issues(conn)}
        events = []
        for e in conn.execute(sql + " ORDER BY t.start_date DESC, e.name", args):
            if flagged and e["id"] not in flags:
                continue
            info = sync.event_pool(conn, e["id"])
            events.append({"e": e, "pool": info[1] if info else None, "issue": flags.get(e["id"])})
        series_opts = conn.execute("SELECT id, name, code FROM series ORDER BY name").fetchall()
        game_opts = [r[0] for r in conn.execute("SELECT DISTINCT game_name FROM event ORDER BY 1")]
    return render(request, "events.html", events=events, series_opts=series_opts, game_opts=game_opts,
                  f_series=series, f_game=game, f_flagged=flagged)


@router.get("/events/{event_id}")
def event_page(request: Request, event_id: str):
    with db.session() as conn:
        e = one(conn, """SELECT e.*, t.name AS tname, t.id AS tid, t.slug AS tslug, t.start_date, t.series_id, s.name AS sname, s.code AS scode
                         FROM event e JOIN tournament t ON t.id=e.tournament_id
                         LEFT JOIN series s ON s.id=t.series_id WHERE e.id=?""", event_id)
        info = sync.event_pool(conn, event_id)
        issue = sync.event_issues(conn, event_id=event_id)
        rows = conn.execute("""SELECT p.id AS player_id, p.gamer_tag, ee.placement, po.id AS payout_id,
                                      po.amount_cents, po.computed_cents, po.status, po.paid_at, po.paid_on
                               FROM event_entry_c ee JOIN player p ON p.id=ee.player_id
                               LEFT JOIN payout po ON po.event_id=ee.event_id AND po.player_id=ee.player_id
                               WHERE ee.event_id=?
                               ORDER BY ee.placement IS NULL, ee.placement, p.gamer_tag COLLATE NOCASE""",
                            (event_id,)).fetchall()
    undistributed = breakdown = None
    if info:
        rule, pool, entrants, attendees, fee = info
        breakdown = pool_breakdown(rule, entrants, attendees, fee)
        _, undistributed = compute_payouts(pool, rule.splits,
                                           {r["player_id"]: r["placement"] for r in rows if r["placement"]})
    return render(request, "event.html", e=e, info=info, breakdown=breakdown, rows=rows,
                  undistributed=undistributed, issue=issue[0]["reason"] if issue else None,
                  owed=sum(r["amount_cents"] for r in rows if r["status"] == "owed"),
                  paid=sum(r["amount_cents"] for r in rows if r["status"] == "paid"))


@router.get("/games")
def games_page(request: Request):
    with db.session() as conn:
        games = conn.execute(f"""SELECT e.game_name, COUNT(*) AS events, COUNT(DISTINCT t.id) AS tournaments,
                                        SUM(e.entrant_count) AS entrants, SUM(e.owed) AS owed, SUM(e.paid) AS paid,
                                        MAX(t.start_date) AS last_date
                                 FROM (SELECT e.*, {EVENT_SUMS} FROM event e) e
                                 JOIN tournament t ON t.id=e.tournament_id WHERE t.excluded=0
                                 GROUP BY e.game_name ORDER BY e.game_name""").fetchall()
    return render(request, "games.html", games=games)


@router.get("/games/{name:path}")
def game_page(request: Request, name: str):
    with db.session() as conn:
        events = []
        flags = {i["id"]: i["reason"] for i in sync.event_issues(conn)}
        for e in conn.execute(f"""SELECT e.*, t.name AS tname, t.start_date, t.series_id, s.name AS sname, s.code AS scode, {EVENT_SUMS}
                                  FROM event e JOIN tournament t ON t.id=e.tournament_id
                                  LEFT JOIN series s ON s.id=t.series_id
                                  WHERE e.game_name=? AND t.excluded=0 ORDER BY t.start_date DESC, e.name""",
                              (name,)):
            winners = conn.execute("""SELECT p.id, p.gamer_tag FROM event_entry_c ee JOIN player p ON p.id=ee.player_id
                                      WHERE ee.event_id=? AND ee.placement=1 ORDER BY p.gamer_tag COLLATE NOCASE""",
                                   (e["id"],)).fetchall()
            info = sync.event_pool(conn, e["id"])
            events.append({"e": e, "winners": winners, "pool": info[1] if info else None, "issue": flags.get(e["id"])})
        rules = conn.execute("""SELECT r.*, s.name AS sname, s.code AS scode, e.name AS event_name, e.tournament_id,
                                       p.starts_on AS period_start
                                FROM rule r JOIN series s ON s.id=r.series_id LEFT JOIN event e ON e.id=r.event_id
                                LEFT JOIN series_period p ON p.id=r.period_id
                                WHERE r.game_name=? OR e.game_name=? ORDER BY s.name, r.event_id, p.starts_on""",
                             (name, name)).fetchall()
        players = conn.execute("""SELECT p.id, p.gamer_tag, COUNT(*) AS events, MIN(ee.placement) AS best,
                                         SUM(ee.placement=1) AS wins
                                  FROM event_entry_c ee JOIN player p ON p.id=ee.player_id
                                  JOIN event e ON e.id=ee.event_id WHERE e.game_name=?
                                  GROUP BY p.id ORDER BY wins DESC, events DESC, p.gamer_tag COLLATE NOCASE""",
                               (name,)).fetchall()
    if not events and not rules:
        raise HTTPException(404)
    return render(request, "game.html", game_name=name, events=events, rules=rules, players=players)


@router.get("/payouts")
def payouts_page(request: Request, status: str = "", series: int | None = None, game: str = "", q: str = ""):
    sql = """SELECT po.*, p.gamer_tag, e.id AS eid, e.name AS ename, e.game_name, t.id AS tid, t.name AS tname,
                    t.start_date, t.series_id, s.name AS sname, s.code AS scode
             FROM payout po JOIN player p ON p.id=po.player_id JOIN event e ON e.id=po.event_id
             JOIN tournament t ON t.id=e.tournament_id LEFT JOIN series s ON s.id=t.series_id WHERE 1=1"""
    args = []
    for clause, value in ((" AND po.status=?", status if status in ("owed", "paid") else ""),
                          (" AND t.series_id=?", series), (" AND e.game_name=?", game),
                          (" AND p.gamer_tag LIKE ?", f"%{q}%" if q else "")):
        if value not in ("", None):
            sql += clause
            args.append(value)
    with db.session() as conn:
        rows = conn.execute(sql + " ORDER BY t.start_date DESC, e.name, po.placement, p.gamer_tag COLLATE NOCASE",
                            args).fetchall()
        series_opts = conn.execute("SELECT id, name, code FROM series ORDER BY name").fetchall()
        game_opts = [r[0] for r in conn.execute("SELECT DISTINCT game_name FROM event ORDER BY 1")]
    return render(request, "payouts.html", rows=rows, series_opts=series_opts, game_opts=game_opts,
                  f_status=status, f_series=series, f_game=game, f_q=q,
                  owed=sum(r["amount_cents"] for r in rows if r["status"] == "owed"),
                  paid=sum(r["amount_cents"] for r in rows if r["status"] == "paid"))


@router.get("/players")
def players_page(request: Request, q: str = ""):
    with db.session() as conn:
        # merged players are listed once, under the player they were merged into
        players = conn.execute("""SELECT p.id, p.gamer_tag, COALESCE(te.n, 0) AS tournaments, te.last_played,
                 COALESCE(ee.n, 0) AS events, ee.best,
                 (SELECT GROUP_CONCAT(a.gamer_tag, ', ') FROM player a WHERE a.merged_into=p.id) AS aliases,
                 COALESCE((SELECT SUM(amount_cents) FROM payout WHERE player_id=p.id AND status='owed'),0) AS owed,
                 COALESCE((SELECT SUM(amount_cents) FROM payout WHERE player_id=p.id AND status='paid'),0) AS paid
                 FROM player p
                 LEFT JOIN (SELECT x.player_id, COUNT(*) AS n, MAX(t.start_date) AS last_played FROM tournament_entry_c x
                            JOIN tournament t ON t.id=x.tournament_id GROUP BY x.player_id) te ON te.player_id=p.id
                 LEFT JOIN (SELECT player_id, COUNT(*) AS n, MIN(placement) AS best FROM event_entry_c
                            GROUP BY player_id) ee ON ee.player_id=p.id
                 WHERE p.merged_into IS NULL AND (p.gamer_tag LIKE ? OR EXISTS (
                   SELECT 1 FROM player a WHERE a.merged_into=p.id AND a.gamer_tag LIKE ?))
                 ORDER BY p.gamer_tag COLLATE NOCASE""", (f"%{q}%", f"%{q}%")).fetchall()
        groups = {}
        for p in conn.execute("SELECT id, gamer_tag FROM player WHERE merged_into IS NULL ORDER BY gamer_tag COLLATE NOCASE"):
            groups.setdefault(sync.tag_key(p["gamer_tag"]), []).append(p)
    duplicates = [g for key, g in groups.items() if key and len(g) > 1]
    return render(request, "players.html", players=players, q=q, duplicates=duplicates)


@router.get("/players/{player_id}")
def player_page(request: Request, player_id: str):
    with db.session() as conn:
        p = one(conn, "SELECT * FROM player WHERE id=?", player_id)
        if p["merged_into"]:
            return RedirectResponse(f"/players/{quote(p['merged_into'], safe='')}", status_code=307)
        aliases = conn.execute("SELECT * FROM player WHERE merged_into=? ORDER BY gamer_tag COLLATE NOCASE",
                               (player_id,)).fetchall()
        others = conn.execute("""SELECT id, gamer_tag FROM player WHERE merged_into IS NULL AND id != ?
                                 ORDER BY gamer_tag COLLATE NOCASE""", (player_id,)).fetchall()
        history = conn.execute("""SELECT t.id AS tid, t.name AS tname, t.start_date, t.series_id, s.name AS sname, s.code AS scode,
                                         e.id AS eid, e.name AS ename, e.game_name, ee.placement,
                                         po.id AS payout_id, po.amount_cents, po.computed_cents, po.status, po.paid_at, po.paid_on
                                  FROM event_entry_c ee JOIN event e ON e.id=ee.event_id
                                  JOIN tournament t ON t.id=e.tournament_id LEFT JOIN series s ON s.id=t.series_id
                                  LEFT JOIN payout po ON po.event_id=e.id AND po.player_id=ee.player_id
                                  WHERE ee.player_id=? ORDER BY t.start_date DESC, e.name""", (player_id,)).fetchall()
        tournaments = conn.execute("SELECT COUNT(*) FROM tournament_entry_c WHERE player_id=?", (player_id,)).fetchone()[0]
        square_payments = conn.execute("""SELECT sp.*, t.name AS tname, t.start_date FROM square_payment sp
                                          JOIN tournament t ON t.id=sp.tournament_id JOIN player a ON a.id=sp.player_id
                                          WHERE COALESCE(a.merged_into, a.id)=? AND sp.ignored=0
                                          ORDER BY sp.created_at DESC""", (player_id,)).fetchall()
    owed = sum(h["amount_cents"] for h in history if h["status"] == "owed")
    paid = sum(h["amount_cents"] for h in history if h["status"] == "paid")
    best = min((h["placement"] for h in history if h["placement"]), default=None)
    totals = {}
    for h in history:
        row = totals.setdefault((h["series_id"], h["sname"], h["game_name"]), {
            "series_id": h["series_id"], "sname": h["sname"], "scode": h["scode"], "game": h["game_name"], "events": 0, "wins": 0,
            "best": None, "owed": 0, "paid": 0})
        row["events"] += 1
        row["wins"] += h["placement"] == 1
        if h["placement"]:
            row["best"] = min(row["best"] or h["placement"], h["placement"])
        if h["status"]:
            row[h["status"]] += h["amount_cents"]
    return render(request, "player.html", p=p, history=history, owed=owed, paid=paid, tournaments=tournaments,
                  aliases=aliases, others=others, square_payments=square_payments, best=best, totals=sorted(totals.values(), key=lambda r: (r["sname"] or "", r["game"])))


@router.post("/players/{player_id}/merge")
def merge_player(player_id: str, other: str = Form(...)):
    """Merge `other` into this player (e.g. the same person imported under a second parry.gg account)."""
    try:
        with db.session() as conn:
            sync.merge_players(conn, player_id, other)
    except RuleError as e:
        return back(f"/players/{quote(player_id, safe='')}", str(e))
    return back(f"/players/{quote(player_id, safe='')}")


@router.post("/players/{player_id}/unmerge")
def unmerge_player(player_id: str):
    try:
        with db.session() as conn:
            keep_id = sync.unmerge_player(conn, player_id)
    except RuleError as e:
        return back("/players", str(e))
    return back(f"/players/{quote(keep_id, safe='')}")


def paid_day(text: str) -> str:
    """The day money was sent, from a form (#39): today if blank."""
    return parse_date(text) if text.strip() else bank.today()


@router.post("/payouts/{payout_id}/paid")
def mark_paid(payout_id: int, note: str = Form(""), undo: str = Form(""), next: str = Form(""),
              paid_on: str = Form("")):
    with db.session() as conn:
        po = one(conn, "SELECT * FROM payout WHERE id=?", payout_id)
        to = safe_next(next, f"/players/{po['player_id']}")
        try:
            if undo:
                bank.mark_owed(conn, "payout", payout_id)
            else:
                bank.mark_paid(conn, "payout", payout_id, paid_day(paid_on), note)
        except RuleError as e:
            return back(to, str(e))
    return back(to)


@router.post("/players/{player_id}/settle")
def settle_all(player_id: str, note: str = Form(""), next: str = Form(""), paid_on: str = Form("")):
    to = safe_next(next, f"/players/{player_id}")
    with db.session() as conn:
        one(conn, "SELECT id FROM player WHERE id=?", player_id)
        try:
            day = paid_day(paid_on)
            for r in conn.execute("SELECT id FROM payout WHERE player_id=? AND status='owed'", (player_id,)).fetchall():
                bank.mark_paid(conn, "payout", r["id"], day, note)
        except RuleError as e:
            return back(to, str(e))
    return back(to)


VENUE_SUMS = """(SELECT COUNT(DISTINCT series_id) FROM series_period WHERE venue_id=v.id) AS series,
    (SELECT COUNT(*) FROM tournament t LEFT JOIN series_period p ON p.id=t.period_id
     WHERE COALESCE(t.venue_id, p.venue_id)=v.id AND t.venue_mode != 'none' AND t.excluded=0) AS tournaments,
    (SELECT MAX(t.start_date) FROM tournament t LEFT JOIN series_period p ON p.id=t.period_id
     WHERE COALESCE(t.venue_id, p.venue_id)=v.id AND t.venue_mode != 'none' AND t.excluded=0) AS last_date,
    COALESCE((SELECT SUM(amount_cents) FROM venue_payment WHERE venue_id=v.id AND status='owed'),0) AS owed,
    COALESCE((SELECT SUM(amount_cents) FROM venue_payment WHERE venue_id=v.id AND status='paid'),0) AS paid"""


@router.get("/venues")
def venues_page(request: Request):
    with db.session() as conn:
        venues = conn.execute(f"SELECT v.*, {VENUE_SUMS} FROM venue v ORDER BY v.name COLLATE NOCASE").fetchall()
    return render(request, "venues.html", venues=venues)


def save_venue_form(conn, venue_id, name, notes, flat, entry_pct, bracket_pct, square_location_id="") -> int:
    name = name.strip()
    if not name:
        raise RuleError("Enter a venue name")
    other = conn.execute("SELECT id FROM venue WHERE name=? COLLATE NOCASE AND id IS NOT ?", (name, venue_id)).fetchone()
    if other:
        raise RuleError(f"There is already a venue called {name}")
    cut = parse_venue(flat, entry_pct, bracket_pct)
    values = (name, notes.strip(), cut.flat, str(cut.entry_pct), str(cut.bracket_pct), square_location_id.strip())
    if venue_id is None:
        return conn.execute("""INSERT INTO venue (name, notes, flat_cents, entry_pct, bracket_pct, square_location_id)
                               VALUES (?,?,?,?,?,?)""", values).lastrowid
    conn.execute("""UPDATE venue SET name=?, notes=?, flat_cents=?, entry_pct=?, bracket_pct=?, square_location_id=?
                    WHERE id=?""", (*values, venue_id))
    sync.recompute_venue_tournaments(conn, venue_id)
    return venue_id


@router.post("/venues")
def create_venue(name: str = Form(...), notes: str = Form(""), flat: str = Form("0"), entry_pct: str = Form("0"),
                 bracket_pct: str = Form("0")):
    try:
        with db.session() as conn:
            venue_id = save_venue_form(conn, None, name, notes, flat, entry_pct, bracket_pct)
    except RuleError as e:
        return back("/venues", str(e))
    return back(f"/venues/{venue_id}")


@router.get("/venues/{venue_id}")
def venue_detail(request: Request, venue_id: int):
    with db.session() as conn:
        v = one(conn, f"SELECT v.*, {VENUE_SUMS} FROM venue v WHERE v.id=?", venue_id)
        series = conn.execute(f"""SELECT s.*, {SERIES_SUMS},
                                         (SELECT MIN(starts_on) FROM series_period WHERE series_id=s.id AND venue_id=?)
                                           AS here_from
                                  FROM series s WHERE s.id IN (SELECT series_id FROM series_period WHERE venue_id=?)
                                  ORDER BY s.name""", (venue_id, venue_id)).fetchall()
        tournaments = conn.execute(f"""SELECT t.*, s.name AS series_name, s.code AS series_code, {TOURNAMENT_SUMS}
                                       FROM tournament t LEFT JOIN series s ON s.id=t.series_id
                                       LEFT JOIN series_period p ON p.id=t.period_id
                                       WHERE (COALESCE(t.venue_id, p.venue_id)=? AND t.venue_mode != 'none'
                                              AND t.excluded=0)
                                          OR t.id IN (SELECT tournament_id FROM venue_payment WHERE venue_id=?)
                                       ORDER BY t.start_date DESC""", (venue_id, venue_id)).fetchall()
    return render(request, "venue_detail.html", v=v, series=series, tournaments=tournaments)


@router.post("/venues/{venue_id}")
def update_venue(venue_id: int, name: str = Form(...), notes: str = Form(""), flat: str = Form("0"),
                 entry_pct: str = Form("0"), bracket_pct: str = Form("0"), square_location_id: str = Form("")):
    try:
        with db.session() as conn:
            one(conn, "SELECT id FROM venue WHERE id=?", venue_id)
            save_venue_form(conn, venue_id, name, notes, flat, entry_pct, bracket_pct, square_location_id)
    except RuleError as e:
        return back(f"/venues/{venue_id}", str(e))
    return back(f"/venues/{venue_id}")


@router.get("/venue")
def venue_page(request: Request, status: str = "", series: int | None = None, venue: int | None = None):
    sql = """SELECT vp.*, t.id AS tid, t.name AS tname, t.start_date, t.series_id, s.name AS sname, s.code AS scode,
                    v.name AS venue_name
             FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id
             LEFT JOIN series s ON s.id=t.series_id LEFT JOIN venue v ON v.id=vp.venue_id WHERE 1=1"""
    args = []
    for clause, value in ((" AND vp.status=?", status if status in ("owed", "paid") else ""),
                          (" AND t.series_id=?", series), (" AND vp.venue_id=?", venue)):
        if value not in ("", None):
            sql += clause
            args.append(value)
    with db.session() as conn:
        rows = []
        for r in conn.execute(sql + " ORDER BY t.start_date DESC, t.name", args).fetchall():
            info = sync.venue_inputs(conn, r["tid"])
            rows.append({"r": r, "entry": info["entry_collected"] if info else 0,
                         "bracket": info["bracket_collected"] if info else 0})
        series_opts = conn.execute("SELECT id, name, code FROM series ORDER BY name").fetchall()
        venue_opts = venue_options(conn)
    return render(request, "venue.html", rows=rows, series_opts=series_opts, venue_opts=venue_opts,
                  f_status=status, f_series=series, f_venue=venue,
                  owed=sum(x["r"]["amount_cents"] for x in rows if x["r"]["status"] == "owed"),
                  paid=sum(x["r"]["amount_cents"] for x in rows if x["r"]["status"] == "paid"))


@router.post("/venue/{tournament_id}/paid")
def venue_mark_paid(tournament_id: str, note: str = Form(""), undo: str = Form(""), next: str = Form(""),
                    paid_on: str = Form("")):
    to = safe_next(next, f"/tournaments/{tournament_id}")
    with db.session() as conn:
        one(conn, "SELECT * FROM venue_payment WHERE tournament_id=?", tournament_id)
        try:
            if undo:
                bank.mark_owed(conn, "venue_payment", tournament_id)
            else:
                bank.mark_paid(conn, "venue_payment", tournament_id, paid_day(paid_on), note)
        except RuleError as e:
            return back(to, str(e))
    return back(to)
