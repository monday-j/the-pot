"""History (#48): the audit log of money actions."""

from fastapi import APIRouter, Request

from .. import audit, db
from ..web import render

router = APIRouter()


@router.get("/history")
def history_page(request: Request, action: str = ""):
    with db.session() as conn:
        rows = audit.entries(conn, action=action if action in audit.ACTIONS else "")
    return render(request, "history.html", rows=rows, actions=audit.ACTIONS, f_action=action, limit=300)
