import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import bank, codes, config, db, square, sync
from .parry_client import GrpcParryClient
from .square_client import HttpSquareClient, SquareConfigError, environment as square_environment
from .payouts import (Rule, RuleError, VenueCut, compute_payouts, parse_splits, pool_breakdown, splits_from_json,
                      splits_to_json, venue_breakdown)

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    db.init()
    stop = threading.Event()
    hours = config.square_auto_pull_hours()
    if hours:  # one worker only (see the Dockerfile), so a single timer thread
        threading.Thread(target=square.auto_pull_loop, args=(stop, lambda: HttpSquareClient(), hours),
                         daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="The Pot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def money(cents) -> str:
    cents = int(cents or 0)
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def ordinal(n) -> str:
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def localtime(stamp: str) -> str:
    """A Square RFC 3339 UTC time in the tournaments' local time zone (TZ)."""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    return when.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M")


templates.env.filters["money"] = money
templates.env.filters["localtime"] = localtime
templates.env.filters["ordinal"] = ordinal
templates.env.filters["fromjson"] = lambda text: [str(x) for x in splits_from_json(text)]
templates.env.filters["urlq"] = lambda text: quote(str(text), safe="")
templates.env.globals["has_key"] = lambda: bool(config.api_key())


def dollars_to_cents(text: str) -> int:
    try:
        return int((Decimal(text.strip() or "0") * 100).to_integral_value())
    except InvalidOperation:
        raise RuleError(f"'{text}' is not a valid amount")


def parse_venue(flat: str, entry_pct: str, bracket_pct: str) -> VenueCut:
    cut = VenueCut(dollars_to_cents(flat), to_pct(entry_pct), to_pct(bracket_pct))
    cut.validate()
    return cut


def parse_date(text: str) -> str:
    try:
        return date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        raise RuleError(f"'{text}' is not a date (use YYYY-MM-DD)")


def pick_venue(conn, text: str) -> int | None:
    """A venue id from a form select ("" means none)."""
    if not text.strip():
        return None
    row = conn.execute("SELECT id FROM venue WHERE id=?", (text.strip(),)).fetchone()
    if row is None:
        raise RuleError("Unknown venue")
    return row["id"]


def venue_options(conn):
    return conn.execute("SELECT id, name FROM venue ORDER BY name COLLATE NOCASE").fetchall()


def to_pct(text: str) -> Decimal:
    try:
        return Decimal(text.strip() or "0")
    except InvalidOperation:
        raise RuleError(f"'{text}' is not a valid percentage")


def back(url: str, error: str | None = None):
    if error:
        url, hash_, fragment = url.partition("#")
        url += ("&" if "?" in url else "?") + "error=" + quote(error) + hash_ + fragment
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, **ctx):
    keep = [(k, v) for k, v in request.query_params.multi_items() if k != "error"]
    here = request.url.path + ("?" + urlencode(keep) if keep else "")  # where inline actions return to
    return templates.TemplateResponse(request, name, {"error": request.query_params.get("error"), "here": here, **ctx})


def safe_next(target: str, default: str) -> str:
    """Only follow same-site paths, never absolute or protocol-relative URLs."""
    ok = target.startswith("/") and not target.startswith(("//", "/\\"))
    return target if ok else default


def describe_rule(r) -> str:
    splits = "/".join(str(x) for x in splits_from_json(r["splits"]))
    bits = [f"{money(r['bracket_fee_cents'])} bracket fee"] if r["bracket_fee_cents"] else []
    if r["fixed_pool_cents"]:
        bits.append(f"{money(r['fixed_pool_cents'])} fixed")
    if r["bracket_fee_cents"] and r["bracket_fee_pool_pct"] not in ("0", "0.0"):
        bits.append(f"{r['bracket_fee_pool_pct']}% of bracket fees")
    if r["entry_fee_share_pct"] not in ("0", "0.0"):
        bits.append(f"{r['entry_fee_share_pct']}% of entry fees")
    return ", ".join(bits + [f"{r['payout_places']} paid ({splits})"])


templates.env.globals["describe_rule"] = describe_rule


def flags_by(issues: list, key: str) -> dict:
    out = {}
    for i in issues:
        out.setdefault(i[key], []).append(i)
    return out


EVENT_SUMS = """COALESCE((SELECT SUM(amount_cents) FROM payout WHERE event_id=e.id AND status='owed'),0) AS owed,
                COALESCE((SELECT SUM(amount_cents) FROM payout WHERE event_id=e.id AND status='paid'),0) AS paid"""
TOURNAMENT_SUMS = """(SELECT COUNT(*) FROM event e WHERE e.tournament_id=t.id) AS events,
    (SELECT SUM(amount_cents) FROM square_payment WHERE tournament_id=t.id AND ignored=0) AS square_taken,
    (SELECT SUM(fee_cents) FROM square_payment WHERE tournament_id=t.id AND ignored=0) AS square_fees,
    (SELECT amount_cents FROM venue_payment WHERE tournament_id=t.id) AS venue_amount,
    (SELECT computed_cents FROM venue_payment WHERE tournament_id=t.id) AS venue_computed,
    (SELECT status FROM venue_payment WHERE tournament_id=t.id) AS venue_status,
    COALESCE((SELECT SUM(po.amount_cents) FROM payout po JOIN event e ON e.id=po.event_id
              WHERE e.tournament_id=t.id AND po.status='owed'),0) AS owed,
    COALESCE((SELECT SUM(po.amount_cents) FROM payout po JOIN event e ON e.id=po.event_id
              WHERE e.tournament_id=t.id AND po.status='paid'),0) AS paid"""
SERIES_SUMS = """(SELECT COUNT(*) FROM tournament t WHERE t.series_id=s.id) AS n,
    COALESCE((SELECT SUM(sp.amount_cents) FROM square_payment sp JOIN tournament t ON t.id=sp.tournament_id
              WHERE t.series_id=s.id AND sp.ignored=0),0) AS square_taken,
    COALESCE((SELECT SUM(sp.fee_cents) FROM square_payment sp JOIN tournament t ON t.id=sp.tournament_id
              WHERE t.series_id=s.id AND sp.ignored=0),0) AS square_fees,
    COALESCE((SELECT SUM(vp.amount_cents) FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id
              WHERE t.series_id=s.id AND vp.status='owed'),0) AS venue_owed,
    COALESCE((SELECT SUM(vp.amount_cents) FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id
              WHERE t.series_id=s.id AND vp.status='paid'),0) AS venue_paid,
    (SELECT COUNT(*) FROM event e JOIN tournament t ON t.id=e.tournament_id WHERE t.series_id=s.id) AS events,
    (SELECT MAX(start_date) FROM tournament t WHERE t.series_id=s.id) AS last_date,
    COALESCE((SELECT SUM(po.amount_cents) FROM payout po JOIN event e ON e.id=po.event_id
              JOIN tournament t ON t.id=e.tournament_id WHERE t.series_id=s.id AND po.status='owed'),0) AS owed,
    COALESCE((SELECT SUM(po.amount_cents) FROM payout po JOIN event e ON e.id=po.event_id
              JOIN tournament t ON t.id=e.tournament_id WHERE t.series_id=s.id AND po.status='paid'),0) AS paid"""


def one(conn, sql, *args):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise HTTPException(404)
    return row


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers ask for this path directly, whatever the page links."""
    return FileResponse(BASE / "static" / "favicon.ico", media_type="image/x-icon")


# --- dashboard & series ----------------------------------------------------

@app.get("/")
def dashboard(request: Request):
    with db.session() as conn:
        owed = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payout WHERE status='owed'").fetchone()[0]
        paid = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payout WHERE status='paid'").fetchone()[0]
        venue_owed = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM venue_payment WHERE status='owed'").fetchone()[0]
        venue_paid = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM venue_payment WHERE status='paid'").fetchone()[0]
        issues = sync.event_issues(conn)
        by_tournament = flags_by(issues, "tournament_id")
        recent = [{**dict(r), "flags": len(by_tournament.get(r["id"], []))} for r in conn.execute(
            f"""SELECT t.*, s.name AS series_name, s.code AS series_code, {TOURNAMENT_SUMS} FROM tournament t
                LEFT JOIN series s ON s.id=t.series_id ORDER BY t.start_date DESC LIMIT 10""")]
        owing = conn.execute("""SELECT p.id, p.gamer_tag, SUM(po.amount_cents) AS owed, COUNT(*) AS n,
                                       MIN(t.start_date) AS since
                                FROM payout po JOIN player p ON p.id=po.player_id JOIN event e ON e.id=po.event_id
                                JOIN tournament t ON t.id=e.tournament_id WHERE po.status='owed'
                                GROUP BY p.id ORDER BY owed DESC, p.gamer_tag COLLATE NOCASE""").fetchall()
    return render(request, "dashboard.html", owed=owed, paid=paid, recent=recent, owing=owing,
                  issues=issues, venue_owed=venue_owed, venue_paid=venue_paid)


@app.get("/series")
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


@app.post("/series")
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


@app.get("/series/{ref}")
def series_page(request: Request, ref: str, period: int | None = None):
    """`ref` is the series code (/series/FNB); a numeric id redirects to the code URL."""
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
        issues = sync.event_issues(conn, series_id=series_id)
        by_tournament = flags_by(issues, "tournament_id")
        tournaments = []
        for t in conn.execute(f"""SELECT t.*, {TOURNAMENT_SUMS} FROM tournament t WHERE t.series_id=?
                                  ORDER BY t.start_date DESC""", (series_id,)).fetchall():
            pools = [sync.event_pool(conn, e["id"]) for e in conn.execute(
                "SELECT id FROM event WHERE tournament_id=?", (t["id"],))]
            tournaments.append({**dict(t), "flags": len(by_tournament.get(t["id"], [])),
                                "pool": sum(i[1] for i in pools if i)})
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
                  rules=rules, events=events, sync_status=sync.status.get(series_id), issues=issues,
                  venues=venues, periods=periods, current=current, shown=shown,
                  square_status=square.status.get(series_id), square_setup=square_setup(),
                  auto_pull_hours=config.square_auto_pull_hours())


@app.post("/series/{series_id}/settings")
def series_settings(series_id: int, name: str = Form(...), code: str = Form(""), pattern: str = Form(""),
                    custom_slug: str = Form("")):
    if not pattern.strip() and not custom_slug.strip():
        return back(f"/series/{series_id}", "Enter a custom slug or a match pattern")
    try:
        with db.session() as conn:
            current = one(conn, "SELECT id, code FROM series WHERE id=?", series_id)
            code = claim_code(conn, code, series_id) if code.strip() else current["code"]
            conn.execute("UPDATE series SET name=?, code=?, pattern=?, custom_slug=? WHERE id=?",
                         (name.strip(), code, pattern.strip(), custom_slug.strip(), series_id))
    except RuleError as e:
        return back(f"/series/{series_id}", str(e))
    return back(f"/series/{code or series_id}")


@app.post("/series/{series_id}/track")
def toggle_track(series_id: int):
    with db.session() as conn:
        one(conn, "SELECT id FROM series WHERE id=?", series_id)
        conn.execute("UPDATE series SET tracked = 1 - tracked WHERE id=?", (series_id,))
    return back(f"/series/{series_id}")


@app.post("/series/{series_id}/sync")
def sync_now(series_id: int):
    with db.session() as conn:
        one(conn, "SELECT id FROM series WHERE id=?", series_id)
    if not config.api_key():
        return back(f"/series/{series_id}", "No API key configured (PARRY_API_KEY)")
    sync.start_sync(series_id, GrpcParryClient)
    return back(f"/series/{series_id}")


@app.post("/series/{series_id}/tournaments")
def add_tournament(series_id: int, link: str = Form(...)):
    try:
        client = GrpcParryClient()
        try:
            with db.session() as conn:
                one(conn, "SELECT id FROM series WHERE id=?", series_id)
                sync.add_manual_tournament(conn, client, series_id, link)
        finally:
            client.close()
    except Exception as e:
        return back(f"/series/{series_id}", f"{type(e).__name__}: {e}")
    return back(f"/series/{series_id}")


@app.post("/tournaments/{tournament_id}/exclude")
def toggle_exclude(tournament_id: str):
    with db.session() as conn:
        t = one(conn, "SELECT * FROM tournament WHERE id=?", tournament_id)
        conn.execute("UPDATE tournament SET excluded = 1 - excluded WHERE id=?", (tournament_id,))
        for e in conn.execute("SELECT id FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
            sync.recompute_event(conn, e["id"])  # excluded tournaments drop their unpaid payouts
        sync.recompute_venue(conn, tournament_id)
    return back(f"/series/{t['series_id']}" if t["series_id"] else "/")


@app.post("/tournaments/{tournament_id}/entry-fee")
def tournament_entry_fee(tournament_id: str, entry_fee: str = Form("")):
    try:
        with db.session() as conn:
            t = one(conn, "SELECT * FROM tournament WHERE id=?", tournament_id)
            conn.execute("UPDATE tournament SET entry_fee_cents=? WHERE id=?",
                         (dollars_to_cents(entry_fee) if entry_fee.strip() else None, tournament_id))
            for e in conn.execute("SELECT id FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
                sync.recompute_event(conn, e["id"])
            sync.recompute_venue(conn, tournament_id)
    except RuleError as e:
        return back(f"/tournaments/{tournament_id}", str(e))
    return back(f"/tournaments/{tournament_id}")


@app.post("/tournaments/{tournament_id}/venue")
def tournament_venue(tournament_id: str, venue_mode: str = Form("series"), venue_id: str = Form(""),
                     venue_flat: str = Form("0"), venue_entry_pct: str = Form("0"), venue_bracket_pct: str = Form("0")):
    try:
        if venue_mode not in ("series", "custom", "none"):
            raise RuleError("Unknown venue setting")
        cut = parse_venue(venue_flat, venue_entry_pct, venue_bracket_pct)
        with db.session() as conn:
            one(conn, "SELECT id FROM tournament WHERE id=?", tournament_id)
            conn.execute("""UPDATE tournament SET venue_mode=?, venue_id=?, venue_flat_cents=?, venue_entry_pct=?,
                            venue_bracket_pct=? WHERE id=?""",
                         (venue_mode, pick_venue(conn, venue_id), cut.flat, str(cut.entry_pct), str(cut.bracket_pct),
                          tournament_id))
            sync.recompute_venue(conn, tournament_id)
    except RuleError as e:
        return back(f"/tournaments/{tournament_id}", str(e))
    return back(f"/tournaments/{tournament_id}")


# --- periods (a series' terms over time) ----------------------------------

@app.post("/series/{series_id}/periods")
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
        return back(f"/series/{series_id}", str(e))
    return back(f"/series/{series_id}?period={period_id}")


@app.post("/periods/{period_id}")
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


@app.post("/periods/{period_id}/delete")
def delete_period(period_id: int):
    with db.session() as conn:
        p = one(conn, "SELECT * FROM series_period WHERE id=?", period_id)
        if p["starts_on"] == "":
            return back(f"/series/{p['series_id']}", "The first terms can't be deleted")
        conn.execute("DELETE FROM series_period WHERE id=?", (period_id,))  # and its game rules
        sync.recompute_series(conn, p["series_id"])  # its tournaments fall back to the terms before
    return back(f"/series/{p['series_id']}")


# --- Square ----------------------------------------------------------------

def square_setup() -> dict:
    """What the tournament page says about Square: whether a key is set, which environment, or what's wrong."""
    if not config.square_token():
        return {"token": False, "env": None, "error": None}
    try:
        return {"token": True, "env": square_environment(), "error": None}
    except SquareConfigError as e:
        return {"token": True, "env": None, "error": str(e)}


@app.post("/tournaments/{tournament_id}/square/pull")
def square_pull(tournament_id: str):
    to = f"/tournaments/{tournament_id}#square"
    with db.session() as conn:
        one(conn, "SELECT id FROM tournament WHERE id=?", tournament_id)
    if not config.square_token():
        return back(to, "No Square API key configured (SQUARE_API_KEY)")
    try:
        client = HttpSquareClient()
        try:
            with db.session() as conn:
                square.pull_tournament(conn, client, tournament_id)
        finally:
            client.close()
    except SquareConfigError as e:
        return back(f"/tournaments/{tournament_id}", str(e))
    except Exception as e:  # shown in the UI; never includes the token
        return back(f"/tournaments/{tournament_id}", f"{type(e).__name__}: {e}")
    return back(to)


@app.post("/series/{series_id}/square/pull")
def series_square_pull(series_id: int):
    """Pull every tournament of the series in the background (one Square query per location, see square.py)."""
    with db.session() as conn:
        one(conn, "SELECT id FROM series WHERE id=?", series_id)
    setup = square_setup()
    if not setup["env"]:
        return back(f"/series/{series_id}", setup["error"] or "No Square API key configured (SQUARE_API_KEY)")
    if not square.start_series_pull(series_id, lambda: HttpSquareClient()):
        return back(f"/series/{series_id}", "A Square pull is already running; try again when it finishes")
    return back(f"/series/{series_id}")


# --- bank reconciliation ---------------------------------------------------

@app.get("/bank")
def bank_page(request: Request):
    with db.session() as conn:
        deposits = [bank.summary(conn, d) for d in conn.execute(
            "SELECT * FROM bank_deposit ORDER BY deposited_on DESC, id DESC").fetchall()]
        waiting = bank.unbanked(conn)
    return render(request, "bank.html", deposits=deposits, waiting=waiting, overdue_days=bank.OVERDUE_DAYS,
                  today=datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat())


def _deposit_form(conn, deposit_id, deposited_on, amount, reference, note) -> int:
    return bank.save_deposit(conn, deposit_id, parse_date(deposited_on), dollars_to_cents(amount), reference, note)


@app.post("/bank")
def add_deposit(deposited_on: str = Form(...), amount: str = Form(...), reference: str = Form(""), note: str = Form("")):
    try:
        with db.session() as conn:
            deposit_id = _deposit_form(conn, None, deposited_on, amount, reference, note)
    except RuleError as e:
        return back("/bank", str(e))
    return back(f"/bank/{deposit_id}")


@app.get("/bank/{deposit_id}")
def deposit_page(request: Request, deposit_id: int):
    with db.session() as conn:
        d = one(conn, "SELECT * FROM bank_deposit WHERE id=?", deposit_id)
        info, options = bank.summary(conn, d), bank.candidates(conn, d)
    return render(request, "deposit.html", x=info, options=options, lookback=bank.LOOKBACK_DAYS)


@app.post("/bank/{deposit_id}")
def update_deposit(deposit_id: int, deposited_on: str = Form(...), amount: str = Form(...), reference: str = Form(""),
                   note: str = Form("")):
    try:
        with db.session() as conn:
            one(conn, "SELECT id FROM bank_deposit WHERE id=?", deposit_id)
            _deposit_form(conn, deposit_id, deposited_on, amount, reference, note)
    except RuleError as e:
        return back(f"/bank/{deposit_id}", str(e))
    return back(f"/bank/{deposit_id}")


@app.post("/bank/{deposit_id}/delete")
def delete_deposit(deposit_id: int):
    with db.session() as conn:
        one(conn, "SELECT id FROM bank_deposit WHERE id=?", deposit_id)
        conn.execute("DELETE FROM bank_deposit WHERE id=?", (deposit_id,))  # its payments become unbanked again
    return back("/bank")


@app.post("/bank/{deposit_id}/payments")
def match_payments(deposit_id: int, payment_id: list[str] = Form([])):
    with db.session() as conn:
        one(conn, "SELECT id FROM bank_deposit WHERE id=?", deposit_id)
        bank.match(conn, deposit_id, payment_id)
    return back(f"/bank/{deposit_id}")


@app.post("/bank/{deposit_id}/payments/{payment_id}/remove")
def unmatch_payment(deposit_id: int, payment_id: str):
    with db.session() as conn:
        one(conn, "SELECT id FROM bank_deposit WHERE id=?", deposit_id)
        bank.unmatch(conn, deposit_id, payment_id)
    return back(f"/bank/{deposit_id}")


@app.post("/square-payments/{payment_id}/link")
def square_link(payment_id: str, player_id: str = Form("")):
    try:
        with db.session() as conn:
            p = one(conn, "SELECT * FROM square_payment WHERE id=?", payment_id)
            square.link_payment(conn, payment_id, player_id.strip() or None)
    except RuleError as e:
        return back(f"/tournaments/{p['tournament_id']}", str(e))
    return back(f"/tournaments/{p['tournament_id']}#square")


@app.post("/square-payments/{payment_id}/ignore")
def square_ignore(payment_id: str):
    with db.session() as conn:
        p = one(conn, "SELECT * FROM square_payment WHERE id=?", payment_id)
        conn.execute("UPDATE square_payment SET ignored = 1 - ignored WHERE id=?", (payment_id,))
    return back(f"/tournaments/{p['tournament_id']}#square")


# --- rules -----------------------------------------------------------------

@app.post("/series/{series_id}/rules")
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


@app.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int):
    with db.session() as conn:
        r = one(conn, "SELECT * FROM rule WHERE id=?", rule_id)
        conn.execute("DELETE FROM rule WHERE id=?", (rule_id,))
        sync.recompute_series(conn, r["series_id"])
    return back(f"/series/{r['series_id']}" + (f"?period={r['period_id']}" if r["period_id"] else ""))


# --- tournaments, events & games -------------------------------------------

@app.get("/tournaments/{tournament_id}")
def tournament_page(request: Request, tournament_id: str):
    with db.session() as conn:
        t = one(conn, """SELECT t.*, s.name AS series_name, s.code AS series_code, p.entry_fee_cents AS series_fee,
                                p.starts_on AS period_start, p.venue_id AS series_venue_id, v.name AS series_venue
                         FROM tournament t LEFT JOIN series s ON s.id=t.series_id
                         LEFT JOIN series_period p ON p.id=t.period_id LEFT JOIN venue v ON v.id=p.venue_id
                         WHERE t.id=?""", tournament_id)
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
    return render(request, "tournament.html", t=t, events=events, attendees=list(attendees.values()), venue=venue,
                  venues=venues, recon=recon, square_setup=square_setup())


@app.get("/events")
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


@app.get("/events/{event_id}")
def event_page(request: Request, event_id: str):
    with db.session() as conn:
        e = one(conn, """SELECT e.*, t.name AS tname, t.id AS tid, t.slug AS tslug, t.start_date, t.series_id, s.name AS sname, s.code AS scode
                         FROM event e JOIN tournament t ON t.id=e.tournament_id
                         LEFT JOIN series s ON s.id=t.series_id WHERE e.id=?""", event_id)
        info = sync.event_pool(conn, event_id)
        issue = sync.event_issues(conn, event_id=event_id)
        rows = conn.execute("""SELECT p.id AS player_id, p.gamer_tag, ee.placement, po.id AS payout_id,
                                      po.amount_cents, po.computed_cents, po.status, po.paid_at
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


@app.get("/games")
def games_page(request: Request):
    with db.session() as conn:
        games = conn.execute(f"""SELECT e.game_name, COUNT(*) AS events, COUNT(DISTINCT t.id) AS tournaments,
                                        SUM(e.entrant_count) AS entrants, SUM(e.owed) AS owed, SUM(e.paid) AS paid,
                                        MAX(t.start_date) AS last_date
                                 FROM (SELECT e.*, {EVENT_SUMS} FROM event e) e
                                 JOIN tournament t ON t.id=e.tournament_id WHERE t.excluded=0
                                 GROUP BY e.game_name ORDER BY e.game_name""").fetchall()
    return render(request, "games.html", games=games)


@app.get("/games/{name:path}")
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


@app.get("/payouts")
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


# --- players & payments ----------------------------------------------------

@app.get("/players")
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


@app.get("/players/{player_id}")
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
                                         po.id AS payout_id, po.amount_cents, po.computed_cents, po.status, po.paid_at
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


@app.post("/players/{player_id}/merge")
def merge_player(player_id: str, other: str = Form(...)):
    """Merge `other` into this player (e.g. the same person imported under a second parry.gg account)."""
    try:
        with db.session() as conn:
            sync.merge_players(conn, player_id, other)
    except RuleError as e:
        return back(f"/players/{quote(player_id, safe='')}", str(e))
    return back(f"/players/{quote(player_id, safe='')}")


@app.post("/players/{player_id}/unmerge")
def unmerge_player(player_id: str):
    try:
        with db.session() as conn:
            keep_id = sync.unmerge_player(conn, player_id)
    except RuleError as e:
        return back("/players", str(e))
    return back(f"/players/{quote(keep_id, safe='')}")


@app.post("/payouts/{payout_id}/paid")
def mark_paid(payout_id: int, note: str = Form(""), undo: str = Form(""), next: str = Form("")):
    with db.session() as conn:
        po = one(conn, "SELECT * FROM payout WHERE id=?", payout_id)
        if undo:
            conn.execute("UPDATE payout SET status='owed', paid_at=NULL, amount_cents=computed_cents WHERE id=?",
                         (payout_id,))
        else:
            conn.execute("UPDATE payout SET status='paid', paid_at=?, note=? WHERE id=?",
                         (sync.now(), note.strip(), payout_id))
    return back(safe_next(next, f"/players/{po['player_id']}"))


@app.post("/players/{player_id}/settle")
def settle_all(player_id: str, note: str = Form(""), next: str = Form("")):
    with db.session() as conn:
        one(conn, "SELECT id FROM player WHERE id=?", player_id)
        conn.execute("UPDATE payout SET status='paid', paid_at=?, note=? WHERE player_id=? AND status='owed'",
                     (sync.now(), note.strip(), player_id))
    return back(safe_next(next, f"/players/{player_id}"))


VENUE_SUMS = """(SELECT COUNT(DISTINCT series_id) FROM series_period WHERE venue_id=v.id) AS series,
    (SELECT COUNT(*) FROM tournament t LEFT JOIN series_period p ON p.id=t.period_id
     WHERE COALESCE(t.venue_id, p.venue_id)=v.id AND t.venue_mode != 'none' AND t.excluded=0) AS tournaments,
    (SELECT MAX(t.start_date) FROM tournament t LEFT JOIN series_period p ON p.id=t.period_id
     WHERE COALESCE(t.venue_id, p.venue_id)=v.id AND t.venue_mode != 'none' AND t.excluded=0) AS last_date,
    COALESCE((SELECT SUM(amount_cents) FROM venue_payment WHERE venue_id=v.id AND status='owed'),0) AS owed,
    COALESCE((SELECT SUM(amount_cents) FROM venue_payment WHERE venue_id=v.id AND status='paid'),0) AS paid"""


@app.get("/venues")
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


@app.post("/venues")
def create_venue(name: str = Form(...), notes: str = Form(""), flat: str = Form("0"), entry_pct: str = Form("0"),
                 bracket_pct: str = Form("0")):
    try:
        with db.session() as conn:
            venue_id = save_venue_form(conn, None, name, notes, flat, entry_pct, bracket_pct)
    except RuleError as e:
        return back("/venues", str(e))
    return back(f"/venues/{venue_id}")


@app.get("/venues/{venue_id}")
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


@app.post("/venues/{venue_id}")
def update_venue(venue_id: int, name: str = Form(...), notes: str = Form(""), flat: str = Form("0"),
                 entry_pct: str = Form("0"), bracket_pct: str = Form("0"), square_location_id: str = Form("")):
    try:
        with db.session() as conn:
            one(conn, "SELECT id FROM venue WHERE id=?", venue_id)
            save_venue_form(conn, venue_id, name, notes, flat, entry_pct, bracket_pct, square_location_id)
    except RuleError as e:
        return back(f"/venues/{venue_id}", str(e))
    return back(f"/venues/{venue_id}")


@app.get("/venue")
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


@app.post("/venue/{tournament_id}/paid")
def venue_mark_paid(tournament_id: str, note: str = Form(""), undo: str = Form(""), next: str = Form("")):
    with db.session() as conn:
        one(conn, "SELECT tournament_id FROM venue_payment WHERE tournament_id=?", tournament_id)
        if undo:
            conn.execute("""UPDATE venue_payment SET status='owed', paid_at=NULL, amount_cents=computed_cents
                            WHERE tournament_id=?""", (tournament_id,))
        else:
            conn.execute("UPDATE venue_payment SET status='paid', paid_at=?, note=? WHERE tournament_id=?",
                         (sync.now(), note.strip(), tournament_id))
    return back(safe_next(next, f"/tournaments/{tournament_id}"))
