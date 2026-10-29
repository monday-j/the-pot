"""Pull a tournament's Square payments and reconcile them against what attendees should have paid."""
import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import bank, config, db, sync
from .payouts import RuleError
from .square_client import SquareClient, SquareConfigError, SquareRateLimited, environment

# Payments from the tournament's day until this hour the next morning count towards it.
NEXT_DAY_CUTOFF = time(6)


def window(start_date: str, tz: str | None = None) -> tuple[str, str]:
    """The tournament's local day (plus the small hours after) as UTC RFC 3339 times for Square."""
    zone = ZoneInfo(tz or config.TIMEZONE)
    day = date.fromisoformat(start_date)
    begin = datetime.combine(day, time(0), zone)
    end = datetime.combine(day + timedelta(days=1), NEXT_DAY_CUTOFF, zone)
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fmt(begin), fmt(end)


def _auto_player(conn, tournament_id: str, fingerprint: str):
    """The player a card was linked to by hand before, if they attended this tournament.

    Only when it's unambiguous: a card that has paid for more than one player (e.g. someone paying for a friend) is
    never auto-linked, and only a card's first payment in a tournament is, so a second tap stays unlinked and flagged.
    """
    if not fingerprint:
        return None
    if conn.execute("SELECT 1 FROM square_payment WHERE tournament_id=? AND fingerprint=?",
                    (tournament_id, fingerprint)).fetchone():
        return None
    owners = [r["pid"] for r in conn.execute(
        """SELECT DISTINCT COALESCE(p.merged_into, p.id) AS pid FROM square_payment sp JOIN player p ON p.id=sp.player_id
           WHERE sp.fingerprint=? AND sp.link='manual'""", (fingerprint,))]
    if len(owners) == 1 and conn.execute("SELECT 1 FROM tournament_entry_c WHERE tournament_id=? AND player_id=?",
                                         (tournament_id, owners[0])).fetchone():
        return owners[0]
    return None


def _location(conn, tournament_id: str) -> str:
    """The Square location of the tournament's venue, or "" for the seller's main location."""
    info = sync.venue_inputs(conn, tournament_id)
    if info and info["venue_id"]:
        return conn.execute("SELECT square_location_id FROM venue WHERE id=?", (info["venue_id"],)).fetchone()[0]
    return ""


def _utc(stamp: str):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _store(conn, tournament_id: str | None, p, counts: dict):
    """Insert a completed payment for a tournament, or refresh one already stored. Keeps links and ignores.

    A stored payment stays with the tournament it was filed under (even if its time now fits another one, e.g. after a
    date change), but its details are always refreshed: Square adds the processing fee, and refunds, later on.
    `tournament_id` is None for a payment outside every window: refreshed if stored, otherwise skipped.
    """
    row = conn.execute("SELECT * FROM square_payment WHERE id=?", (p.id,)).fetchone()
    fields = (p.amount_cents, p.fee_cents, p.created_at, p.source_type, p.card_brand, p.last_4, p.fingerprint,
              p.cardholder_name, p.note, p.receipt_url)
    if row:
        conn.execute("""UPDATE square_payment SET amount_cents=?, fee_cents=?, created_at=?, source_type=?, card_brand=?,
                        last_4=?, fingerprint=?, cardholder_name=?, note=?, receipt_url=? WHERE id=?""", (*fields, p.id))
        counts["updated" if tournament_id in (None, row["tournament_id"]) else "elsewhere"] += 1
        return
    if tournament_id is None:
        return
    player = _auto_player(conn, tournament_id, p.fingerprint)
    paid_out_by = conn.execute(  # an imported Square payout that already listed this payment
        "SELECT deposit_id FROM square_payout_entry WHERE payment_id=? AND type='CHARGE' LIMIT 1", (p.id,)).fetchone()
    conn.execute("""INSERT INTO square_payment (id, tournament_id, player_id, link, amount_cents, fee_cents, created_at,
                    source_type, card_brand, last_4, fingerprint, cardholder_name, note, receipt_url, deposit_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (p.id, tournament_id, player, "auto" if player else "none", *fields,
                  paid_out_by[0] if paid_out_by else None))
    counts["new"] += 1
    counts["linked"] += player is not None


def pull_tournaments(conn, client: SquareClient, tournament_ids) -> dict:
    """Store the completed Square payments (net of refunds) of several tournaments. Idempotent.

    To keep the number of Square calls low, this makes one (paged) query per Square location spanning all the
    tournaments' windows, then files each payment under the tournament whose window it falls in; payments between
    tournaments are skipped. A payment in two windows (the small hours after a night, before the next day's event)
    goes to the earlier tournament. Tournaments without a date are skipped.
    """
    by_location = {}
    for tid in tournament_ids:
        t = conn.execute("SELECT id, start_date FROM tournament WHERE id=?", (tid,)).fetchone()
        if t and t["start_date"]:
            begin, end = window(t["start_date"])
            by_location.setdefault(_location(conn, tid), []).append((_utc(begin), _utc(end), tid))
    counts = {"new": 0, "updated": 0, "elsewhere": 0, "linked": 0}
    for location, windows in by_location.items():
        windows.sort()
        begin, end = windows[0][0], max(w[1] for w in windows)
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        for p in client.list_payments(fmt(begin), fmt(end), location):
            if p.status != "COMPLETED" or not p.created_at:
                continue
            when = _utc(p.created_at)
            _store(conn, next((w[2] for w in windows if w[0] <= when <= w[1]), None), p, counts)
    return counts


def pull_tournament(conn, client: SquareClient, tournament_id: str) -> dict:
    """Store one tournament's completed Square payments; see pull_tournaments."""
    t = conn.execute("SELECT * FROM tournament WHERE id=?", (tournament_id,)).fetchone()
    if not t["start_date"]:
        raise RuleError("This tournament has no date, so its Square payments can't be found")
    return pull_tournaments(conn, client, [tournament_id])


def pull_series(conn, client: SquareClient, series_id: int, since: str | None = None) -> dict:
    """Pull every dated, non-excluded tournament of a series (only those on or after `since`, if given)."""
    ids = [r[0] for r in conn.execute(
        """SELECT id FROM tournament WHERE series_id=? AND excluded=0 AND COALESCE(start_date, '') != ''
           AND (? IS NULL OR start_date >= ?) ORDER BY start_date""", (series_id, since, since))]
    return {**pull_tournaments(conn, client, ids), "tournaments": len(ids)}


# --- background and automatic pulls ----------------------------------------

AUTO_PULL_DAYS = 14  # automatic pulls cover tracked series' tournaments from this many days back
AUTO_PULL_FIRST_DELAY = 60  # seconds after startup before the first automatic pull

_running = threading.Lock()  # one Square pull at a time, manual or automatic
status: dict = {}  # series_id or "payouts" -> {"state": "running"|"done"|"error", "message": str, "at": str, "auto": bool}
log = logging.getLogger("the_pot.square")


def _run(key, client_factory, job, auto: bool, message: str = "Pulling payments from Square..."):
    """Run one Square job, job(conn, client) -> result message, recording its status under `key` (a series id, or
    "payouts"). The caller holds _running, which this releases."""
    status[key] = {"state": "running", "message": message, "at": sync.now(), "auto": auto}
    try:
        client = client_factory()
        try:
            with db.session() as conn:
                result = job(conn, client)
        finally:
            getattr(client, "close", lambda: None)()
        status[key] = {"state": "done", "message": result, "at": sync.now(), "auto": auto}
    except Exception as e:  # shown on the page; never includes the token
        detail = str(e) if isinstance(e, (SquareConfigError, SquareRateLimited)) else f"{type(e).__name__}: {e}"
        status[key] = {"state": "error", "message": detail, "at": sync.now(), "auto": auto}
        log.warning("Square %s pull failed: %s", key, detail)
    finally:
        _running.release()


def _series_job(series_id: int, since: str | None):
    def job(conn, client):
        r = pull_series(conn, client, series_id, since)
        message = f"{r['new']} new and {r['updated']} updated payments across {r['tournaments']} tournaments"
        return message + (f", {r['linked']} linked automatically" if r["linked"] else "")
    return job


def _payouts_job(since: str):
    def job(conn, client):
        begin = datetime.combine(date.fromisoformat(since), time(0), ZoneInfo(config.TIMEZONE))
        r = bank.import_payouts(conn, client, begin.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return (f"{r['new']} new and {r['updated']} updated payouts since {since} ({r['unchanged']} unchanged), "
                f"{r['matched']} payments matched")
    return job


def _start(key, client_factory, job, message: str) -> bool:
    if not _running.acquire(blocking=False):
        return False
    status[key] = {"state": "running", "message": message, "at": sync.now(), "auto": False}
    threading.Thread(target=_run, args=(key, client_factory, job, False, message), daemon=True).start()
    return True


def start_series_pull(series_id: int, client_factory) -> bool:
    """Pull a whole series in the background. False when a Square pull is already running."""
    return _start(series_id, client_factory, _series_job(series_id, None), "Pulling payments from Square...")


def start_payouts_pull(client_factory, since: str) -> bool:
    """Import Square payouts sent since a date, in the background. False when a Square pull is already running."""
    return _start("payouts", client_factory, _payouts_job(since), "Importing payouts from Square...")


def auto_pull_once(client_factory, days: int = AUTO_PULL_DAYS):
    """Pull recent tournaments of every tracked series, one series after another, then recent payouts."""
    if not config.square_token():
        return
    try:
        environment()
    except SquareConfigError as e:
        log.warning("Automatic Square pull skipped: %s", e)
        return
    since = (datetime.now(ZoneInfo(config.TIMEZONE)).date() - timedelta(days=days)).isoformat()
    with db.session() as conn:
        series = [r[0] for r in conn.execute(
            """SELECT DISTINCT s.id FROM series s JOIN tournament t ON t.series_id=s.id
               WHERE s.tracked=1 AND t.excluded=0 AND t.start_date >= ? ORDER BY s.id""", (since,))]
    for series_id in series:
        if _running.acquire(blocking=False):  # a manual pull in progress? skip this series until next time
            _run(series_id, client_factory, _series_job(series_id, since), True)
    if _running.acquire(blocking=False):  # then the payouts that paid them out
        _run("payouts", client_factory, _payouts_job(since), True, "Importing payouts from Square...")


def auto_pull_loop(stop: threading.Event, client_factory, hours: float):
    """Every `hours` (first run shortly after startup) until `stop` is set."""
    wait = AUTO_PULL_FIRST_DELAY
    while not stop.wait(wait):
        try:
            auto_pull_once(client_factory)
        except Exception:
            log.exception("Automatic Square pull failed")
        wait = hours * 3600


def expected_by_player(conn, tournament_id: str) -> dict:
    """What each attendee should have paid: the entry fee plus the bracket fee of each event they entered."""
    t = conn.execute("""SELECT t.*, p.entry_fee_cents AS p_fee FROM tournament t
                        LEFT JOIN series_period p ON p.id=t.period_id WHERE t.id=?""", (tournament_id,)).fetchone()
    fee = t["entry_fee_cents"] if t["entry_fee_cents"] is not None else (t["p_fee"] or 0)
    expected = {r[0]: fee for r in conn.execute(
        "SELECT player_id FROM tournament_entry_c WHERE tournament_id=?", (tournament_id,))}
    for e in conn.execute("SELECT * FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
        rule = sync.resolve_rule(conn, t["period_id"], e["id"], e["game_name"])
        if rule is None or not rule.bracket_fee:
            continue
        for r in conn.execute("SELECT player_id FROM event_entry_c WHERE event_id=?", (e["id"],)):
            expected[r[0]] = expected.get(r[0], fee) + rule.bracket_fee
    return expected


def reconcile(conn, tournament_id: str) -> dict:
    """Expected vs taken, per player and in total. Ignored payments don't count."""
    expected = expected_by_player(conn, tournament_id)
    payments = conn.execute("""SELECT sp.*, COALESCE(a.merged_into, sp.player_id) AS pid, p.gamer_tag
                               FROM square_payment sp LEFT JOIN player a ON a.id=sp.player_id
                               LEFT JOIN player p ON p.id=COALESCE(a.merged_into, sp.player_id)
                               WHERE sp.tournament_id=? ORDER BY sp.created_at""", (tournament_id,)).fetchall()
    counted = [p for p in payments if not p["ignored"]]
    paid = {}
    for p in counted:
        if p["pid"]:
            paid[p["pid"]] = paid.get(p["pid"], 0) + p["amount_cents"]
    tags = {r["id"]: r["gamer_tag"] for r in conn.execute(
        "SELECT p.id, p.gamer_tag FROM tournament_entry_c te JOIN player p ON p.id=te.player_id WHERE te.tournament_id=?",
        (tournament_id,))}
    players = []
    for pid in set(expected) | set(paid):
        want, got = expected.get(pid, 0), paid.get(pid, 0)
        state = "ok" if got == want else "unpaid" if got == 0 else "short" if got < want else "over"
        players.append({"id": pid, "tag": tags.get(pid, pid), "expected": want, "paid": got, "diff": got - want,
                        "state": state})
    players.sort(key=lambda r: (r["state"] == "ok", r["tag"].lower()))
    expected_total, taken = sum(expected.values()), sum(p["amount_cents"] for p in counted)
    fees = sum(p["fee_cents"] or 0 for p in counted)
    banked = sum(p["amount_cents"] - (p["fee_cents"] or 0) for p in counted if p["deposit_id"])
    return {"payments": payments, "players": players, "expected": expected_total, "taken": taken,
            "fees": fees, "net": taken - fees, "banked": banked, "fees_pending": sum(1 for p in counted if p["fee_cents"] is None),
            "difference": taken - expected_total, "attendees": len(expected), "count": len(counted),
            "unlinked": sum(1 for p in counted if not p["player_id"]),
            "problems": sum(1 for r in players if r["state"] != "ok")}


def link_payment(conn, payment_id: str, player_id: str | None):
    if player_id and not conn.execute("SELECT 1 FROM player WHERE id=?", (player_id,)).fetchone():
        raise RuleError("Unknown player")
    conn.execute("UPDATE square_payment SET player_id=?, link=? WHERE id=?",
                 (player_id or None, "manual" if player_id else "none", payment_id))
