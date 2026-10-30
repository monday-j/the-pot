"""Shared by every page: templates and their filters, form parsing, redirects and SQL fragments."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, square_auth
from .parry_client import GrpcParryClient
from .payouts import RuleError, VenueCut, splits_from_json
from .square_client import HttpSquareClient, SquareConfigError, environment as square_environment

BASE = Path(__file__).parent
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


def shortday(day: str | None) -> str:
    """An ISO date as "Thu 29 Oct", with the year if it isn't this year."""
    try:
        d = date.fromisoformat(day or "")
    except ValueError:
        return day or ""
    return f"{d:%a} {d.day} {d:%b}" + (f" {d.year}" if d.year != datetime.now(ZoneInfo(config.TIMEZONE)).year else "")


templates.env.filters["money"] = money
templates.env.filters["shortday"] = shortday
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
        bits.append(f"{r['entry_fee_share_pct']}% of venue fees")
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


def client_key(request: Request) -> str:
    return request.client.host if request.client else ""


NO_SQUARE = "Square isn't set up: see the README to give the app read-only access"


def parry_client():
    """A parry.gg client; tests replace GrpcParryClient here with a fake."""
    return GrpcParryClient()


def square_client():
    """A Square client; tests replace HttpSquareClient here with a fake."""
    return HttpSquareClient()


def square_setup() -> dict:
    """What the tournament page says about Square: whether a key is set, which environment, or what's wrong."""
    if not config.square_configured():
        return {"token": False, "env": None, "error": None}
    try:  # settings only: the token itself is fetched and checked with Square when a pull runs
        square_auth.credentials()
        return {"token": True, "env": square_environment(), "error": None}
    except SquareConfigError as e:
        return {"token": True, "env": None, "error": str(e)}
