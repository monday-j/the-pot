"""Pull a tournament's Square payments and reconcile them against what attendees should have paid."""
import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations
from zoneinfo import ZoneInfo

from . import bank, config, db, ledger, sync
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
              p.cardholder_name, p.note, p.receipt_url, p.order_id)
    if row and sync.locked(conn, "SELECT locked_by FROM tournament WHERE id=?", row["tournament_id"]):
        return  # its tournament is locked by a bank reconciliation (#28)
    if row:
        conn.execute("""UPDATE square_payment SET amount_cents=?, fee_cents=?, created_at=?, source_type=?, card_brand=?,
                        last_4=?, fingerprint=?, cardholder_name=?, note=?, receipt_url=?, order_id=? WHERE id=?""",
                     (*fields, p.id))
        counts["updated" if tournament_id in (None, row["tournament_id"]) else "elsewhere"] += 1
        return
    if tournament_id is None:
        return
    player = _auto_player(conn, p.fingerprint) if p.amount_cents > 0 else None  # not a refunded one
    paid_out_by = conn.execute(  # an imported Square payout that already listed this payment
        "SELECT deposit_id FROM square_payout_entry WHERE payment_id=? AND type='CHARGE' LIMIT 1", (p.id,)).fetchone()
    conn.execute("""INSERT INTO square_payment (id, tournament_id, player_id, link, amount_cents, fee_cents, created_at,
                    source_type, card_brand, last_4, fingerprint, cardholder_name, note, receipt_url, order_id,
                    deposit_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    fetched = fetch_line_items(conn, client, tournament_ids)
    if fetched is not None:
        counts["orders"] = fetched
    return counts


# --- what each payment was for (#31) ----------------------------------------

FEE_MEANINGS = ("venue", "bracket", "catchup")  # count towards the takings; 'other' is not a fee
MEANINGS = {"venue": "venue fee", "bracket": "bracket fee", "catchup": "catch-up", "other": "not a fee"}
NO_CATEGORY, CUSTOM_AMOUNT = "(none)", "(custom)"


def fetch_line_items(conn, client, tournament_ids) -> int | None:
    """Fetch the orders of the tournaments' payments not fetched yet, and the item library if they sell anything it
    doesn't know. None when the Square grant doesn't cover orders and items (it needs authorising again)."""
    allowed = getattr(client, "line_items_allowed", None)
    if allowed is None or not allowed():
        return None
    ids = list(tournament_ids)
    marks = ",".join("?" * len(ids))
    orders = [r[0] for r in conn.execute(
        f"""SELECT DISTINCT sp.order_id FROM square_payment sp JOIN tournament t ON t.id=sp.tournament_id
            WHERE sp.tournament_id IN ({marks}) AND t.locked_by IS NULL AND sp.order_id != ''
            AND sp.order_id NOT IN (SELECT id FROM square_order)""", ids)] if ids else []
    for order_id in orders:
        for li in client.retrieve_order(order_id):
            conn.execute("""INSERT OR REPLACE INTO square_order_line (order_id, uid, name, variation_name, quantity,
                            item_type, catalog_object_id, note, total_cents) VALUES (?,?,?,?,?,?,?,?,?)""",
                         (order_id, li.uid, li.name, li.variation_name, li.quantity, li.item_type, li.catalog_object_id,
                          li.note, li.total_cents))
        conn.execute("INSERT OR REPLACE INTO square_order (id, fetched_at) VALUES (?,?)", (order_id, sync.now()))
    if conn.execute("""SELECT 1 FROM square_order_line WHERE catalog_object_id != ''
                       AND catalog_object_id NOT IN (SELECT id FROM square_variation) LIMIT 1""").fetchone():
        refresh_catalog(conn, client)
    return len(orders)


def refresh_catalog(conn, client):
    """Store the item library's variations (with their item's category) and category names. Meanings are kept."""
    items, names = client.list_catalog()
    for cid, name in names.items():
        conn.execute("INSERT INTO square_category (id, name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                     (cid, name))
    for item in items:
        for vid in item.variation_ids:
            conn.execute("INSERT OR REPLACE INTO square_variation (id, item_name, category_id) VALUES (?,?,?)",
                         (vid, item.name, item.category_id))


def item_key(line) -> str:
    """What a line item's meaning is filed under (#33): its name, ignoring case and spaces; custom amounts share one."""
    if line["item_type"] == "CUSTOM_AMOUNT" or not line["catalog_object_id"]:
        return CUSTOM_AMOUNT
    return " ".join((line["name"] or "").split()).lower() or NO_CATEGORY


def order_lines(conn, order_ids) -> dict:
    """Line items by order id, each with its Square category name (for reference) and its item's meaning (None: not
    chosen yet)."""
    ids = sorted({o for o in order_ids if o})
    if not ids:
        return {}
    out = {}
    for r in conn.execute(
            f"""SELECT l.*, COALESCE(c.name, '') AS category FROM square_order_line l
                LEFT JOIN square_variation v ON v.id=l.catalog_object_id LEFT JOIN square_category c ON c.id=v.category_id
                WHERE l.order_id IN ({",".join("?" * len(ids))}) ORDER BY l.order_id, l.uid""", ids):
        out.setdefault(r["order_id"], []).append(dict(r))
    meanings = {r["key"]: r["meaning"] for r in conn.execute("SELECT key, meaning FROM square_item")}
    for lines in out.values():
        for line in lines:
            line["item_key"] = item_key(line)
            line["meaning"] = meanings.get(line["item_key"])
    return out


def _split(amount: int, lines: list) -> dict:
    """A payment's amount split by what its order's lines were for (pro rata, so a split tender or a partial refund
    shares them out): {meaning or None (not chosen yet): cents}."""
    total = sum(l["total_cents"] for l in lines)
    if not lines or total <= 0:
        return {}
    parts = {}
    for l in lines:
        parts[l["meaning"]] = parts.get(l["meaning"], 0) + l["total_cents"]
    out = {m: round(amount * c / total) for m, c in parts.items()}
    biggest = max(out, key=lambda m: parts[m])
    out[biggest] += amount - sum(out.values())  # rounding: the parts add up to the payment
    return out


def item_meanings(conn) -> list[dict]:
    """Every item the pulled orders sold, with its category, variations, how much was sold and its meaning (#33)."""
    lines = order_lines(conn, [r[0] for r in conn.execute("SELECT DISTINCT order_id FROM square_order_line")])
    seen = {}
    for line in (l for ls in lines.values() for l in ls):
        key = line["item_key"]
        i = seen.setdefault(key, {"key": key, "name": "Custom amount" if key == CUSTOM_AMOUNT else line["name"] or "?",
                                  "meaning": line["meaning"], "lines": 0, "cents": 0, "categories": set(),
                                  "variations": set()})
        i["lines"] += 1
        i["cents"] += line["total_cents"]
        if line["category"]:
            i["categories"].add(line["category"])
        if line["variation_name"]:
            i["variations"].add(line["variation_name"])
    return sorted(({**i, "categories": sorted(i["categories"]), "variations": sorted(i["variations"])[:6]}
                   for i in seen.values()), key=lambda i: (i["meaning"] is not None, i["name"].lower()))


def set_meaning(conn, key: str, meaning: str | None, name: str = ""):
    if meaning is not None and meaning not in MEANINGS:
        raise RuleError("Unknown meaning")
    conn.execute("""INSERT INTO square_item (key, name, meaning) VALUES (?,?,?)
                    ON CONFLICT(key) DO UPDATE SET meaning=excluded.meaning""", (key, name, meaning))


def pull_tournament(conn, client: SquareClient, tournament_id: str) -> dict:
    """Store one tournament's completed Square payments; see pull_tournaments."""
    t = conn.execute("SELECT * FROM tournament WHERE id=?", (tournament_id,)).fetchone()
    if not t["start_date"]:
        raise RuleError("This tournament has no date, so its Square payments can't be found")
    check_unlocked(conn, tournament_id)
    return pull_tournaments(conn, client, [tournament_id])


def pull_series(conn, client: SquareClient, series_id: int, since: str | None = None) -> dict:
    """Pull every dated, non-excluded tournament of a series (only those on or after `since`, if given) that isn't
    locked by a bank reconciliation."""
    ids = [r[0] for r in conn.execute(
        """SELECT id FROM tournament WHERE series_id=? AND excluded=0 AND COALESCE(start_date, '') != ''
           AND locked_by IS NULL
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


def payment_rows(conn, tournament_id: str | None = None) -> list[dict]:
    """A tournament's Square payments (every tournament's if None), oldest first, each with the player it's linked to
    after merges (pid), its line items, its amount split by what they were for (parts) and how much of it counts
    toward the takings (counted_cents): only its fee lines count (#31); lines not given a meaning yet still do."""
    payments = [dict(p) for p in conn.execute(
        """SELECT sp.*, COALESCE(a.merged_into, sp.player_id) AS pid, p.gamer_tag
           FROM square_payment sp LEFT JOIN player a ON a.id=sp.player_id
           LEFT JOIN player p ON p.id=COALESCE(a.merged_into, sp.player_id)
           WHERE ? IS NULL OR sp.tournament_id=? ORDER BY sp.created_at, sp.id""", (tournament_id, tournament_id))]
    lines = order_lines(conn, [p["order_id"] for p in payments])
    for p in payments:
        p["lines"] = lines.get(p["order_id"], [])
        p["parts"] = _split(p["amount_cents"], p["lines"]) if p["amount_cents"] > 0 and not p["ignored"] else {}
        p["counted_cents"] = (p["amount_cents"] - p["parts"].get("other", 0)) if p["parts"] else p["amount_cents"]
    return payments


def reconcile(conn, tournament_id: str, books: dict | None = None) -> dict:
    """A tournament's takings: its Square payments, and each entrant's charge from the ledger (#37): what they owed
    (plus what they paid for others), what their payments that night came to, how it was paid and what's left.

    Payments marked "not a fee" (ignored) don't count, and neither do fully refunded ones (stored at $0), apart from
    their card fees: Square's payment data doesn't say whether it gave those back. Payments marked "no player needed"
    count, but aren't asked to be linked (#30). A player whose payment there went to other weeks or credit (they didn't
    enter, or paid extra) is listed too."""
    b = books or ledger.books(conn)
    payments = payment_rows(conn, tournament_id)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, COALESCE(start_date, name) FROM tournament")}
    for p in payments:  # where its money went, if not (all) to its own night: older weeks, or credit
        p["elsewhere"] = [{"tournament_id": tid, "day": names.get(tid), "cents": cents, "kind": kind}
                          for tid, cents, kind in b["payments"].get(p["id"], []) if tid != tournament_id]
    counted = [p for p in payments if not p["ignored"] and p["amount_cents"] > 0 and p["counted_cents"] > 0]
    refunded = [p for p in payments if not p["ignored"] and p["amount_cents"] <= 0]
    tonight = {}
    for p in counted:
        if p["pid"]:
            tonight[p["pid"]] = tonight.get(p["pid"], 0) + p["counted_cents"]
    tags = {r["id"]: r["gamer_tag"] for r in conn.execute(
        "SELECT p.id, p.gamer_tag FROM tournament_entry_c te JOIN player p ON p.id=te.player_id WHERE te.tournament_id=?",
        (tournament_id,))}
    for p in counted:  # a card's payments link to its player even where they weren't entered (#22)
        if p["pid"]:
            tags.setdefault(p["pid"], p["gamer_tag"])
    settlements = {}
    for r in conn.execute("""SELECT s.*, COALESCE(a.merged_into, s.player_id) AS pid, pb.gamer_tag AS paid_by_tag
                             FROM settlement s LEFT JOIN player a ON a.id=s.player_id
                             LEFT JOIN player pb ON pb.id=COALESCE((SELECT merged_into FROM player WHERE id=s.paid_by),
                                                                   s.paid_by)
                             WHERE s.tournament_id=? ORDER BY s.id""", (tournament_id,)):
        settlements.setdefault(r["pid"], []).append(dict(r))
    charges = {pid: c for (tid, pid), c in b["charges"].items() if tid == tournament_id}
    players = []
    for pid in set(charges) | set(tonight):
        c = charges.get(pid) or ledger.charge_row(tournament_id, pid, "")
        players.append({**c, "id": pid, "tag": tags.get(pid, pid), "expected": c["due"] + c["covers"],
                        "paid": tonight.get(pid, 0), "state": c["state"] or "not_entered",
                        "settlements": settlements.get(pid, [])})
    order = {"owing": 0, "unmatched": 1, "covered": 2}
    players.sort(key=lambda r: (order.get(r["state"], 3), r["tag"].lower()))
    t = b["tournaments"].get(tournament_id)
    if t is None:  # takings not tracked (e.g. nothing pulled yet): what entrants would owe, for reference
        t = {**ledger.tournament_row(), "due": sum(expected_by_player(conn, tournament_id).values())}
        attendees = conn.execute("SELECT COUNT(*) FROM tournament_entry_c WHERE tournament_id=?",
                                 (tournament_id,)).fetchone()[0]
    else:
        attendees = len(charges)
    by_meaning = {}
    for p in payments:
        for m, cents in p["parts"].items():
            by_meaning[m] = by_meaning.get(m, 0) + cents
    unlinked = [p for p in counted if not p["pid"]]
    to_link = [p for p in unlinked if not p["no_player"]]
    takings = [p for p in payments if not p["ignored"]]  # card fees and banking cover whole payments, drinks too
    fees = sum(p["fee_cents"] or 0 for p in takings)
    sold = sum(p["amount_cents"] for p in takings if p["amount_cents"] > 0)
    taken = sum(p["counted_cents"] for p in counted)
    return {"payments": payments, "players": players, "tracked": tournament_id in b["tournaments"],
            "expected": t["due"], "taken": taken, "difference": taken - t["due"],
            "fees": fees, "net": sold - fees, "sold": sold, "by_meaning": by_meaning,
            "banked": sum(p["amount_cents"] - (p["fee_cents"] or 0) for p in takings if p["deposit_id"]),
            "fees_pending": sum(1 for p in counted if p["fee_cents"] is None),
            "unmapped": len({l["item_key"] for p in payments for l in p["lines"] if l["meaning"] is None}),
            "attendees": attendees, "count": len(counted), "unlinked": len(to_link),
            "unlinked_cents": sum(p["amount_cents"] for p in to_link), "no_player": len(unlinked) - len(to_link),
            "anonymous": t["anonymous"], "gap": t["gap"], "written_off": t["written_off"],
            "owing": sum(1 for r in players if r["state"] == "owing"),
            "owing_cents": sum(r["balance"] for r in players if r["state"] == "owing"),
            "unmatched": sum(1 for r in players if r["state"] == "unmatched"),
            "refunded": len(refunded), "refunded_fees": sum(p["fee_cents"] or 0 for p in refunded)}


def suggestions(conn, tournament_id: str, recon: dict) -> list[dict]:
    """Who a player's extra money on the night was probably for (#51). A player whose payments that night came to more
    than they owed has the rest as credit; when other entrants that night still owe, suggest which of them it paid
    for: combinations of up to three whose balances add up to the extra exactly, or failing that, the closest under
    it. Pairs where the payer has paid for that player before come first. Only ever a suggestion: confirming records
    each as paid by another player (ledger.settle)."""
    extra = {}
    for p in recon["payments"]:
        if p["pid"]:
            extra[p["pid"]] = extra.get(p["pid"], 0) + sum(a["cents"] for a in p["elsewhere"] if a["kind"] == "credit")
    debtors = [r for r in recon["players"] if r["state"] in ("owing", "unmatched") and r["balance"] > 0]
    if not debtors:
        return []
    tags = {r["id"]: r["tag"] for r in recon["players"]}
    before = {(r[0], r[1]) for r in conn.execute(
        """SELECT COALESCE(pb.merged_into, s.paid_by), COALESCE(pa.merged_into, s.player_id) FROM settlement s
           JOIN player pb ON pb.id=s.paid_by JOIN player pa ON pa.id=s.player_id WHERE s.method='other_player'""")}
    largest = 3 if len(debtors) <= 25 else 2
    out = []
    for payer, cents in sorted(extra.items(), key=lambda x: -x[1]):
        if cents <= 0:
            continue
        options = [r for r in debtors if r["id"] != payer]
        ranked = []
        for size in range(1, largest + 1):
            for combo in combinations(options, size):
                total = sum(r["balance"] for r in combo)
                if total <= cents:
                    known = sum((payer, r["id"]) in before for r in combo)
                    ranked.append(((total == cents, known, total, -size), combo, total))
        ranked.sort(key=lambda x: x[0], reverse=True)
        exact = [x for x in ranked if x[2] == cents]
        for _, combo, total in (exact[:3] or ranked[:1]):
            out.append({"payer": payer, "payer_tag": tags.get(payer, payer), "extra": cents, "total": total,
                        "exact": total == cents, "players": [{"id": r["id"], "tag": r["tag"], "balance": r["balance"],
                                                              "before": (payer, r["id"]) in before} for r in combo]})
    return out


def takings_issues(conn, series_id: int | None = None, books: dict | None = None) -> list[dict]:
    """Tracked tournaments whose takings need attention on the night's own terms (#37), latest first, each with a
    `kind`: "link", payments still to link to players (or mark "no player needed"); or "gap", money missing that
    isn't owed by anyone named, not written off. Players who owe don't count: what they owe is followed up on its own
    (ledger.owing). Excluded tournaments, and ones whose takings aren't tracked, are skipped."""
    b = books or ledger.books(conn)
    out = []
    for t in conn.execute("""SELECT id, name, series_id, start_date FROM tournament WHERE (? IS NULL OR series_id=?)
                             ORDER BY start_date DESC""", (series_id, series_id)):
        x = b["tournaments"].get(t["id"])
        if x is None or not (x["to_link"] or x["gap"]):
            continue
        out.append({"tournament_id": t["id"], "tournament_name": t["name"], "series_id": t["series_id"],
                    "kind": "link" if x["to_link"] else "gap", "unlinked": x["to_link"],
                    "unlinked_cents": x["to_link_cents"], "gap": x["gap"]})
    return out


def lock_clear_tournaments(conn) -> int:
    """Lock the tournaments up to the latest bank reconciliation that are clear of issues: no payments to link or money
    missing that nobody named owes, no events with unfinalised placements (#27, #37). Players who still owe don't hold
    it open: their debts can be paid later all the same. A locked tournament's figures, charges and payment links can't
    change, and imports skip it (#28), until that reconciliation is undone. Returns how many were locked."""
    last = bank.latest(conn)
    if last is None:
        return 0
    flagged = {i["tournament_id"] for i in takings_issues(conn)} | {i["tournament_id"] for i in sync.event_issues(conn)}
    ids = [r[0] for r in conn.execute(
        """SELECT id FROM tournament WHERE locked_by IS NULL AND excluded=0 AND COALESCE(start_date, '') != ''
           AND start_date <= ?""", (last["reconciled_on"],)) if r[0] not in flagged]
    conn.executemany("UPDATE tournament SET locked_by=? WHERE id=?", [(last["id"], tid) for tid in ids])
    for tid in ids:  # what its entrants owe stops following its entries and rules (#36)
        ledger.freeze(conn, tid)
    return len(ids)


def check_unlocked(conn, tournament_id: str):
    t = conn.execute("SELECT locked_by FROM tournament WHERE id=?", (tournament_id,)).fetchone()
    if t and t["locked_by"] is not None:
        raise RuleError(bank.locked_message(conn, t["locked_by"]))


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
