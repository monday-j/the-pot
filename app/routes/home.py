"""The dashboard."""

from fastapi import APIRouter, Request

from .. import db, square, sync
from ..web import TOURNAMENT_SUMS, flags_by, render

router = APIRouter()


@router.get("/")
def dashboard(request: Request):
    with db.session() as conn:
        owed = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payout WHERE status='owed'").fetchone()[0]
        paid = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM payout WHERE status='paid'").fetchone()[0]
        venue_owed = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM venue_payment WHERE status='owed'").fetchone()[0]
        venue_paid = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM venue_payment WHERE status='paid'").fetchone()[0]
        issues = sync.event_issues(conn)
        by_tournament = flags_by(issues, "tournament_id")
        payment_issues = square.payment_issues(conn)
        unpaid = {i["tournament_id"]: i for i in payment_issues}
        recent = [{**dict(r), "flags": len(by_tournament.get(r["id"], [])), "unpaid": unpaid.get(r["id"])} for r in conn.execute(
            f"""SELECT t.*, s.name AS series_name, s.code AS series_code, {TOURNAMENT_SUMS} FROM tournament t
                LEFT JOIN series s ON s.id=t.series_id ORDER BY t.start_date DESC LIMIT 10""")]
        owing = [{**dict(r), "venue": False} for r in conn.execute(
            """SELECT p.id, p.gamer_tag AS name, SUM(po.amount_cents) AS owed, COUNT(*) AS n, MIN(t.start_date) AS since
               FROM payout po JOIN player p ON p.id=po.player_id JOIN event e ON e.id=po.event_id
               JOIN tournament t ON t.id=e.tournament_id WHERE po.status='owed' GROUP BY p.id""")]
        owing += [{**dict(r), "venue": True} for r in conn.execute(  # a deleted venue's payments under one "(no venue)" row
            """SELECT vp.venue_id AS id, COALESCE(v.name, '(no venue)') AS name, SUM(vp.amount_cents) AS owed,
                      COUNT(*) AS n, MIN(t.start_date) AS since
               FROM venue_payment vp JOIN tournament t ON t.id=vp.tournament_id LEFT JOIN venue v ON v.id=vp.venue_id
               WHERE vp.status='owed' GROUP BY vp.venue_id""")]
        owing.sort(key=lambda r: (-r["owed"], r["name"].lower()))
    return render(request, "dashboard.html", owed=owed, paid=paid, recent=recent, owing=owing,
                  issues=issues, payment_issues=payment_issues, venue_owed=venue_owed, venue_paid=venue_paid)
