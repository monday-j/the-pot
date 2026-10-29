"""Manual bank reconciliation: deposits typed in from the bank statement, matched to the Square payments they paid out.

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


def summary(conn, deposit) -> dict:
    """A deposit with the payments matched to it: matched net vs the deposited amount."""
    matched = _rows(conn, "sp.deposit_id=?", (deposit["id"],))
    matched_net = sum(p["net"] for p in matched)
    return {"d": deposit, "payments": matched, "matched": matched_net, "difference": deposit["amount_cents"] - matched_net,
            "fees_pending": sum(1 for p in matched if p["fee_cents"] is None)}


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


def match(conn, deposit_id: int, payment_ids):
    """Put payments in a deposit. A payment already in another deposit is left there."""
    for pid in payment_ids:
        conn.execute("UPDATE square_payment SET deposit_id=? WHERE id=? AND deposit_id IS NULL", (deposit_id, pid))


def unmatch(conn, deposit_id: int, payment_id: str):
    conn.execute("UPDATE square_payment SET deposit_id=NULL WHERE id=? AND deposit_id=?", (payment_id, deposit_id))
