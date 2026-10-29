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


def _card_owners(conn, fingerprint: str) -> list[str]:
    """The players a card has been linked to by hand (merged players resolved)."""
    return [r["pid"] for r in conn.execute(
        """SELECT DISTINCT COALESCE(p.merged_into, p.id) AS pid FROM square_payment sp JOIN player p ON p.id=sp.player_id
           WHERE sp.fingerprint=? AND sp.link='manual'""", (fingerprint,))]


def _auto_player(conn, fingerprint: str):
    """The player a card has been linked to by hand, in any tournament (#22).

    Only when it's unambiguous: a card that has paid for more than one player (e.g. someone paying for a friend) is
    never auto-linked. A second tap the same night, or a payment at a tournament the player didn't enter, links too:
    it then shows them as overpaid rather than hiding as unlinked.
    """
    owners = _card_owners(conn, fingerprint) if fingerprint else []
    return owners[0] if len(owners) == 1 else None


def _relink_card(conn, fingerprint: str):
    """After a card's payment is linked or unlinked by hand, link its other payments to match, in every tournament.

    Its automatic links follow the one player it's now linked to by hand, and its unlinked payments join them (not
    ignored or refunded ones). Payments linked or unlinked by hand are left alone. A card no longer linked to anyone
    loses its automatic links; one linked to several players keeps those it has but gains no more.
    """
    owners = _card_owners(conn, fingerprint)
    if len(owners) > 1:
        return
    owner = owners[0] if owners else None
    conn.execute("""UPDATE square_payment SET player_id=?, link=? WHERE fingerprint=?
                    AND (link='auto' OR (link='none' AND ignored=0 AND amount_cents > 0))""",
                 (owner, "auto" if owner else "none", fingerprint))


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
    player = _auto_player(conn, p.fingerprint) if p.amount_cents > 0 else None  # not a refunded one
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
        lock_clear_tournaments(conn)
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
    if not config.square_configured():
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
    """What each attendee should have paid: the venue fee plus the bracket fee of each event they entered."""
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
    """Expected vs taken, per player and in total. Ignored payments don't count, and neither do fully refunded
    ones (stored at $0), apart from their card fees: Square's payment data doesn't say whether it gave those back."""
    expected = expected_by_player(conn, tournament_id)
    payments = conn.execute("""SELECT sp.*, COALESCE(a.merged_into, sp.player_id) AS pid, p.gamer_tag
                               FROM square_payment sp LEFT JOIN player a ON a.id=sp.player_id
                               LEFT JOIN player p ON p.id=COALESCE(a.merged_into, sp.player_id)
                               WHERE sp.tournament_id=? ORDER BY sp.created_at""", (tournament_id,)).fetchall()
    counted = [p for p in payments if not p["ignored"] and p["amount_cents"] > 0]
    refunded = [p for p in payments if not p["ignored"] and p["amount_cents"] <= 0]
    paid = {}
    for p in counted:
        if p["pid"]:
            paid[p["pid"]] = paid.get(p["pid"], 0) + p["amount_cents"]
    tags = {r["id"]: r["gamer_tag"] for r in conn.execute(
        "SELECT p.id, p.gamer_tag FROM tournament_entry_c te JOIN player p ON p.id=te.player_id WHERE te.tournament_id=?",
        (tournament_id,))}
    for p in counted:  # a card's payments link to its player even where they weren't entered (#22)
        if p["pid"]:
            tags.setdefault(p["pid"], p["gamer_tag"])
    resolved = {r["player_id"]: r["note"] for r in conn.execute(
        "SELECT player_id, note FROM entrant_override WHERE tournament_id=?", (tournament_id,))}
    players = []
    for pid in set(expected) | set(paid):
        want, got = expected.get(pid, 0), paid.get(pid, 0)
        state = "ok" if got == want else "unpaid" if got == 0 else "short" if got < want else "over"
        players.append({"id": pid, "tag": tags.get(pid, pid), "expected": want, "paid": got, "diff": got - want,
                        "state": state, "note": resolved.get(pid)})
    unlinked = [p for p in counted if not p["pid"]]
    if unlinked:  # card taps carry no names: until they're linked, "unpaid" may just mean "not matched yet"
        for r in players:
            if r["state"] == "unpaid":
                r["state"] = "unmatched"
    for r in players:  # sorted out by hand, e.g. someone else paid for them (#29)
        if r["state"] != "ok" and r["note"] is not None:
            r["state"] = "resolved"
    order = {"unpaid": 0, "short": 0, "over": 0, "unmatched": 1, "resolved": 2, "ok": 2}
    players.sort(key=lambda r: (order[r["state"]], r["tag"].lower()))
    expected_total, taken = sum(expected.values()), sum(p["amount_cents"] for p in counted)
    waived = sum(max(-r["diff"], 0) for r in players if r["state"] == "resolved")  # not missing: it's sorted out
    fees = sum(p["fee_cents"] or 0 for p in counted + refunded)
    banked = sum(p["amount_cents"] - (p["fee_cents"] or 0) for p in counted + refunded if p["deposit_id"])
    return {"payments": payments, "players": players, "expected": expected_total, "taken": taken,
            "fees": fees, "net": taken - fees, "banked": banked, "fees_pending": sum(1 for p in counted if p["fee_cents"] is None),
            "difference": taken - expected_total, "attendees": len(expected), "count": len(counted),
            "unlinked": len(unlinked), "unlinked_cents": sum(p["amount_cents"] for p in unlinked),
            "unmatched": sum(1 for r in players if r["state"] == "unmatched"),
            "shortfall": max(expected_total - taken - waived, 0), "refunded": len(refunded),
            "refunded_fees": sum(p["fee_cents"] or 0 for p in refunded),
            "problems": sum(1 for r in players if r["state"] in ("unpaid", "short", "over"))}


def payment_issues(conn, series_id: int | None = None) -> list[dict]:
    """Tournaments whose Square reconciliation needs attention, each with a `kind`:

    "missing": linked players are unpaid or short (players, missing), or, with payments still to link, less was taken
    than expected overall (players 0, missing = the shortfall). "link": nothing is known to be missing, but payments
    still need linking to players. Only tournaments with Square payments pulled are checked: one never pulled (or a
    series not using Square) would otherwise show everyone as unpaid. Excluded tournaments are skipped.
    """
    out = []
    for t in conn.execute("""SELECT t.id, t.name, t.series_id, t.start_date FROM tournament t
                             WHERE t.excluded=0 AND (? IS NULL OR t.series_id=?)
                             AND EXISTS (SELECT 1 FROM square_payment WHERE tournament_id=t.id)
                             ORDER BY t.start_date DESC""", (series_id, series_id)).fetchall():
        r = reconcile(conn, t["id"])
        missing = [p for p in r["players"] if p["state"] in ("unpaid", "short")]
        row = {"tournament_id": t["id"], "tournament_name": t["name"], "series_id": t["series_id"],
               "players": len(missing), "missing": -sum(p["diff"] for p in missing),
               "unlinked": r["unlinked"], "unlinked_cents": r["unlinked_cents"]}
        if missing:
            out.append({**row, "kind": "missing"})
        elif r["unmatched"] and r["shortfall"]:
            out.append({**row, "kind": "missing", "missing": r["shortfall"]})
        elif r["unlinked"]:
            out.append({**row, "kind": "link"})
    return out


def lock_clear_tournaments(conn) -> int:
    """Lock the tournaments up to the latest bank reconciliation that are clear of issues: no payments missing or to
    link, no events with unfinalised placements (#27). A locked tournament's figures and payment links can't change,
    and imports skip it (#28), until that reconciliation is undone. Returns how many were locked."""
    last = bank.latest(conn)
    if last is None:
        return 0
    flagged = {i["tournament_id"] for i in payment_issues(conn)} | {i["tournament_id"] for i in sync.event_issues(conn)}
    ids = [r[0] for r in conn.execute(
        """SELECT id FROM tournament WHERE locked_by IS NULL AND excluded=0 AND COALESCE(start_date, '') != ''
           AND start_date <= ?""", (last["reconciled_on"],)) if r[0] not in flagged]
    conn.executemany("UPDATE tournament SET locked_by=? WHERE id=?", [(last["id"], tid) for tid in ids])
    return len(ids)


def check_unlocked(conn, tournament_id: str):
    t = conn.execute("SELECT locked_by FROM tournament WHERE id=?", (tournament_id,)).fetchone()
    if t and t["locked_by"] is not None:
        raise RuleError(bank.locked_message(conn, t["locked_by"]))


def resolve_entrant(conn, tournament_id: str, player_id: str, note: str):
    """Mark an entrant's payment as sorted out some other way (#29), with a note saying how."""
    if not note.strip():
        raise RuleError("Say how it was sorted out, e.g. who paid for them")
    check_unlocked(conn, tournament_id)
    conn.execute("""INSERT INTO entrant_override (tournament_id, player_id, note, created_at) VALUES (?,?,?,?)
                    ON CONFLICT(tournament_id, player_id) DO UPDATE SET note=excluded.note""",
                 (tournament_id, player_id, note.strip(), sync.now()))


def unresolve_entrant(conn, tournament_id: str, player_id: str):
    check_unlocked(conn, tournament_id)
    conn.execute("DELETE FROM entrant_override WHERE tournament_id=? AND player_id=?", (tournament_id, player_id))


def link_payment(conn, payment_id: str, player_id: str | None):
    """Link a payment to a player by hand, or unlink it (it then stays unlinked, whatever its card is linked to later),
    and link the card's other payments to match."""
    if player_id and not conn.execute("SELECT 1 FROM player WHERE id=?", (player_id,)).fetchone():
        raise RuleError("Unknown player")
    payment = conn.execute("SELECT tournament_id FROM square_payment WHERE id=?", (payment_id,)).fetchone()
    if payment:
        check_unlocked(conn, payment["tournament_id"])
    conn.execute("UPDATE square_payment SET player_id=?, link=? WHERE id=?",
                 (player_id or None, "manual" if player_id else "unlinked", payment_id))
    card = conn.execute("SELECT fingerprint FROM square_payment WHERE id=?", (payment_id,)).fetchone()
    if card and card[0]:
        _relink_card(conn, card[0])
