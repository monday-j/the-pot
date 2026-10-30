"""Series: discovery, settings, terms over time, game rules and pulling a whole series from Square."""

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import codes, config, db, runsheet, square, sync
from ..payouts import Rule, RuleError, parse_splits, splits_to_json
from ..web import (EVENT_SUMS, NO_SQUARE, SERIES_SUMS, TOURNAMENT_SUMS, back, dollars_to_cents, flags_by, one,
                   parry_client, parse_date, pick_venue, render, square_client, square_setup, to_pct, venue_options)

router = APIRouter()


@router.get("/series")
def series_index(request: Request):
    with db.session() as conn:
        issues = sync.event_issues(conn)
        by_series = flags_by(issues, "series_id")
        series = [{**dict(r), "flags": len(by_series.get(r["id"], []))} for r in conn.execute(
            f"SELECT s.*, {SERIES_SUMS} FROM series s ORDER BY s.name")]
    return render(request, "series_list.html", series=series)


def claim_code(conn, code: str, exclude_id: int | None = None) -> str:
    """Validate a three-letter code and make sure no other series uses it."""
    code = codes.normalise_code(code)
    other = conn.execute("SELECT name FROM series WHERE code=? AND id IS NOT ?", (code, exclude_id)).fetchone()
    if other:
        raise RuleError(f"Code {code} is already used by {other['name']}")
    return code


@router.post("/series")
def create_series(name: str = Form(...), pattern: str = Form(""), custom_slug: str = Form(""), code: str = Form("")):
    if not pattern.strip() and not custom_slug.strip():
        return back("/series", "Enter a custom slug or a match pattern")
    try:
        with db.session() as conn:
            code = claim_code(conn, code) if code.strip() else codes.suggest_code(
                name, [r[0] for r in conn.execute("SELECT code FROM series")])
            cur = conn.execute("INSERT INTO series (name, code, pattern, custom_slug) VALUES (?,?,?,?)",
                               (name.strip(), code, pattern.strip(), custom_slug.strip()))
            sync.create_period(conn, cur.lastrowid)
    except RuleError as e:
        return back("/series", str(e))
    return back(f"/series/{code}")


@router.get("/series/{ref}")
def series_page(request: Request, ref: str, period: int | None = None, tab: str = ""):
    """`ref` is the series code (/series/FNB); a numeric id redirects to the code URL. Tabs (#46): tournaments and
    their stage, terms & rules (the default when a `period` is asked for), money, and settings."""
    tab = tab if tab in ("tournaments", "terms", "money", "settings") else "terms" if period else "tournaments"
    with db.session() as conn:
        s = conn.execute("SELECT id, code FROM series WHERE id=?" if ref.isdigit() else
                         "SELECT id, code FROM series WHERE code=?", (int(ref) if ref.isdigit() else ref.upper(),)).fetchone()
        if s is None:
            raise HTTPException(404)
        if ref.isdigit() and s["code"]:
            query = request.url.query
            return RedirectResponse(f"/series/{s['code']}" + (f"?{query}" if query else ""), status_code=307)
        series_id = s["id"]
        s = one(conn, f"SELECT s.*, {SERIES_SUMS} FROM series s WHERE s.id=?", series_id)
        periods = conn.execute("""SELECT p.*, v.name AS venue,
                                         (SELECT COUNT(*) FROM tournament t WHERE t.period_id=p.id) AS tournaments,
                                         (SELECT COUNT(*) FROM rule r WHERE r.period_id=p.id) AS rules
                                  FROM series_period p LEFT JOIN venue v ON v.id=p.venue_id
                                  WHERE p.series_id=? ORDER BY p.starts_on""", (series_id,)).fetchall()
        current = periods[-1] if periods else None
        shown = next((p for p in periods if p["id"] == period), current)
        cards = {c["id"]: c for cs in runsheet.stages(conn, series_id).values() for c in cs}
        tournaments = []
        for t in conn.execute(f"""SELECT t.*, {TOURNAMENT_SUMS} FROM tournament t WHERE t.series_id=?
                                  ORDER BY t.start_date DESC""", (series_id,)).fetchall():
            pools = [sync.event_pool(conn, e["id"]) for e in conn.execute(
                "SELECT id FROM event WHERE tournament_id=?", (t["id"],))]
            tournaments.append({**dict(t), "pool": sum(i[1] for i in pools if i)})
        games = conn.execute(f"""SELECT e.game_name, COUNT(*) AS events, SUM(e.entrant_count) AS entrants,
                                        COUNT(DISTINCT t.id) AS tournaments, SUM(owed) AS owed, SUM(paid) AS paid
                                 FROM (SELECT e.*, {EVENT_SUMS} FROM event e) e
                                 JOIN tournament t ON t.id=e.tournament_id
                                 WHERE t.series_id=? GROUP BY e.game_name ORDER BY e.game_name""",
                             (series_id,)).fetchall()
        shown_id = shown["id"] if shown else None
        game_rules = {r["game_name"]: r for r in conn.execute(
            "SELECT * FROM rule WHERE period_id=? AND event_id IS NULL", (shown_id,))}
        rules = conn.execute("""SELECT r.*, e.name AS event_name, e.tournament_id, t.name AS tournament_name
                                FROM rule r LEFT JOIN event e ON e.id=r.event_id
                                LEFT JOIN tournament t ON t.id=e.tournament_id
                                WHERE r.series_id=? AND (r.period_id=? OR r.event_id IS NOT NULL)
                                ORDER BY r.event_id IS NOT NULL, r.game_name, t.start_date, r.event_id""",
                             (series_id, shown_id)).fetchall()
        events = conn.execute("""SELECT e.id, e.name, t.name AS tname FROM event e JOIN tournament t
                                 ON t.id=e.tournament_id WHERE t.series_id=? ORDER BY t.start_date DESC""",
                              (series_id,)).fetchall()
        venues = venue_options(conn)
    return render(request, "series.html", s=s, tournaments=tournaments, games=games, game_rules=game_rules,
                  rules=rules, events=events, sync_status=sync.status.get(series_id), tab=tab, cards=cards,
                  stages=runsheet.STAGES, venues=venues, periods=periods, current=current, shown=shown,
                  square_status=square.status.get(series_id), square_setup=square_setup(),
                  auto_pull_hours=config.square_auto_pull_hours())


@router.post("/series/{series_id}/settings")
def series_settings(series_id: int, name: str = Form(...), code: str = Form(""), pattern: str = Form(""),
                    custom_slug: str = Form("")):
    if not pattern.strip() and not custom_slug.strip():
        return back(f"/series/{series_id}?tab=settings", "Enter a custom slug or a match pattern")
    try:
        with db.session() as conn:
            current = one(conn, "SELECT id, code FROM series WHERE id=?", series_id)
            code = claim_code(conn, code, series_id) if code.strip() else current["code"]
            conn.execute("UPDATE series SET name=?, code=?, pattern=?, custom_slug=? WHERE id=?",
                         (name.strip(), code, pattern.strip(), custom_slug.strip(), series_id))
    except RuleError as e:
        return back(f"/series/{series_id}?tab=settings", str(e))
    return back(f"/series/{code or series_id}?tab=settings")


@router.post("/series/{series_id}/track")
def toggle_track(series_id: int):
    with db.session() as conn:
        one(conn, "SELECT id FROM series WHERE id=?", series_id)
        conn.execute("UPDATE series SET tracked = 1 - tracked WHERE id=?", (series_id,))
    return back(f"/series/{series_id}")


@router.post("/series/{series_id}/sync")
def sync_now(series_id: int):
    with db.session() as conn:
        one(conn, "SELECT id FROM series WHERE id=?", series_id)
    if not config.api_key():
        return back(f"/series/{series_id}", "No API key configured (PARRY_API_KEY)")
    sync.start_sync(series_id, parry_client, square.lock_clear_tournaments)  # newly clear ones lock
    return back(f"/series/{series_id}")


@router.post("/series/{series_id}/tournaments")
def add_tournament(series_id: int, link: str = Form(...)):
    try:
        client = parry_client()
        try:
            with db.session() as conn:
                one(conn, "SELECT id FROM series WHERE id=?", series_id)
                sync.add_manual_tournament(conn, client, series_id, link)
        finally:
            client.close()
    except Exception as e:
        return back(f"/series/{series_id}", f"{type(e).__name__}: {e}")
    return back(f"/series/{series_id}")


@router.post("/series/{series_id}/periods")
def add_period(series_id: int, starts_on: str = Form(...), venue_id: str = Form(""), entry_fee: str = Form("0"),
               note: str = Form("")):
    """Start new terms from a date, e.g. a move to another venue. Game rules are copied from the terms in
    effect on that date, so only what changed needs editing."""
    try:
        day, fee = parse_date(starts_on), dollars_to_cents(entry_fee)
        with db.session() as conn:
            one(conn, "SELECT id FROM series WHERE id=?", series_id)
            if conn.execute("SELECT 1 FROM series_period WHERE series_id=? AND starts_on=?", (series_id, day)).fetchone():
                raise RuleError(f"Terms already start on {day}")
            before = sync.period_for(conn, series_id, day)
            period_id = sync.create_period(conn, series_id, day, pick_venue(conn, venue_id), fee, note.strip(),
                                           copy_rules_from=before["id"] if before else None)
            sync.recompute_series(conn, series_id)
    except RuleError as e:
        return back(f"/series/{series_id}?tab=terms", str(e))
    return back(f"/series/{series_id}?period={period_id}")


@router.post("/periods/{period_id}")
def update_period(period_id: int, starts_on: str = Form(""), venue_id: str = Form(""), entry_fee: str = Form("0"),
                  note: str = Form("")):
    with db.session() as conn:
        p = one(conn, "SELECT * FROM series_period WHERE id=?", period_id)
    to = f"/series/{p['series_id']}?period={period_id}"
    try:
        day = "" if p["starts_on"] == "" else parse_date(starts_on)  # the first terms always run from the start
        fee = dollars_to_cents(entry_fee)
        with db.session() as conn:
            if conn.execute("SELECT 1 FROM series_period WHERE series_id=? AND starts_on=? AND id != ?",
                            (p["series_id"], day, period_id)).fetchone():
                raise RuleError(f"Terms already start on {day}")
            conn.execute("UPDATE series_period SET starts_on=?, venue_id=?, entry_fee_cents=?, note=? WHERE id=?",
                         (day, pick_venue(conn, venue_id), fee, note.strip(), period_id))
            sync.recompute_series(conn, p["series_id"])
    except RuleError as e:
        return back(to, str(e))
    return back(to)


@router.post("/periods/{period_id}/delete")
def delete_period(period_id: int):
    with db.session() as conn:
        p = one(conn, "SELECT * FROM series_period WHERE id=?", period_id)
        if p["starts_on"] == "":
            return back(f"/series/{p['series_id']}?tab=terms", "The first terms can't be deleted")
        conn.execute("DELETE FROM series_period WHERE id=?", (period_id,))  # and its game rules
        sync.recompute_series(conn, p["series_id"])  # its tournaments fall back to the terms before
    return back(f"/series/{p['series_id']}?tab=terms")


@router.post("/series/{series_id}/square/pull")
def series_square_pull(series_id: int):
    """Pull every tournament of the series in the background (one Square query per location, see square.py)."""
    with db.session() as conn:
        one(conn, "SELECT id FROM series WHERE id=?", series_id)
    setup = square_setup()
    if not setup["env"]:
        return back(f"/series/{series_id}", setup["error"] or NO_SQUARE)
    if not square.start_series_pull(series_id, square_client):
        return back(f"/series/{series_id}", "A Square pull is already running; try again when it finishes")
    return back(f"/series/{series_id}")


@router.post("/series/{series_id}/rules")
def save_rule(series_id: int, game_name: str = Form(""), event_id: str = Form(""), period_id: str = Form(""),
              bracket_fee: str = Form("0"), payout_places: int = Form(...), fixed_pool: str = Form("0"),
              bracket_fee_pool_pct: str = Form("0"), entry_fee_share_pct: str = Form("0"),
              splits: str = Form(...)):
    try:
        rule = Rule(bracket_fee=dollars_to_cents(bracket_fee), payout_places=payout_places,
                    fixed_pool=dollars_to_cents(fixed_pool), bracket_fee_pool_pct=to_pct(bracket_fee_pool_pct),
                    entry_fee_share_pct=to_pct(entry_fee_share_pct), splits=parse_splits(splits))
        rule.validate()
        event_id = event_id or None
        if not event_id and not game_name.strip():
            raise RuleError("Pick a game, or an event for a single-event override")
        with db.session() as conn:
            one(conn, "SELECT id FROM series WHERE id=?", series_id)
            period = conn.execute("SELECT id FROM series_period WHERE series_id=? AND id=?",
                                  (series_id, period_id or None)).fetchone() or sync.period_for(conn, series_id, None)
            if event_id:  # a single-event override applies whatever the terms
                game_name, rule_period = "", None
                conn.execute("DELETE FROM rule WHERE series_id=? AND event_id=?", (series_id, event_id))
            else:
                rule_period = period["id"]
                conn.execute("DELETE FROM rule WHERE period_id=? AND game_name=? AND event_id IS NULL",
                             (rule_period, game_name))
            conn.execute("""INSERT INTO rule (series_id, period_id, game_name, event_id, bracket_fee_cents, payout_places,
                            fixed_pool_cents, bracket_fee_pool_pct, entry_fee_share_pct, splits)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (series_id, rule_period, game_name, event_id, rule.bracket_fee, rule.payout_places,
                          rule.fixed_pool, str(rule.bracket_fee_pool_pct), str(rule.entry_fee_share_pct),
                          splits_to_json(rule.splits)))
            sync.recompute_series(conn, series_id)
    except RuleError as e:
        return back(f"/series/{series_id}" + (f"?period={period_id}" if period_id else ""), str(e))
    return back(f"/series/{series_id}?period={period['id']}")


@router.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int):
    with db.session() as conn:
        r = one(conn, "SELECT * FROM rule WHERE id=?", rule_id)
        conn.execute("DELETE FROM rule WHERE id=?", (rule_id,))
        sync.recompute_series(conn, r["series_id"])
    return back(f"/series/{r['series_id']}" + (f"?period={r['period_id']}" if r["period_id"] else ""))
