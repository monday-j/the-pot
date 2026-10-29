"""Bank reconciliation: deposits, matched to the Square payments they paid out.

Deposits are typed in from the bank statement, or imported from Square's payouts (each with Square's own list of the
payments, refunds and fees it covered); an imported payout is "expected" until it's ticked as seen in the bank.
Square pays takings out net of its processing fees, usually one deposit per business day. Every stored Square payment
counts here, ignored ones included (it's still money Square deposited), net of refunds and fees.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config
from .payouts import RuleError

LOOKBACK_DAYS = 10  # payments up to this many days before a deposit are offered as its candidates
OVERDUE_DAYS = 3  # unbanked payments older than this are flagged


def local_day(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return when.astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat()


def net(p) -> int:
    return p["amount_cents"] - (p["fee_cents"] or 0)


PAYMENT_SQL = """SELECT sp.*, t.name AS tname, p.gamer_tag FROM square_payment sp JOIN tournament t ON t.id=sp.tournament_id
                 LEFT JOIN player p ON p.id=sp.player_id"""


def _rows(conn, where: str, args=()) -> list[dict]:
    return [{**dict(r), "day": local_day(r["created_at"]), "net": net(r)}
            for r in conn.execute(f"{PAYMENT_SQL} WHERE {where} ORDER BY sp.created_at", args)]


def _flag(deposit, today: str) -> str:
    """What's wrong with an imported payout, if anything: it bounced, or it hasn't been seen in the bank in time."""
    if not deposit["square_payout_id"]:
        return ""
    if deposit["payout_status"] == "FAILED":
        return "payout failed"
    overdue = (date.fromisoformat(today) - timedelta(days=OVERDUE_DAYS)).isoformat()
    if not deposit["seen_in_bank"] and deposit["deposited_on"] < overdue:
        return "not seen in the bank"
    return ""


def summary(conn, deposit, today: str | None = None) -> dict:
    """A deposit with what it covers: matched net vs the deposited amount.

    For an imported payout, Square's entries are the breakdown: payments we have (filed under tournaments), other
    Square takings (payments we never pulled, e.g. outside tournament times) and the rest (refunds, fees, adjustments).
    """
    today = today or datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    matched = _rows(conn, "sp.deposit_id=?", (deposit["id"],))
    out = {"d": deposit, "payments": matched, "fees_pending": sum(1 for p in matched if p["fee_cents"] is None),
           "imported": bool(deposit["square_payout_id"]), "flag": _flag(deposit, today)}
    if not out["imported"]:
        out["matched"] = sum(p["net"] for p in matched)
    else:
        entries = [dict(r) for r in conn.execute(
            """SELECT e.*, sp.tournament_id, t.name AS tname, sp.player_id, p.gamer_tag, sp.deposit_id AS paid_out_by
               FROM square_payout_entry e LEFT JOIN square_payment sp ON sp.id=e.payment_id AND e.payment_id != ''
               LEFT JOIN tournament t ON t.id=sp.tournament_id LEFT JOIN player p ON p.id=sp.player_id
               WHERE e.deposit_id=? ORDER BY e.effective_at, e.id""", (deposit["id"],))]
        for e in entries:
            e["group"] = "tournament" if e["tournament_id"] else "other" if e["type"] == "CHARGE" else "adjustment"
        out["entries"] = entries
        out["matched"] = sum(e["net_cents"] for e in entries)
        for group in ("tournament", "other", "adjustment"):
            rows = [e for e in entries if e["group"] == group]
            out[group] = {"count": len(rows), "net": sum(e["net_cents"] for e in rows)}
    out["difference"] = deposit["amount_cents"] - out["matched"]
    return out


def candidates(conn, deposit) -> list[dict]:
    """Unbanked payments from the days before the deposit. The suggestion ticks whole days, working back from the
    deposit date, while they still fit in it: Square usually pays out the latest business day's takings in one go."""
    day = date.fromisoformat(deposit["deposited_on"])
    earliest = (day - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = [p for p in _rows(conn, "sp.deposit_id IS NULL") if earliest <= p["day"] < deposit["deposited_on"]]
    by_day = {}
    for p in rows:
        by_day.setdefault(p["day"], []).append(p)
    total, open_ = 0, True
    for d in sorted(by_day, reverse=True):
        day_net = sum(p["net"] for p in by_day[d])
        fits = open_ and total + day_net <= deposit["amount_cents"]
        total += day_net if fits else 0
        open_ = fits
        for p in by_day[d]:
            p["suggested"] = fits
    return rows


def unbanked(conn, today: str | None = None) -> list[dict]:
    """Payments in no deposit yet, grouped by day, with a flag for days older than OVERDUE_DAYS."""
    today = today or datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    overdue = (date.fromisoformat(today) - timedelta(days=OVERDUE_DAYS)).isoformat()
    days = {}
    for p in _rows(conn, "sp.deposit_id IS NULL"):
        d = days.setdefault(p["day"], {"day": p["day"], "count": 0, "gross": 0, "fees": 0, "net": 0, "pending": 0,
                                       "tournaments": set()})
        d["count"] += 1
        d["gross"] += p["amount_cents"]
        d["fees"] += p["fee_cents"] or 0
        d["net"] += p["net"]
        d["pending"] += p["fee_cents"] is None
        d["tournaments"].add((p["tournament_id"], p["tname"]))
    return [{**d, "tournaments": sorted(d["tournaments"], key=lambda t: t[1]), "overdue": d["day"] < overdue}
            for d in sorted(days.values(), key=lambda d: d["day"])]


def save_deposit(conn, deposit_id, deposited_on: str, amount_cents: int, reference: str, note: str) -> int:
    if amount_cents <= 0:
        raise RuleError("A deposit must be more than $0")
    values = (deposited_on, amount_cents, reference.strip(), note.strip())
    if deposit_id is None:
        return conn.execute("INSERT INTO bank_deposit (deposited_on, amount_cents, reference, note) VALUES (?,?,?,?)",
                            values).lastrowid
    conn.execute("UPDATE bank_deposit SET deposited_on=?, amount_cents=?, reference=?, note=? WHERE id=?",
                 (*values, deposit_id))
    return deposit_id


def square_locations(conn) -> list[str]:
    """The seller's main location ("") plus any location set on a venue."""
    return [""] + [r[0] for r in conn.execute(
        "SELECT DISTINCT square_location_id FROM venue WHERE square_location_id != '' ORDER BY 1")]


def _match_entries(conn, deposit_id: int) -> int:
    """Match stored payments to the imported payout that paid them out (by its CHARGE entries). A payment already
    in a deposit, e.g. matched by hand, is never moved."""
    return conn.execute("""UPDATE square_payment SET deposit_id=? WHERE deposit_id IS NULL AND id IN (
                               SELECT payment_id FROM square_payout_entry WHERE deposit_id=? AND type='CHARGE')""",
                        (deposit_id, deposit_id)).rowcount


def import_payouts(conn, client, begin_time: str, end_time: str) -> dict:
    """Import Square's payouts sent in the range as deposits, with their entries, and match their payments.

    Idempotent. To keep Square calls low, a payout's entries are only fetched when the payout is new or Square has
    changed it (its version or status). A payout's date is when it arrives in the bank (Square's arrival_date).
    """
    counts = {"new": 0, "updated": 0, "unchanged": 0, "matched": 0}
    for location in square_locations(conn):
        for p in client.list_payouts(begin_time, end_time, location):
            row = conn.execute("SELECT * FROM bank_deposit WHERE square_payout_id=?", (p.id,)).fetchone()
            if row and row["payout_version"] == p.version and row["payout_status"] == p.status:
                counts["unchanged"] += 1
                continue
            day = p.arrival_date or (local_day(p.created_at) if p.created_at else date.today().isoformat())
            if row:
                deposit_id = row["id"]
                conn.execute("""UPDATE bank_deposit SET deposited_on=?, amount_cents=?, payout_status=?, payout_version=?
                                WHERE id=?""", (day, p.amount_cents, p.status, p.version, deposit_id))
                counts["updated"] += 1
            else:
                deposit_id = conn.execute(
                    """INSERT INTO bank_deposit (deposited_on, amount_cents, reference, square_payout_id, payout_status,
                       payout_version, seen_in_bank) VALUES (?,?,?,?,?,?,0)""",
                    (day, p.amount_cents, "Square payout", p.id, p.status, p.version)).lastrowid
                counts["new"] += 1
            conn.execute("DELETE FROM square_payout_entry WHERE deposit_id=?", (deposit_id,))
            for e in client.list_payout_entries(p.id):
                conn.execute("""INSERT OR REPLACE INTO square_payout_entry (id, deposit_id, type, payment_id, effective_at,
                                gross_cents, fee_cents, net_cents) VALUES (?,?,?,?,?,?,?,?)""",
                             (e.id, deposit_id, e.type, e.payment_id, e.effective_at, e.gross_cents, e.fee_cents,
                              e.net_cents))
            counts["matched"] += _match_entries(conn, deposit_id)
    return counts


def set_seen(conn, deposit_id: int, seen: bool):
    conn.execute("UPDATE bank_deposit SET seen_in_bank=? WHERE id=? AND square_payout_id IS NOT NULL",
                 (int(seen), deposit_id))


def match(conn, deposit_id: int, payment_ids):
    """Put payments in a deposit. A payment already in another deposit is left there."""
    for pid in payment_ids:
        conn.execute("UPDATE square_payment SET deposit_id=? WHERE id=? AND deposit_id IS NULL", (deposit_id, pid))


def unmatch(conn, deposit_id: int, payment_id: str):
    conn.execute("UPDATE square_payment SET deposit_id=NULL WHERE id=? AND deposit_id=?", (payment_id, deposit_id))
