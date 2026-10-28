"""Import tournaments from parry.gg and (re)compute payouts from the stored rules."""
import threading
from datetime import datetime, timezone
from decimal import Decimal

from . import db
from .parry_client import ParryClient, matches_pattern
from .payouts import Rule, VenueCut, compute_payouts, pool_cents, splits_from_json, venue_breakdown


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- rules -----------------------------------------------------------------

def rule_from_row(row) -> Rule:
    return Rule(
        bracket_fee=row["bracket_fee_cents"], payout_places=row["payout_places"],
        fixed_pool=row["fixed_pool_cents"], bracket_fee_pool_pct=Decimal(row["bracket_fee_pool_pct"]),
        entry_fee_share_pct=Decimal(row["entry_fee_share_pct"]), splits=splits_from_json(row["splits"]))


def resolve_rule(conn, series_id: int, event_id: str, game_name: str):
    """Event override beats the series+game rule. Returns a Rule or None."""
    row = conn.execute("SELECT * FROM rule WHERE series_id=? AND event_id=?", (series_id, event_id)).fetchone()
    row = row or conn.execute("SELECT * FROM rule WHERE series_id=? AND game_name=? AND event_id IS NULL",
                              (series_id, game_name)).fetchone()
    return rule_from_row(row) if row else None


def event_pool(conn, event_id: str):
    """Returns (rule, pool_cents, entrants, attendees, entry_fee) or None when the event has no rule."""
    ev = conn.execute("""SELECT e.*, t.series_id, t.excluded, t.num_attendees, t.entry_fee_cents AS t_fee,
                                s.entry_fee_cents AS s_fee
                         FROM event e JOIN tournament t ON t.id=e.tournament_id
                         LEFT JOIN series s ON s.id=t.series_id WHERE e.id=?""", (event_id,)).fetchone()
    if not ev or ev["series_id"] is None or ev["excluded"]:
        return None
    rule = resolve_rule(conn, ev["series_id"], event_id, ev["game_name"])
    if rule is None:
        return None
    entrants = conn.execute("SELECT COUNT(*) FROM event_entry WHERE event_id=?", (event_id,)).fetchone()[0] \
        or ev["entrant_count"]
    attendees = conn.execute("SELECT COUNT(*) FROM tournament_entry WHERE tournament_id=?",
                             (ev["tournament_id"],)).fetchone()[0] or ev["num_attendees"]
    fee = ev["t_fee"] if ev["t_fee"] is not None else (ev["s_fee"] or 0)
    return rule, pool_cents(rule, entrants, attendees, fee), entrants, attendees, fee


_ISSUES_SQL = """SELECT e.id, e.name, e.completed, t.id AS tournament_id, t.name AS tournament_name, t.series_id,
                        (SELECT COUNT(*) FROM event_entry WHERE event_id=e.id AND placement IS NOT NULL) AS placed,
                        (SELECT COUNT(*) FROM event_entry WHERE event_id=e.id AND placement=1) AS firsts
                 FROM event e JOIN tournament t ON t.id=e.tournament_id WHERE t.excluded=0"""


def placement_issue(completed: bool, placed: int, firsts: int) -> str | None:
    """Why an event's placements can't be trusted for payouts yet, or None when they look final."""
    if not completed:
        return "Not marked complete on parry.gg"
    if placed == 0:
        return "No placements recorded"
    if firsts == 0:
        return "No 1st place"
    if firsts > 1:
        return "Tied for 1st place"
    return None


def event_issues(conn, series_id: int | None = None, tournament_id: str | None = None,
                 event_id: str | None = None) -> list[dict]:
    """Events (of non-excluded tournaments) whose placements aren't finalised, each with a `reason`."""
    sql, args = _ISSUES_SQL, []
    for column, value in (("t.series_id", series_id), ("t.id", tournament_id), ("e.id", event_id)):
        if value is not None:
            sql += f" AND {column}=?"
            args.append(value)
    out = []
    for r in conn.execute(sql + " ORDER BY t.start_date DESC, e.name", args):
        reason = placement_issue(bool(r["completed"]), r["placed"], r["firsts"])
        if reason:
            out.append({**dict(r), "reason": reason})
    return out


def recompute_event(conn, event_id: str):
    """Upsert payouts for one event (none while its placements are unfinalised).
    Paid rows keep their amount; a changed result shows as a discrepancy."""
    finalised = not event_issues(conn, event_id=event_id) and conn.execute(
        "SELECT 1 FROM event WHERE id=?", (event_id,)).fetchone() is not None
    info = event_pool(conn, event_id) if finalised else None
    computed = {}
    if info:
        rule, pool, *_ = info
        placements = {r["player_id"]: r["placement"] for r in conn.execute(
            "SELECT player_id, placement FROM event_entry WHERE event_id=? AND placement IS NOT NULL", (event_id,))}
        computed, _ = compute_payouts(pool, rule.splits, placements)
        placed = placements
    else:
        placed = {}
    existing = {r["player_id"]: r for r in conn.execute("SELECT * FROM payout WHERE event_id=?", (event_id,))}
    for player, amount in computed.items():
        row = existing.get(player)
        if row is None:
            conn.execute("INSERT INTO payout (event_id, player_id, placement, amount_cents, computed_cents)"
                         " VALUES (?,?,?,?,?)", (event_id, player, placed[player], amount, amount))
        elif row["status"] == "paid":
            conn.execute("UPDATE payout SET computed_cents=?, placement=? WHERE id=?", (amount, placed[player], row["id"]))
        else:
            conn.execute("UPDATE payout SET amount_cents=?, computed_cents=?, placement=? WHERE id=?",
                         (amount, amount, placed[player], row["id"]))
    for player, row in existing.items():
        if player in computed:
            continue
        if row["status"] == "paid":
            conn.execute("UPDATE payout SET computed_cents=0 WHERE id=?", (row["id"],))
        else:
            conn.execute("DELETE FROM payout WHERE id=?", (row["id"],))


def venue_inputs(conn, tournament_id: str):
    """Everything the venue cut depends on, or None when this tournament has no venue.

    Returns a dict: venue_name, cut, attendees, entry_fee, entry_collected, bracket_collected, unruled
    (names of events with no payout rule, whose bracket fee is therefore unknown and counted as $0).
    """
    t = conn.execute("""SELECT t.*, s.venue_name AS s_name, s.venue_flat_cents AS s_flat,
                               s.venue_entry_pct AS s_entry, s.venue_bracket_pct AS s_bracket,
                               s.entry_fee_cents AS s_fee
                        FROM tournament t LEFT JOIN series s ON s.id=t.series_id WHERE t.id=?""",
                     (tournament_id,)).fetchone()
    if not t or t["series_id"] is None or t["excluded"] or t["venue_mode"] == "none":
        return None
    if t["venue_mode"] == "custom":
        cut = VenueCut(t["venue_flat_cents"], Decimal(t["venue_entry_pct"]), Decimal(t["venue_bracket_pct"]))
    else:
        cut = VenueCut(t["s_flat"], Decimal(t["s_entry"]), Decimal(t["s_bracket"]))
    attendees = conn.execute("SELECT COUNT(*) FROM tournament_entry WHERE tournament_id=?",
                             (tournament_id,)).fetchone()[0] or t["num_attendees"]
    fee = t["entry_fee_cents"] if t["entry_fee_cents"] is not None else (t["s_fee"] or 0)
    bracket, unruled = 0, []
    for e in conn.execute("SELECT * FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
        rule = resolve_rule(conn, t["series_id"], e["id"], e["game_name"])
        if rule is None:
            unruled.append(e["name"])
            continue
        entrants = conn.execute("SELECT COUNT(*) FROM event_entry WHERE event_id=?", (e["id"],)).fetchone()[0] \
            or e["entrant_count"]
        bracket += rule.bracket_fee * entrants
    return {"venue_name": t["s_name"], "cut": cut, "attendees": attendees, "entry_fee": fee,
            "entry_collected": fee * attendees, "bracket_collected": bracket, "unruled": unruled}


def venue_total(info) -> int:
    if info is None or info["cut"].is_zero:
        return 0
    return venue_breakdown(info["cut"], info["entry_collected"], info["bracket_collected"])["total"]


def recompute_venue(conn, tournament_id: str):
    """Upsert the venue payable. Like player payouts, a paid row keeps its amount and a changed
    calculation shows as a discrepancy; an unpaid row is removed when there is nothing to pay."""
    info = venue_inputs(conn, tournament_id)
    total = venue_total(info)
    row = conn.execute("SELECT * FROM venue_payment WHERE tournament_id=?", (tournament_id,)).fetchone()
    if row is None:
        if total:
            conn.execute("INSERT INTO venue_payment (tournament_id, amount_cents, computed_cents) VALUES (?,?,?)",
                         (tournament_id, total, total))
    elif row["status"] == "paid":
        conn.execute("UPDATE venue_payment SET computed_cents=? WHERE tournament_id=?", (total, tournament_id))
    elif total:
        conn.execute("UPDATE venue_payment SET amount_cents=?, computed_cents=? WHERE tournament_id=?",
                     (total, total, tournament_id))
    else:
        conn.execute("DELETE FROM venue_payment WHERE tournament_id=?", (tournament_id,))


def recompute_series(conn, series_id: int):
    for r in conn.execute("SELECT e.id FROM event e JOIN tournament t ON t.id=e.tournament_id WHERE t.series_id=?",
                          (series_id,)).fetchall():
        recompute_event(conn, r["id"])
    for t in conn.execute("SELECT id FROM tournament WHERE series_id=?", (series_id,)).fetchall():
        recompute_venue(conn, t["id"])


# --- import ----------------------------------------------------------------

def _upsert_player(conn, user_id: str, gamer_tag: str):
    conn.execute("INSERT INTO player (id, gamer_tag) VALUES (?,?) "
                 "ON CONFLICT(id) DO UPDATE SET gamer_tag=excluded.gamer_tag WHERE excluded.gamer_tag != ''",
                 (user_id, gamer_tag))


def prune_removed_events(conn, tournament_id: str, live_ids: set) -> int:
    """Drop local events parry.gg no longer lists for this tournament (e.g. one an organiser deleted).

    Safety: does nothing when parry.gg returned no events at all (likelier a bad response than a tournament
    with every event removed), and keeps any event that still has a payout marked paid.
    """
    if not live_ids:
        return 0
    removed = 0
    for e in conn.execute("SELECT id FROM event WHERE tournament_id=?", (tournament_id,)).fetchall():
        if e["id"] in live_ids:
            continue
        if conn.execute("SELECT 1 FROM payout WHERE event_id=? AND status='paid'", (e["id"],)).fetchone():
            continue
        conn.execute("DELETE FROM event WHERE id=?", (e["id"],))  # cascades its entries, unpaid payouts and rules
        removed += 1
    return removed


def import_tournament(conn, client: ParryClient, series_id: int, tournament_id: str, manual: bool = False):
    info = client.get_tournament(tournament_id, is_id=True)
    conn.execute("""INSERT INTO tournament (id, series_id, name, slug, start_date, num_attendees, manual)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, slug=excluded.slug,
                      start_date=excluded.start_date, num_attendees=excluded.num_attendees,
                      series_id=COALESCE(tournament.series_id, excluded.series_id),
                      manual=MAX(tournament.manual, excluded.manual)""",
                 (info.id, series_id, info.name, info.slug, info.start_date, info.num_attendees, int(manual)))
    for ev in info.events:
        conn.execute("""INSERT INTO event (id, tournament_id, name, game_name, entrant_count, completed, slug)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name, game_name=excluded.game_name,
                          entrant_count=excluded.entrant_count, completed=excluded.completed, slug=excluded.slug""",
                     (ev.id, info.id, ev.name, ev.game_name, ev.entrant_count, int(ev.completed), ev.slug))
    event_ids = {e.id for e in info.events}
    prune_removed_events(conn, info.id, event_ids)
    for a in client.get_attendees(info.id):
        _upsert_player(conn, a.user_id, a.gamer_tag)
        conn.execute("INSERT OR IGNORE INTO tournament_entry VALUES (?,?)", (info.id, a.user_id))
        for eid in a.event_ids:
            if eid in event_ids:
                conn.execute("INSERT OR IGNORE INTO event_entry (event_id, player_id) VALUES (?,?)", (eid, a.user_id))
    for ev in info.events:
        if not ev.completed:
            continue
        for p in client.get_event_placements(ev.id):
            _upsert_player(conn, p.user_id, p.gamer_tag)
            conn.execute("INSERT OR IGNORE INTO tournament_entry VALUES (?,?)", (info.id, p.user_id))
            conn.execute("INSERT INTO event_entry (event_id, player_id, placement) VALUES (?,?,?) "
                         "ON CONFLICT(event_id, player_id) DO UPDATE SET placement=excluded.placement",
                         (ev.id, p.user_id, p.placement))
    return info


def sync_series(conn, client: ParryClient, series_id: int) -> dict:
    """Idempotent: discover by custom slug (or name pattern), add manual tournaments, import each, recompute payouts."""
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    excluded = {r["id"] for r in conn.execute("SELECT id FROM tournament WHERE excluded=1")}
    if series["custom_slug"]:
        wanted = {t.id for t in client.search_tournaments(custom_slug=series["custom_slug"])}
    else:
        wanted = {t.id for t in client.search_tournaments(series["pattern"])
                  if matches_pattern(series["pattern"], t.name, t.slug)}
    wanted |= {r["id"] for r in conn.execute(
        "SELECT id FROM tournament WHERE series_id=? AND manual=1", (series_id,))}
    wanted -= excluded
    imported = 0
    for tid in sorted(wanted):
        other = conn.execute("SELECT series_id FROM tournament WHERE id=?", (tid,)).fetchone()
        if other and other["series_id"] not in (None, series_id):
            continue
        import_tournament(conn, client, series_id, tid)
        imported += 1
    recompute_series(conn, series_id)
    conn.execute("UPDATE series SET last_synced=? WHERE id=?", (now(), series_id))
    return {"tournaments": imported}


def add_manual_tournament(conn, client: ParryClient, series_id: int, text: str):
    info = client.get_tournament(text)
    conn.execute("UPDATE tournament SET excluded=0 WHERE id=?", (info.id,))
    import_tournament(conn, client, series_id, info.id, manual=True)
    recompute_series(conn, series_id)
    return info


# --- background runner -----------------------------------------------------

_lock = threading.Lock()
status: dict = {}  # series_id -> {"state": "running"|"done"|"error", "message": str}


def start_sync(series_id: int, client_factory) -> bool:
    with _lock:
        if status.get(series_id, {}).get("state") == "running":
            return False
        status[series_id] = {"state": "running", "message": "Importing from parry.gg..."}

    def work():
        try:
            client = client_factory()
            try:
                with db.session() as conn:
                    result = sync_series(conn, client, series_id)
            finally:
                getattr(client, "close", lambda: None)()
            status[series_id] = {"state": "done", "message": f"Imported {result['tournaments']} tournaments"}
        except Exception as e:  # surfaced in the UI; never include request metadata
            status[series_id] = {"state": "error", "message": f"{type(e).__name__}: {e}"}

    threading.Thread(target=work, daemon=True).start()
    return True
