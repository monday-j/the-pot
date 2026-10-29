"""Pull a tournament's Square payments and reconcile them against what attendees should have paid."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, sync
from .payouts import RuleError
from .square_client import SquareClient

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


def pull_tournament(conn, client: SquareClient, tournament_id: str) -> dict:
    """Store the tournament's completed Square payments (net of refunds). Idempotent; keeps links and ignores.
    A payment already stored against another tournament (e.g. one the same day) is left there."""
    t = conn.execute("SELECT * FROM tournament WHERE id=?", (tournament_id,)).fetchone()
    if not t["start_date"]:
        raise RuleError("This tournament has no date, so its Square payments can't be found")
    info = sync.venue_inputs(conn, tournament_id)
    location = ""
    if info and info["venue_id"]:
        location = conn.execute("SELECT square_location_id FROM venue WHERE id=?",
                                (info["venue_id"],)).fetchone()[0]
    begin, end = window(t["start_date"])
    counts = {"new": 0, "updated": 0, "elsewhere": 0, "linked": 0}
    for p in client.list_payments(begin, end, location):
        if p.status != "COMPLETED":
            continue
        row = conn.execute("SELECT * FROM square_payment WHERE id=?", (p.id,)).fetchone()
        if row and row["tournament_id"] != tournament_id:
            counts["elsewhere"] += 1
            continue
        fields = (p.amount_cents, p.created_at, p.source_type, p.card_brand, p.last_4, p.fingerprint,
                  p.cardholder_name, p.note, p.receipt_url)
        if row:
            conn.execute("""UPDATE square_payment SET amount_cents=?, created_at=?, source_type=?, card_brand=?, last_4=?,
                            fingerprint=?, cardholder_name=?, note=?, receipt_url=? WHERE id=?""", (*fields, p.id))
            counts["updated"] += 1
            continue
        player = _auto_player(conn, tournament_id, p.fingerprint)
        conn.execute("""INSERT INTO square_payment (id, tournament_id, player_id, link, amount_cents, created_at, source_type,
                        card_brand, last_4, fingerprint, cardholder_name, note, receipt_url)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (p.id, tournament_id, player, "auto" if player else "none", *fields))
        counts["new"] += 1
        counts["linked"] += player is not None
    return counts


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
    return {"payments": payments, "players": players, "expected": expected_total, "taken": taken,
            "difference": taken - expected_total, "attendees": len(expected), "count": len(counted),
            "unlinked": sum(1 for p in counted if not p["player_id"]),
            "problems": sum(1 for r in players if r["state"] != "ok")}


def link_payment(conn, payment_id: str, player_id: str | None):
    if player_id and not conn.execute("SELECT 1 FROM player WHERE id=?", (player_id,)).fetchone():
        raise RuleError("Unknown player")
    conn.execute("UPDATE square_payment SET player_id=?, link=? WHERE id=?",
                 (player_id or None, "manual" if player_id else "none", payment_id))
