"""The run sheet (#41): where every tournament is in the workflow, and the three money totals beside it.

A tournament is in one stage, the first of these that applies:
- collect: its takings need attention (payments to link, or money missing that nobody named owes),
- waiting: it hasn't happened yet, or parry.gg hasn't finalised its placements, so payouts can't be worked out,
- pay out: a player or the venue is still owed for it,
- done: nothing left to do; locked once a bank reconciliation takes it in.
Players who still owe for a night don't hold it up: that's followed up under "owed to us".
"""
from datetime import date

from . import bank, ledger, square, sync

STAGES = {"collect": "Collect", "waiting": "Waiting on parry.gg", "payout": "Pay out", "done": "Done"}
LANE_ORDER = ("waiting", "collect", "payout", "done")
DONE_SHOWN = 6  # the done lane shows the latest few; the rest are on the series pages


def _days_since(day: str | None, today: str) -> int | None:
    return (date.fromisoformat(today) - date.fromisoformat(day)).days if day else None


def stages(conn, series_id: int | None = None, books: dict | None = None) -> dict:
    """{stage: [tournament cards]} for the non-excluded tournaments (of one series if given). Each card has the
    tournament's id, name, date, series and attendance, and what's behind its stage: takings issue, events not final,
    upcoming, to pay, owed to us (by how many players) and locked. Lanes needing work list the oldest first."""
    b = books or ledger.books(conn)
    today = bank.today()
    takings = {i["tournament_id"]: i for i in square.takings_issues(conn, series_id, books=b)}
    not_final = {}
    for i in sync.event_issues(conn, series_id=series_id):
        not_final[i["tournament_id"]] = not_final.get(i["tournament_id"], 0) + 1
    owed = {}
    for cs in ledger.owing(b).values():
        for c in cs:
            o = owed.setdefault(c["tournament_id"], {"cents": 0, "players": 0})
            o["cents"] += c["balance"]
            o["players"] += 1
    lanes = {k: [] for k in LANE_ORDER}
    for t in conn.execute(
            """SELECT t.id, t.name, t.start_date, t.num_attendees, t.locked_by, t.series_id, s.name AS series_name,
                      COALESCE((SELECT SUM(po.amount_cents) FROM payout po JOIN event e ON e.id=po.event_id
                                WHERE e.tournament_id=t.id AND po.status='owed'),0) AS players_owed,
                      COALESCE((SELECT amount_cents FROM venue_payment
                                WHERE tournament_id=t.id AND status='owed'),0) AS venue_owed
               FROM tournament t LEFT JOIN series s ON s.id=t.series_id
               WHERE t.excluded=0 AND (? IS NULL OR t.series_id=?)
               ORDER BY COALESCE(t.start_date, ''), t.name""", (series_id, series_id)):
        card = {**dict(t), "takings": takings.get(t["id"]), "not_final": not_final.get(t["id"], 0),
                "upcoming": bool(t["start_date"] and t["start_date"] > today),
                "to_pay": t["players_owed"] + t["venue_owed"], "owed": owed.get(t["id"]),
                "locked": t["locked_by"] is not None}
        if card["takings"]:
            stage = "collect"
        elif card["upcoming"] or card["not_final"]:
            stage = "waiting"
        elif card["to_pay"]:
            stage = "payout"
        else:
            stage = "done"
        card["stage"] = stage
        lanes[stage].append(card)
    lanes["done"].reverse()  # latest first
    return lanes


# How old a debt is, in days: up to a week, a month, three months, or more (#43).
AGES = ((7, "0–7 days"), (30, "8–30 days"), (90, "31–90 days"), (None, "90+ days"))


def age_bucket(days: int | None) -> int:
    for i, (upto, _) in enumerate(AGES):
        if upto is None or (days or 0) <= upto:
            return i
    return len(AGES) - 1


def owed_to_us(conn, books: dict | None = None) -> dict:
    """What players owe us: the total, how many owe, the days since the oldest unpaid night, the total in each age
    bucket (AGES), and per player (most owed first) their balance, its ages and the nights behind it."""
    b = books or ledger.books(conn)
    today = bank.today()
    names = {r[0]: r[1] for r in conn.execute("SELECT id, gamer_tag FROM player")}
    tnames = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM tournament")}
    players, ages = [], [0] * len(AGES)
    for pid, cs in ledger.owing(b).items():
        nights, by_age = [], [0] * len(AGES)
        for c in cs:
            days = _days_since(c["date"] or None, today)
            nights.append({**c, "tournament_name": tnames.get(c["tournament_id"]), "days": days,
                           "age": age_bucket(days)})
            by_age[age_bucket(days)] += c["balance"]
            ages[age_bucket(days)] += c["balance"]
        players.append({"id": pid, "name": names.get(pid, pid), "cents": sum(c["balance"] for c in cs),
                        "since": cs[0]["date"], "nights": nights, "ages": by_age})
    players.sort(key=lambda p: (-p["cents"], p["name"].lower()))
    oldest = min((p["since"] for p in players if p["since"]), default=None)
    return {"cents": sum(p["cents"] for p in players), "players": players, "oldest_days": _days_since(oldest, today),
            "ages": ages, "credit": sum(b["credit"].values())}


def to_pay(conn) -> dict:
    """Who we owe: players and venues with payouts not yet marked paid, most owed first, and the total."""
    rows = [{**dict(r), "venue": False} for r in conn.execute(
        """SELECT p.id, p.gamer_tag AS name, SUM(po.amount_cents) AS owed, COUNT(*) AS n, MIN(t.start_date) AS since
           FROM payout po JOIN player p ON p.id=po.player_id JOIN event e ON e.id=po.event_id
           JOIN tournament t ON t.id=e.tournament_id WHERE po.status='owed' GROUP BY p.id""")]
    rows += [{**dict(r), "venue": True} for r in conn.execute(  # a deleted venue's payments under one "(no venue)" row
        """SELECT vp.venue_id AS id, COALESCE(v.name, '(no venue)') AS name, SUM(vp.amount_cents) AS owed,
                  COUNT(*) AS n, MIN(t.start_date) AS since
           FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id LEFT JOIN venue v ON v.id=vp.venue_id
           WHERE vp.status='owed' GROUP BY vp.venue_id""")]
    rows.sort(key=lambda r: (-r["owed"], r["name"].lower()))
    return {"cents": sum(r["owed"] for r in rows), "recipients": rows,
            "players_cents": sum(r["owed"] for r in rows if not r["venue"]),
            "venues_cents": sum(r["owed"] for r in rows if r["venue"])}


def bank_summary(conn) -> dict:
    """The bank as the app sees it today: the estimated balance (last reconciled balance, plus money in and minus
    money out since; None before the first reconciliation), Square money not deposited yet, the days since the last
    reconciliation, and the total filed as unexplained."""
    last = bank.latest(conn)
    moved = bank.movements(conn)
    in_transit = conn.execute("""SELECT COALESCE(SUM(amount_cents - COALESCE(fee_cents, 0)), 0) FROM square_payment
                                 WHERE deposit_id IS NULL AND amount_cents > 0""").fetchone()[0]
    unexplained = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM bank_adjustment WHERE category='unexplained'").fetchone()[0]
    return {"estimate": None if last is None else last["balance_cents"] + moved["in"] - moved["out"],
            "last": last, "days": _days_since(last["reconciled_on"] if last else None, bank.today()),
            "in_transit": in_transit, "unexplained": unexplained}
