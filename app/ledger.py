"""What each entrant owes for a tournament, and how it was paid (#36).

A tournament whose takings are tracked charges each entrant its venue fee plus the bracket fee of each event they
entered (square.expected_by_player). While it's open that's worked out from its entries and rules; once a bank
reconciliation locks it, the charges are frozen (the charge table).

Who paid what is worked out from scratch every time, so it can't go stale when a payment is linked, refunded or merged.
For each player, in date order:

- what they pay at a tournament (the fee part of their Square payments there) pays that night's charge first, then
  their oldest charges still open from earlier tournaments ("paid late"); anything left is credit,
- credit pays their next charges ("paid by credit"),
- a bank transfer or cash (a receipt) pays the tournament it was for, if given, then their oldest open charges; what's
  left is credit too.

Decisions made by hand are applied first (settlements): a fee waived, written off, paid by another player (who then
owes it instead) or sorted out some other way, and a Square payment pointed at a tournament it pays for.

Card taps carry no names. A tournament with payments still to link to players can't say who hasn't paid yet, so its
open charges are "not matched yet". Payments marked "no player needed" pay its open charges as a whole: what they don't
cover is missing from the tournament (its gap) rather than owed by anyone in particular, until it's linked or written
off.
"""
import json
import sys

from . import bank, db, square, sync
from .payouts import RuleError

# Decisions made by hand, by what they do.
CHARGE_METHODS = {"waived": "waived", "written_off": "written off", "other_player": "paid by another player",
                  "other": "sorted out"}
CREDIT_METHODS = {"income": "kept as income", "refunded": "refunded"}
RECEIPT_METHODS = {"bank": "bank transfer", "cash": "cash"}
STATES = {"paid": "paid", "late": "paid late", "credit": "paid by credit", "settled": "sorted out",
          "written_off": "written off", "owing": "owing", "unmatched": "not matched yet", "covered": "paid, unnamed",
          "not_entered": "didn't enter"}


def tracked(conn) -> list:
    """Tournaments whose takings are tracked: not excluded, and on, or 'auto' with Square payments pulled."""
    return conn.execute(
        """SELECT id, start_date, locked_by FROM tournament t WHERE excluded=0 AND (takings='on' OR (takings='auto'
           AND EXISTS (SELECT 1 FROM square_payment WHERE tournament_id=t.id)))""").fetchall()


def _canonical(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT id, COALESCE(merged_into, id) FROM player")}


def charges(conn) -> dict:
    """{(tournament id, player id): cents due} for every tracked tournament: frozen when it's locked (and was frozen),
    otherwise worked out now."""
    canon = _canonical(conn)
    frozen = {}
    for r in conn.execute("SELECT c.* FROM charge c JOIN tournament t ON t.id=c.tournament_id WHERE t.locked_by IS NOT NULL"):
        key = (r["tournament_id"], canon.get(r["player_id"], r["player_id"]))
        frozen[key] = frozen.get(key, 0) + r["due_cents"]
    frozen_tids = {tid for tid, _ in frozen}
    out = {}
    for t in tracked(conn):
        if t["locked_by"] is not None and t["id"] in frozen_tids:
            out.update({k: v for k, v in frozen.items() if k[0] == t["id"]})
        else:
            out.update({(t["id"], pid): due for pid, due in square.expected_by_player(conn, t["id"]).items()})
    return {k: v for k, v in out.items() if v > 0}


def freeze(conn, tournament_id: str):
    """Store a tournament's charges as they are now, as a reconciliation locks it."""
    conn.execute("DELETE FROM charge WHERE tournament_id=?", (tournament_id,))
    is_tracked = any(t["id"] == tournament_id for t in tracked(conn))
    if is_tracked:
        conn.executemany("INSERT INTO charge (tournament_id, player_id, due_cents) VALUES (?,?,?)",
                         [(tournament_id, pid, due) for pid, due in
                          square.expected_by_player(conn, tournament_id).items()])


def charge_row(tid, pid, date):
    return {"tournament_id": tid, "player_id": pid, "date": date, "due": 0, "covers": 0, "night": 0, "late": 0,
            "credit": 0, "receipt": 0, "settled": {}, "balance": 0, "state": None}


def tournament_row(written_off: int = 0) -> dict:
    return {"due": 0, "to_link": 0, "to_link_cents": 0, "anonymous": 0, "open": 0, "gap": 0, "written_off": written_off,
            "unmatched": 0}


def books(conn) -> dict:
    """Everything owed and paid, worked out now (see the module docstring).

    - charges: {(tournament id, player id): charge}, each with what's due (plus `covers`, paid for others), how it was
      paid (night, late, credit, receipt, settled by method) and what's left (balance), and its state (STATES).
    - payments: {Square payment id: [(tournament id, cents, kind)]}, where its counted money went ("credit" for what's
      left over, with no tournament).
    - receipts: {receipt id: [(tournament id, cents, kind)]}, the same.
    - credit: {player id: cents of credit left}.
    - tournaments: {tournament id: totals}: due, to_link (payments) and to_link_cents, anonymous (paid by payments
      with no player needed), open (named balances left), gap (missing, not owed by anyone named), written_off (of
      the gap), unmatched.
    - issues: problems with decisions made by hand, e.g. a payment pointed at more than it now counts.
    """
    canon = _canonical(conn)
    dates = {r["id"]: (r["start_date"] or "", r["id"]) for r in conn.execute("SELECT id, start_date FROM tournament")}
    out_charges = {}

    def charge(tid, pid):
        if (tid, pid) not in out_charges:
            out_charges[(tid, pid)] = charge_row(tid, pid, dates.get(tid, ("", tid))[0])
        return out_charges[(tid, pid)]

    for (tid, pid), due in charges(conn).items():
        charge(tid, pid)["due"] = due
    payments = [p for p in square.payment_rows(conn) if not p["ignored"] and p["amount_cents"] > 0
                and p["counted_cents"] > 0]
    receipts = [{**dict(r), "player_id": canon.get(r["player_id"], r["player_id"])}
                for r in conn.execute("SELECT * FROM receipt ORDER BY received_on, id")]
    settlements = [{**dict(r), "player_id": canon.get(r["player_id"], r["player_id"]) if r["player_id"] else None,
                    "paid_by": canon.get(r["paid_by"], r["paid_by"]) if r["paid_by"] else None}
                   for r in conn.execute("SELECT * FROM settlement ORDER BY settled_on, id")]
    issues = []
    pay_alloc = {p["id"]: [] for p in payments}
    receipt_alloc = {r["id"]: [] for r in receipts}
    available = {p["id"]: p["counted_cents"] for p in payments}

    # decisions made by hand first
    gap_written_off = {}
    disposals = {}
    for s in settlements:
        if s["method"] in CREDIT_METHODS:
            disposals.setdefault(s["player_id"], []).append(s)
        elif s["player_id"] is None:
            gap_written_off[s["tournament_id"]] = gap_written_off.get(s["tournament_id"], 0) + s["amount_cents"]
        elif s["method"] == "square":
            c = charge(s["tournament_id"], s["player_id"])
            if s["square_payment_id"] not in available:
                issues.append({**s, "problem": "its payment no longer counts"})
                continue
            take = min(s["amount_cents"], available[s["square_payment_id"]])
            if take < s["amount_cents"]:
                issues.append({**s, "problem": "its payment now counts for less than this"})
            available[s["square_payment_id"]] -= take
            c["late"] += take
            pay_alloc[s["square_payment_id"]].append((s["tournament_id"], take, "late"))
        else:
            c = charge(s["tournament_id"], s["player_id"])
            c["settled"][s["method"]] = c["settled"].get(s["method"], 0) + s["amount_cents"]
            if s["method"] == "other_player" and s["paid_by"]:
                charge(s["tournament_id"], s["paid_by"])["covers"] += s["amount_cents"]

    def open_cents(c):
        return c["due"] + c["covers"] - c["night"] - c["late"] - c["credit"] - c["receipt"] - sum(c["settled"].values())

    # then each player's money, in date order
    by_player = {}
    for p in payments:
        if p["pid"]:
            by_player.setdefault(p["pid"], {"payments": [], "receipts": []})["payments"].append(p)
    for r in receipts:
        by_player.setdefault(r["player_id"], {"payments": [], "receipts": []})["receipts"].append(r)
    for (tid, pid) in list(out_charges):
        by_player.setdefault(pid, {"payments": [], "receipts": []})
    credit_left = {}
    for pid, money in by_player.items():
        mine = sorted((c for c in out_charges.values() if c["player_id"] == pid),
                      key=lambda c: dates.get(c["tournament_id"], ("", c["tournament_id"])))
        events = []
        tids = {c["tournament_id"] for c in mine} | {p["tournament_id"] for p in money["payments"]}
        for tid in tids:
            events.append((dates.get(tid, ("", tid)), 0, tid))
        for r in money["receipts"]:
            events.append(((r["received_on"], "~"), 1, r))
        for s in disposals.get(pid, []):
            events.append(((s["settled_on"], "~"), 2, s))
        events.sort(key=lambda e: (e[0], e[1], str(e[2]["id"]) if isinstance(e[2], dict) else e[2]))
        opened, credits = [], []  # charges of tournaments already reached; [cents, source] of credit left

        def pay_older(cents, before_tid, kind, record):
            for c in opened:
                if cents <= 0:
                    break
                if c["tournament_id"] == before_tid:
                    continue
                take = min(cents, open_cents(c))
                if take > 0:
                    c[kind] += take
                    record.append((c["tournament_id"], take, "late"))
                    cents -= take
            return cents

        for _, kind, item in events:
            if kind == 0:
                tid = item
                c = next((c for c in mine if c["tournament_id"] == tid), None)
                for p in (p for p in money["payments"] if p["tournament_id"] == tid):
                    cents = available[p["id"]]
                    if c and cents > 0:
                        take = min(cents, open_cents(c))
                        if take > 0:
                            c["night"] += take
                            pay_alloc[p["id"]].append((tid, take, "night"))
                            cents -= take
                    cents = pay_older(cents, tid, "late", pay_alloc[p["id"]])
                    if cents > 0:
                        credits.append([cents, ("payment", p["id"])])
                        pay_alloc[p["id"]].append((None, cents, "credit"))
                if c:
                    opened.append(c)
                    for cr in credits:
                        take = min(cr[0], open_cents(c))
                        if take > 0:
                            c["credit"] += take
                            cr[0] -= take
                    credits = [cr for cr in credits if cr[0] > 0]
            elif kind == 1:
                r, cents = item, item["amount_cents"]
                target = next((c for c in opened if c["tournament_id"] == r["tournament_id"]), None)
                if target is None and r["tournament_id"]:
                    target = next((c for c in mine if c["tournament_id"] == r["tournament_id"]), None)
                if target:
                    take = min(cents, open_cents(target))
                    if take > 0:
                        target["receipt"] += take
                        receipt_alloc[r["id"]].append((target["tournament_id"], take, "receipt"))
                        cents -= take
                cents = pay_older(cents, None, "receipt", receipt_alloc[r["id"]])
                if cents > 0:
                    credits.append([cents, ("receipt", r["id"])])
                    receipt_alloc[r["id"]].append((None, cents, "credit"))
            else:
                s, cents = item, item["amount_cents"]
                for cr in credits:
                    take = min(cr[0], cents)
                    cr[0] -= take
                    cents -= take
                credits = [cr for cr in credits if cr[0] > 0]
                if cents > 0:
                    issues.append({**s, "problem": f"{cents} cents more than the credit there was"})
        if credits:
            credit_left[pid] = sum(cr[0] for cr in credits)

    # the tournaments as a whole: payments with no player
    tournaments = {t["id"]: tournament_row(gap_written_off.get(t["id"], 0)) for t in tracked(conn)}
    for p in payments:
        if not p["pid"] and p["tournament_id"] in tournaments:
            t = tournaments[p["tournament_id"]]
            if p["no_player"]:
                t["anonymous"] += p["counted_cents"]
            else:
                t["to_link"] += 1
                t["to_link_cents"] += p["counted_cents"]
    for c in out_charges.values():
        c["balance"] = open_cents(c)
        t = tournaments.get(c["tournament_id"])
        if t is not None:
            t["due"] += c["due"]
            t["open"] += max(c["balance"], 0)
    for tid, t in tournaments.items():
        if t["to_link"] or t["anonymous"]:
            t["gap"] = max(t["open"] - t["anonymous"] - t["to_link_cents"] - t["written_off"], 0)
    for c in out_charges.values():
        t = tournaments.get(c["tournament_id"], {})
        if c["balance"] > 0:
            if t.get("to_link") or (t.get("anonymous") and t.get("gap")):
                c["state"] = "unmatched"
                t["unmatched"] += 1
            elif t.get("anonymous"):
                c["state"] = "covered"
            else:
                c["state"] = "owing"
        elif c["settled"].get("written_off"):
            c["state"] = "written_off"
        elif c["settled"]:
            c["state"] = "settled"
        elif c["late"] or c["receipt"]:
            c["state"] = "late"
        elif c["credit"]:
            c["state"] = "credit"
        else:
            c["state"] = "paid"
    return {"charges": out_charges, "payments": pay_alloc, "receipts": receipt_alloc, "credit": credit_left,
            "tournaments": tournaments, "issues": issues}


def owing(b: dict) -> dict:
    """{player id: [charges still owed by them, oldest first]} from books()."""
    out = {}
    for c in sorted(b["charges"].values(), key=lambda c: (c["date"], c["tournament_id"])):
        if c["state"] == "owing":
            out.setdefault(c["player_id"], []).append(c)
    return out


# --- decisions made by hand --------------------------------------------------

def canonical(conn, player_id: str) -> str:
    """The player someone is reported under (after merges); the id itself if unknown."""
    row = conn.execute("SELECT COALESCE(merged_into, id) FROM player WHERE id=?", (player_id,)).fetchone()
    return row[0] if row else player_id


def _player(conn, player_id: str) -> str:
    if not conn.execute("SELECT 1 FROM player WHERE id=?", (player_id,)).fetchone():
        raise RuleError("Unknown player")
    return canonical(conn, player_id)


def _amount(cents: int):
    if cents <= 0:
        raise RuleError("Enter an amount above $0")


def settle(conn, tournament_id: str, player_id: str, method: str, cents: int, reason: str = "",
           paid_by: str | None = None, day: str | None = None) -> int:
    """Settle (part of) what an entrant owes for a tournament by hand, up to what they still owe."""
    if method not in CHARGE_METHODS:
        raise RuleError("Unknown way to settle")
    _amount(cents)
    pid = _player(conn, player_id)
    day = day or bank.today()
    bank.check_open(conn, day)
    if method == "other_player":
        if not paid_by:
            raise RuleError("Choose who paid for them")
        paid_by = _player(conn, paid_by)
        if paid_by == pid:
            raise RuleError("Choose someone else")
    elif not reason.strip():
        raise RuleError("Say why, e.g. a prize or a sponsor's entry")
    c = books(conn)["charges"].get((tournament_id, pid))
    if c is None or c["balance"] <= 0:
        raise RuleError("They don't owe anything for this tournament")
    if cents > c["balance"]:
        raise RuleError(f"They only owe {c['balance'] / 100:.2f}")
    return conn.execute("""INSERT INTO settlement (tournament_id, player_id, method, amount_cents, paid_by, reason,
                           settled_on, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (tournament_id, pid, method, cents, paid_by, reason.strip(), day, sync.now())).lastrowid


def write_off_gap(conn, tournament_id: str, reason: str, day: str | None = None) -> int:
    """Write off what's missing from a tournament that isn't owed by anyone named (e.g. the organiser covers it)."""
    if not reason.strip():
        raise RuleError("Say why, e.g. covered from my pocket")
    day = day or bank.today()
    bank.check_open(conn, day)
    gap = books(conn)["tournaments"].get(tournament_id, {}).get("gap", 0)
    if not gap:
        raise RuleError("Nothing is missing from this tournament")
    return conn.execute("""INSERT INTO settlement (tournament_id, method, amount_cents, reason, settled_on, created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (tournament_id, "written_off", gap, reason.strip(), day, sync.now())).lastrowid


def point_payment(conn, payment_id: str, tournament_id: str, cents: int, day: str | None = None) -> int:
    """Point (part of) a linked Square payment at a tournament its player owes for, instead of the oldest."""
    _amount(cents)
    p = next((p for p in square.payment_rows(conn) if p["id"] == payment_id), None)
    if p is None or not p["pid"]:
        raise RuleError("Link the payment to a player first")
    day = day or bank.today()
    bank.check_open(conn, day)
    c = books(conn)["charges"].get((tournament_id, p["pid"]))
    if c is None or c["due"] <= 0:
        raise RuleError("They don't owe anything for that tournament")
    taken = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM settlement WHERE square_payment_id=?",
                         (payment_id,)).fetchone()[0]
    if cents > p["counted_cents"] - taken:
        raise RuleError(f"Only {(p['counted_cents'] - taken) / 100:.2f} of this payment is left to point")
    return conn.execute("""INSERT INTO settlement (tournament_id, player_id, method, amount_cents, square_payment_id,
                           settled_on, created_at) VALUES (?,?,?,?,?,?,?)""",
                        (tournament_id, p["pid"], "square", cents, payment_id, day, sync.now())).lastrowid


def use_credit(conn, player_id: str, method: str, cents: int, reason: str = "", day: str | None = None) -> int:
    """Take (part of) a player's credit off them: kept as income, or refunded to them."""
    if method not in CREDIT_METHODS:
        raise RuleError("Unknown use of credit")
    _amount(cents)
    pid = _player(conn, player_id)
    day = day or bank.today()
    bank.check_open(conn, day)
    left = books(conn)["credit"].get(pid, 0)
    if cents > left:
        raise RuleError(f"They only have {left / 100:.2f} of credit")
    return conn.execute("""INSERT INTO settlement (player_id, method, amount_cents, reason, settled_on, created_at)
                           VALUES (?,?,?,?,?,?)""", (pid, method, cents, reason.strip(), day, sync.now())).lastrowid


def delete_settlement(conn, settlement_id: int):
    s = conn.execute("SELECT * FROM settlement WHERE id=?", (settlement_id,)).fetchone()
    if s is None:
        raise RuleError("Unknown settlement")
    bank.check_open(conn, s["settled_on"])
    conn.execute("DELETE FROM settlement WHERE id=?", (settlement_id,))


def add_receipt(conn, player_id: str, method: str, cents: int, day: str, tournament_id: str | None = None,
                reference: str = "", note: str = "") -> int:
    """Record money a player paid outside Square (a bank transfer or cash)."""
    if method not in RECEIPT_METHODS:
        raise RuleError("Unknown payment method")
    _amount(cents)
    pid = _player(conn, player_id)
    if day > bank.today():
        raise RuleError("That day hasn't happened yet")
    bank.check_open(conn, day)
    return conn.execute("""INSERT INTO receipt (player_id, method, received_on, amount_cents, tournament_id, reference,
                           note, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (pid, method, day, cents, tournament_id or None, reference.strip(), note.strip(),
                         sync.now())).lastrowid


def delete_receipt(conn, receipt_id: int):
    r = conn.execute("SELECT * FROM receipt WHERE id=?", (receipt_id,)).fetchone()
    if r is None:
        raise RuleError("Unknown payment")
    if r["reconciliation_id"] is not None:
        raise RuleError(bank.locked_message(conn, r["reconciliation_id"]))
    bank.check_open(conn, r["received_on"])
    conn.execute("DELETE FROM receipt WHERE id=?", (receipt_id,))


# --- moving older decisions in (#38) -----------------------------------------

MIGRATED = "ledger_v1"


def _insert(conn, tournament_id, player_id, method, cents, reason, day) -> dict:
    conn.execute("""INSERT INTO settlement (tournament_id, player_id, method, amount_cents, reason, settled_on,
                    created_at) VALUES (?,?,?,?,?,?,?)""",
                 (tournament_id, player_id, method, cents, reason, day, sync.now()))
    return {"tournament_id": tournament_id, "player_id": player_id, "method": method, "cents": cents, "reason": reason}


def migrate(conn):
    """Once (#38): freeze the charges of tournaments already locked, and turn what was decided by hand before the
    ledger into settlements, for whatever is still open once later payments have paid what they can:

    - an entrant sorted out by hand (#29, entrant_override) is settled "sorted out", with its note;
    - a shortfall the organiser covered (#32, covered_cents) is written off "Organiser covered: <note>", up to the
      amount covered: what's missing that nobody named owes first, then what named players still owe there, by tag.

    What was done is kept for `python -m app.ledger report`. Payouts, venue payments, Square payments and deposits are
    never touched."""
    if conn.execute("SELECT 1 FROM app_state WHERE key=?", (MIGRATED,)).fetchone():
        return
    for t in tracked(conn):
        if t["locked_by"] is not None and not conn.execute(
                "SELECT 1 FROM charge WHERE tournament_id=? LIMIT 1", (t["id"],)).fetchone():
            freeze(conn, t["id"])
    done, skipped = [], []
    b = books(conn)
    overrides = {}
    for r in conn.execute("""SELECT o.*, t.start_date FROM entrant_override o JOIN tournament t ON t.id=o.tournament_id
                             ORDER BY o.created_at"""):
        overrides.setdefault(r["tournament_id"], []).append({**dict(r), "pid": canonical(conn, r["player_id"])})
    extra = {}  # what each player paid at a tournament beyond their own charge there
    for p in square.payment_rows(conn):
        if p["pid"] and not p["ignored"] and p["amount_cents"] > 0:
            extra[(p["tournament_id"], p["pid"])] = extra.get((p["tournament_id"], p["pid"]), 0) + p["counted_cents"]
    for (tid, pid), cents in list(extra.items()):
        c = b["charges"].get((tid, pid))
        extra[(tid, pid)] = cents - (c["due"] if c else 0)
    for tid, rows in overrides.items():
        # sorted out in pairs (#29): one entrant "paid by X", and X, who paid extra that night, "paid for them"
        payers = [r["pid"] for r in rows if extra.get((tid, r["pid"]), 0) > 0]
        payer = payers[0] if len(payers) == 1 else None
        spare = extra.get((tid, payer), 0) if payer else 0
        for r in rows:
            c = b["charges"].get((tid, r["pid"]))
            if c is None or c["balance"] <= 0:
                skipped.append({"tournament_id": tid, "player_id": r["pid"], "reason": r["note"],
                                "why": "nothing left to settle"})
                continue
            day = bank.local_day(r["created_at"]) if r["created_at"] else (r["start_date"] or bank.today())
            paid_for = min(c["balance"], spare) if payer and r["pid"] != payer else 0
            if paid_for:
                conn.execute("""INSERT INTO settlement (tournament_id, player_id, method, amount_cents, paid_by, reason,
                                settled_on, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                             (tid, r["pid"], "other_player", paid_for, payer, r["note"], day, sync.now()))
                done.append({"tournament_id": tid, "player_id": r["pid"], "method": "other_player", "cents": paid_for,
                             "reason": r["note"], "paid_by": payer})
                spare -= paid_for
            if c["balance"] > paid_for:
                done.append(_insert(conn, tid, r["pid"], "other", c["balance"] - paid_for, r["note"], day))
    b = books(conn)
    tags = {r[0]: r[1] for r in conn.execute("SELECT id, gamer_tag FROM player")}
    for t in conn.execute("""SELECT id, start_date, covered_cents, covered_note FROM tournament
                             WHERE covered_cents IS NOT NULL ORDER BY start_date"""):
        left, reason = t["covered_cents"], "Organiser covered" + (f": {t['covered_note']}" if t["covered_note"] else "")
        day = t["start_date"] or bank.today()
        gap = b["tournaments"].get(t["id"], {}).get("gap", 0)
        if gap and left:
            done.append(_insert(conn, t["id"], None, "written_off", min(gap, left), reason, day))
            left -= min(gap, left)
        owing = sorted((c for (tid, _), c in b["charges"].items() if tid == t["id"] and c["state"] == "owing"),
                       key=lambda c: tags.get(c["player_id"], "").lower())
        for c in owing:
            if left <= 0:
                break
            cents = min(c["balance"], left)
            done.append(_insert(conn, t["id"], c["player_id"], "written_off", cents, reason, day))
            left -= cents
        if left == t["covered_cents"]:
            skipped.append({"tournament_id": t["id"], "player_id": None, "reason": reason,
                            "why": "nothing left to write off"})
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)",
                 (MIGRATED, json.dumps({"at": sync.now(), "settled": done, "skipped": skipped})))


def report(conn) -> list[str]:
    """What the move to the ledger did (#38), and what the books say now: who owes, who has credit, and tournaments
    with money missing that nobody named owes."""
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (MIGRATED,)).fetchone()
    tags = {r[0]: r[1] for r in conn.execute("SELECT id, gamer_tag FROM player")}
    names = {r[0]: f"{r[1]} ({r[2]})" for r in conn.execute("SELECT id, name, start_date FROM tournament")}
    money = lambda cents: f"${cents / 100:,.2f}"
    who = lambda pid: tags.get(pid, pid) if pid else "(nobody named)"
    out = []
    if row:
        m = json.loads(row[0])
        out.append(f"Moved into the ledger {m['at']}: {len(m['settled'])} settled, {len(m['skipped'])} not needed.")
        out += [f"  {names.get(s['tournament_id'], s['tournament_id'])}: {who(s['player_id'])} "
                f"{CHARGE_METHODS.get(s['method'], s['method'])}"
                + (f" ({who(s['paid_by'])})" if s.get("paid_by") else "")
                + f" {money(s['cents'])} ({s['reason']})" for s in m["settled"]]
        out += [f"  {names.get(s['tournament_id'], s['tournament_id'])}: {who(s['player_id'])} not needed, "
                f"{s['why']} ({s['reason']})" for s in m["skipped"]]
    b = books(conn)
    owing_now = owing(b)
    out.append(f"Owed to us: {money(sum(c['balance'] for cs in owing_now.values() for c in cs))} "
               f"by {len(owing_now)} player(s).")
    out += [f"  {who(pid)}: " + ", ".join(f"{names.get(c['tournament_id'])} {money(c['balance'])}" for c in cs)
            for pid, cs in sorted(owing_now.items(), key=lambda x: who(x[0]).lower())]
    out.append(f"Credit: {money(sum(b['credit'].values()))} held by {len(b['credit'])} player(s).")
    out += [f"  {who(pid)}: {money(cents)}" for pid, cents in sorted(b["credit"].items(), key=lambda x: who(x[0]).lower())]
    gaps = {tid: t for tid, t in b["tournaments"].items() if t["gap"] or t["to_link"]}
    out.append(f"Tournaments needing attention: {len(gaps)}.")
    out += [f"  {names.get(tid)}: " + (f"{t['to_link']} payment(s) to link" if t["to_link"] else
                                         f"{money(t['gap'])} missing that nobody named owes") for tid, t in gaps.items()]
    return out


# --- checks ------------------------------------------------------------------

def check(conn) -> list[str]:
    """Problems with the books: a tournament whose charges don't add up, money allocated beyond what it was, or a
    decision made by hand that no longer fits. Empty when all is well."""
    b = books(conn)
    problems = []
    for c in b["charges"].values():
        paid = c["night"] + c["late"] + c["credit"] + c["receipt"] + sum(c["settled"].values())
        if c["due"] + c["covers"] != paid + c["balance"]:
            problems.append(f"{c['tournament_id']} / {c['player_id']}: due {c['due'] + c['covers']} != "
                            f"paid {paid} + open {c['balance']}")
    counted = {p["id"]: p["counted_cents"] for p in square.payment_rows(conn)}
    for pid, allocs in b["payments"].items():
        if sum(a[1] for a in allocs) > counted.get(pid, 0):
            problems.append(f"payment {pid}: allocated more than it counts")
    amounts = {r["id"]: r["amount_cents"] for r in conn.execute("SELECT id, amount_cents FROM receipt")}
    for rid, allocs in b["receipts"].items():
        if sum(a[1] for a in allocs) != amounts.get(rid, 0):
            problems.append(f"receipt {rid}: allocated {sum(a[1] for a in allocs)} of {amounts.get(rid, 0)}")
    problems += [f"settlement {i['id']}: {i['problem']}" for i in b["issues"]]
    return problems


def main(argv=None):
    """python -m app.ledger check: exit 1 if the books don't add up. python -m app.ledger report: what the move to the
    ledger did, and what's owed now."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] not in (["check"], ["report"]):
        print("usage: python -m app.ledger check|report", file=sys.stderr)
        return 2
    db.init()  # as the app does on startup: tables and upgrades
    with db.session() as conn:
        if argv[0] == "report":
            print("\n".join(report(conn)))
            return 0
        problems = check(conn)
    for p in problems:
        print(p)
    print("Books add up." if not problems else f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
