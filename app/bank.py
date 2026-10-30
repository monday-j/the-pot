"""Bank reconciliation: the bank balance, entered by hand, against what the app expects it to be (#27).

Money in is the deposits (Square's payouts, imported, each with Square's own list of the payments, refunds and fees it
covered, and any recorded by hand) and players' bank transfers (ledger receipts). Money out is the payouts to players
and venues marked paid, on the day they were sent. Anything else through the account is a bank adjustment, with a
category saying what it was (#39). The expected balance is the last reconciliation's balance plus what came in and
minus what went out since; the first reconciliation is the opening balance. A balance that doesn't match has to be
explained with adjustments before it's locked, or locked anyway, which files the rest as "unexplained" with a note.

Each reconciliation takes in every movement dated on or before its date that no earlier one took in, and locks it:
what's taken in can't be changed until the reconciliation is undone (only the latest can be, one at a time). Tournaments
up to its date lock too once they're clear of issues (square.lock_clear_tournaments).
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import audit, config
from .payouts import RuleError

# What money through the bank that nothing else records was (#39).
CATEGORIES = {"cash_deposit": "cash deposited", "bank_fee": "bank fee", "interest": "interest",
              "owner_in": "my own money in", "owner_out": "my own money out", "refund_to_player": "refund to a player",
              "other_income": "other income", "other_expense": "other expense", "unexplained": "unexplained"}


def local_day(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return when.astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat()


def net(p) -> int:
    return p["amount_cents"] - (p["fee_cents"] or 0)


def today() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()


def latest(conn):
    """The latest reconciliation, or None."""
    return conn.execute("SELECT * FROM reconciliation ORDER BY reconciled_on DESC, id DESC LIMIT 1").fetchone()


def check_open(conn, day: str):
    """Refuse a change to money dated on or before the latest reconciliation."""
    r = latest(conn)
    if r and day <= r["reconciled_on"]:
        raise RuleError(f"{day} is locked by the bank reconciliation of {r['reconciled_on']}: undo it first")


def locked_message(conn, reconciliation_id) -> str:
    r = conn.execute("SELECT reconciled_on FROM reconciliation WHERE id=?", (reconciliation_id,)).fetchone()
    return f"Locked by the bank reconciliation of {r[0] if r else '?'}: undo it on the Bank page first"


def movements(conn, upto: str | None = None) -> dict:
    """Money in and out that no reconciliation has taken in yet, dated on or before `upto` (any date if None)."""
    deposits = [dict(r) for r in conn.execute(
        """SELECT * FROM bank_deposit WHERE reconciliation_id IS NULL AND payout_status != 'FAILED'
           AND (? IS NULL OR deposited_on <= ?) ORDER BY deposited_on, id""", (upto, upto))]
    receipts = [dict(r) for r in conn.execute(
        """SELECT r.*, p.gamer_tag FROM receipt r LEFT JOIN player p ON p.id=r.player_id
           WHERE r.method='bank' AND r.reconciliation_id IS NULL AND (? IS NULL OR r.received_on <= ?)
           ORDER BY r.received_on, r.id""", (upto, upto))]
    adjustments = [dict(r) for r in conn.execute(
        """SELECT * FROM bank_adjustment WHERE reconciliation_id IS NULL AND (? IS NULL OR occurred_on <= ?)
           ORDER BY occurred_on, id""", (upto, upto))]
    paid_day = lambda r: r["paid_on"] or local_day(r["paid_at"])
    players = [{**dict(r), "day": paid_day(r)} for r in conn.execute(
        """SELECT po.id, po.amount_cents, po.paid_at, po.paid_on, po.note, po.player_id, p.gamer_tag, e.name AS ename,
                  t.id AS tournament_id, t.name AS tname
           FROM payout po JOIN player p ON p.id=po.player_id JOIN event e ON e.id=po.event_id
           JOIN tournament t ON t.id=e.tournament_id
           WHERE po.status='paid' AND po.reconciliation_id IS NULL ORDER BY po.paid_at""")]
    venues = [{**dict(r), "day": paid_day(r)} for r in conn.execute(
        """SELECT vp.tournament_id, vp.amount_cents, vp.paid_at, vp.paid_on, vp.note, vp.venue_id,
                  COALESCE(v.name, '(no venue)') AS venue_name, t.name AS tname
           FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id LEFT JOIN venue v ON v.id=vp.venue_id
           WHERE vp.status='paid' AND vp.reconciliation_id IS NULL ORDER BY vp.paid_at""")]
    if upto:
        players = [r for r in players if r["day"] <= upto]
        venues = [r for r in venues if r["day"] <= upto]
    money_in = (sum(d["amount_cents"] for d in deposits) + sum(r["amount_cents"] for r in receipts)
                + sum(a["amount_cents"] for a in adjustments if a["amount_cents"] > 0))
    money_out = (sum(r["amount_cents"] for r in players) + sum(r["amount_cents"] for r in venues)
                 - sum(a["amount_cents"] for a in adjustments if a["amount_cents"] < 0))
    return {"deposits": deposits, "receipts": receipts, "adjustments": adjustments, "players": players,
            "venues": venues, "in": money_in, "out": money_out,
            "adjusted": sum(a["amount_cents"] for a in adjustments)}


def expected(conn, day: str) -> dict:
    """What the balance should be on `day`: the last reconciled balance, plus money in, minus money out since.
    None for the first reconciliation, which sets the opening balance."""
    last = latest(conn)
    moved = movements(conn, day)
    return {**moved, "previous": last,
            "expected": None if last is None else last["balance_cents"] + moved["in"] - moved["out"]}


def _stamp() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")


def reconcile(conn, day: str, balance_cents: int, note: str, anyway: bool = False) -> int:
    """Record the bank balance on `day` and lock everything up to it.

    The balance has to match what's expected, adjustments included: explain any difference first (add_adjustment).
    With `anyway`, the difference left is filed as an "unexplained" adjustment, which needs a note saying so. The
    reconciliation keeps the balance expected before adjustments, so its difference is what they explained."""
    last = latest(conn)
    if last and day <= last["reconciled_on"]:
        raise RuleError(f"Reconcile after {last['reconciled_on']}, the latest reconciliation (or undo it)")
    if day > today():
        raise RuleError("You can't reconcile a day that hasn't happened yet")
    e = expected(conn, day)
    left = 0 if e["expected"] is None else balance_cents - e["expected"]
    if left and not anyway:
        raise RuleError(f"The balance is {abs(left) / 100:,.2f} {'more' if left > 0 else 'less'} than expected: "
                        "explain the difference first, or lock it anyway with a note")
    if left:
        if not note.strip():
            raise RuleError("Say why you're locking it with the difference unexplained")
        conn.execute("""INSERT INTO bank_adjustment (occurred_on, amount_cents, category, note, created_at)
                        VALUES (?,?,?,?,?)""", (day, left, "unexplained", note.strip(), _stamp()))
        e = expected(conn, day)
    rid = conn.execute("""INSERT INTO reconciliation (reconciled_on, balance_cents, expected_cents, note, created_at)
                          VALUES (?,?,?,?,?)""",
                       (day, balance_cents, balance_cents if e["expected"] is None else e["expected"] - e["adjusted"],
                        note.strip(), _stamp())).lastrowid
    audit.record(conn, "reconciled", balance_cents, day + (f": {note.strip()}" if note.strip() else ""),
                 ref=f"reconciliation:{rid}")
    conn.executemany("UPDATE bank_deposit SET reconciliation_id=? WHERE id=?", [(rid, d["id"]) for d in e["deposits"]])
    conn.executemany("UPDATE receipt SET reconciliation_id=? WHERE id=?", [(rid, r["id"]) for r in e["receipts"]])
    conn.executemany("UPDATE bank_adjustment SET reconciliation_id=? WHERE id=?",
                     [(rid, a["id"]) for a in e["adjustments"]])
    conn.executemany("UPDATE payout SET reconciliation_id=? WHERE id=?", [(rid, r["id"]) for r in e["players"]])
    conn.executemany("UPDATE venue_payment SET reconciliation_id=? WHERE tournament_id=?",
                     [(rid, r["tournament_id"]) for r in e["venues"]])
    return rid


def undo_latest(conn):
    """Undo the latest reconciliation: what it took in, and the tournaments it locked, are unlocked."""
    last = latest(conn)
    if last is None:
        raise RuleError("There's no reconciliation to undo")
    conn.execute("DELETE FROM reconciliation WHERE id=?", (last["id"],))
    audit.record(conn, "reconcile_undone", last["balance_cents"], last["reconciled_on"], ref=f"reconciliation:{last['id']}")


def add_adjustment(conn, day: str, cents: int, category: str, note: str = "") -> int:
    """Record money in (+) or out (-) of the bank that nothing else records, with what it was."""
    if category not in CATEGORIES:
        raise RuleError("Choose what it was")
    if not cents:
        raise RuleError("Enter an amount other than $0")
    if category == "unexplained" and not note.strip():
        raise RuleError("Say what you know about it")
    if day > today():
        raise RuleError("That day hasn't happened yet")
    check_open(conn, day)
    aid = conn.execute("""INSERT INTO bank_adjustment (occurred_on, amount_cents, category, note, created_at)
                          VALUES (?,?,?,?,?)""", (day, cents, category, note.strip(), _stamp())).lastrowid
    audit.record(conn, "adjustment", cents, f"{day}: {CATEGORIES[category]}" + (f", {note.strip()}" if note.strip()
                                                                               else ""), ref=f"adjustment:{aid}")
    return aid


def update_adjustment(conn, adjustment_id: int, category: str, note: str):
    """Say what an adjustment was, e.g. once an unexplained one is understood: allowed even once it's locked, since
    the amount and date can't change."""
    if category not in CATEGORIES:
        raise RuleError("Choose what it was")
    if not conn.execute("SELECT 1 FROM bank_adjustment WHERE id=?", (adjustment_id,)).fetchone():
        raise RuleError("Unknown adjustment")
    conn.execute("UPDATE bank_adjustment SET category=?, note=? WHERE id=?", (category, note.strip(), adjustment_id))
    audit.record(conn, "adjustment_changed", None, CATEGORIES[category] + (f", {note.strip()}" if note.strip() else ""),
                 ref=f"adjustment:{adjustment_id}")


def delete_adjustment(conn, adjustment_id: int):
    a = conn.execute("SELECT * FROM bank_adjustment WHERE id=?", (adjustment_id,)).fetchone()
    if a is None:
        raise RuleError("Unknown adjustment")
    if a["reconciliation_id"] is not None:
        raise RuleError(locked_message(conn, a["reconciliation_id"]))
    conn.execute("DELETE FROM bank_adjustment WHERE id=?", (adjustment_id,))
    audit.record(conn, "adjustment_removed", a["amount_cents"], f"{a['occurred_on']}: {CATEGORIES[a['category']]}",
                 ref=f"adjustment:{adjustment_id}")


PAYABLES = {"payout": "id", "venue_payment": "tournament_id"}


def mark_paid(conn, table: str, key, day: str, note: str = ""):
    """Mark a payout or a venue payment paid, sent on `day` (#39): that's the day it leaves the bank."""
    if day > today():
        raise RuleError("That day hasn't happened yet")
    check_open(conn, day)
    conn.execute(f"UPDATE {table} SET status='paid', paid_at=?, paid_on=?, note=? WHERE {PAYABLES[table]}=?",
                 (_stamp(), day, note.strip(), key))
    _audit_payable(conn, table, key, "paid", f"sent {day}" + (f": {note.strip()}" if note.strip() else ""))


def mark_owed(conn, table: str, key):
    """Undo marking it paid, unless a reconciliation has taken it in."""
    row = conn.execute(f"SELECT reconciliation_id FROM {table} WHERE {PAYABLES[table]}=?", (key,)).fetchone()
    if row and row[0]:
        raise RuleError(locked_message(conn, row[0]))
    _audit_payable(conn, table, key, "owed")
    conn.execute(f"""UPDATE {table} SET status='owed', paid_at=NULL, paid_on=NULL, amount_cents=computed_cents
                     WHERE {PAYABLES[table]}=?""", (key,))


def _audit_payable(conn, table: str, key, status: str, detail: str = ""):
    if table == "payout":
        r = conn.execute("""SELECT po.amount_cents, po.player_id, po.placement, e.tournament_id, e.name FROM payout po
                            JOIN event e ON e.id=po.event_id WHERE po.id=?""", (key,)).fetchone()
        if r:
            audit.record(conn, f"payout_{status}", r["amount_cents"], f"place {r['placement']} in {r['name']}"
                         + (f", {detail}" if detail else ""), r["tournament_id"], r["player_id"], f"payout:{key}")
    else:
        r = conn.execute("SELECT amount_cents FROM venue_payment WHERE tournament_id=?", (key,)).fetchone()
        if r:
            audit.record(conn, f"venue_{status}", r["amount_cents"], detail, key, ref="venue")


def migrate_differences(conn):
    """Differences recorded before #39 (a reconciliation whose balance didn't match, with its note) become
    "unexplained" adjustments it already took in, so they can be explained later. Expected balances don't change."""
    for r in conn.execute("""SELECT * FROM reconciliation r WHERE balance_cents != expected_cents
                             AND NOT EXISTS (SELECT 1 FROM bank_adjustment WHERE reconciliation_id=r.id)""").fetchall():
        conn.execute("""INSERT INTO bank_adjustment (occurred_on, amount_cents, category, note, created_at,
                        reconciliation_id) VALUES (?,?,?,?,?,?)""",
                     (r["reconciled_on"], r["balance_cents"] - r["expected_cents"], "unexplained", r["note"],
                      _stamp(), r["id"]))


def history(conn) -> list[dict]:
    """Every reconciliation, latest first, with its difference and what it took in."""
    out = []
    for r in conn.execute("""SELECT r.*,
            (SELECT COUNT(*) FROM tournament WHERE locked_by=r.id) AS tournaments,
            (SELECT COUNT(*) FROM bank_deposit WHERE reconciliation_id=r.id) AS deposits,
            (SELECT COUNT(*) FROM payout WHERE reconciliation_id=r.id)
              + (SELECT COUNT(*) FROM venue_payment WHERE reconciliation_id=r.id) AS paid_out,
            (SELECT COUNT(*) FROM bank_adjustment WHERE reconciliation_id=r.id) AS adjustments,
            (SELECT COALESCE(SUM(amount_cents), 0) FROM bank_adjustment
             WHERE reconciliation_id=r.id AND category='unexplained') AS unexplained
            FROM reconciliation r ORDER BY r.reconciled_on DESC, r.id DESC""").fetchall():
        out.append({**dict(r), "difference": r["balance_cents"] - r["expected_cents"]})
    return out


PAYMENT_SQL = """SELECT sp.*, t.name AS tname, p.gamer_tag FROM square_payment sp JOIN tournament t ON t.id=sp.tournament_id
                 LEFT JOIN player p ON p.id=sp.player_id"""


def summary(conn, deposit) -> dict:
    """A deposit with Square's breakdown of an imported payout: payments we have (filed under tournaments), other
    Square takings (payments we never pulled, e.g. outside tournament times) and the rest (refunds, fees, adjustments).
    """
    out = {"d": deposit, "imported": bool(deposit["square_payout_id"]), "entries": []}
    if out["imported"]:
        entries = [dict(r) for r in conn.execute(
            """SELECT e.*, sp.tournament_id, t.name AS tname, sp.player_id, p.gamer_tag
               FROM square_payout_entry e LEFT JOIN square_payment sp ON sp.id=e.payment_id AND e.payment_id != ''
               LEFT JOIN tournament t ON t.id=sp.tournament_id LEFT JOIN player p ON p.id=sp.player_id
               WHERE e.deposit_id=? ORDER BY e.effective_at, e.id""", (deposit["id"],))]
        for e in entries:
            e["group"] = "tournament" if e["tournament_id"] else "other" if e["type"] == "CHARGE" else "adjustment"
        out["entries"] = entries
        for group in ("tournament", "other", "adjustment"):
            rows = [e for e in entries if e["group"] == group]
            out[group] = {"count": len(rows), "net": sum(e["net_cents"] for e in rows)}
    return out


def save_deposit(conn, deposit_id, deposited_on: str, amount_cents: int, reference: str, note: str) -> int:
    if amount_cents <= 0:
        raise RuleError("A deposit must be more than $0")
    check_open(conn, deposited_on)
    values = (deposited_on, amount_cents, reference.strip(), note.strip())
    if deposit_id is None:
        deposit_id = conn.execute("INSERT INTO bank_deposit (deposited_on, amount_cents, reference, note) "
                                  "VALUES (?,?,?,?)", values).lastrowid
    else:
        conn.execute("UPDATE bank_deposit SET deposited_on=?, amount_cents=?, reference=?, note=? WHERE id=?",
                     (*values, deposit_id))
    audit.record(conn, "deposit", amount_cents, f"{deposited_on} {reference.strip()}", ref=f"deposit:{deposit_id}")
    return deposit_id


def square_locations(conn) -> list[str]:
    """The seller's main location ("") plus any location set on a venue."""
    return [""] + [r[0] for r in conn.execute(
        "SELECT DISTINCT square_location_id FROM venue WHERE square_location_id != '' ORDER BY 1")]


def _match_entries(conn, deposit_id: int) -> int:
    """Match stored payments to the imported payout that paid them out (by its CHARGE entries). A payment already
    in a deposit is never moved."""
    return conn.execute("""UPDATE square_payment SET deposit_id=? WHERE deposit_id IS NULL AND id IN (
                               SELECT payment_id FROM square_payout_entry WHERE deposit_id=? AND type='CHARGE')""",
                        (deposit_id, deposit_id)).rowcount


def import_payouts(conn, client, begin_time: str, end_time: str) -> dict:
    """Import Square's payouts sent in the range as deposits, with their entries, and match their payments.

    Idempotent. To keep Square calls low, a payout's entries are only fetched when the payout is new or Square has
    changed it (its version or status); one a reconciliation took in is locked, so it's left as it is. A payout's date
    is when it arrives in the bank (Square's arrival_date).
    """
    counts = {"new": 0, "updated": 0, "unchanged": 0, "matched": 0}
    for location in square_locations(conn):
        for p in client.list_payouts(begin_time, end_time, location):
            row = conn.execute("SELECT * FROM bank_deposit WHERE square_payout_id=?", (p.id,)).fetchone()
            if row and (row["reconciliation_id"] is not None or
                        (row["payout_version"] == p.version and row["payout_status"] == p.status)):
                counts["unchanged"] += 1  # a reconciled payout is locked: Square's later changes wait for an undo
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
                       payout_version) VALUES (?,?,?,?,?,?)""",
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
