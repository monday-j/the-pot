"""Setup (#46): connections, what each Square item sold means for the takings, syncing and the weekly reminder, and
the style guide."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import config, db, mail, square, sync
from ..payouts import RuleError
from ..web import back, render, square_setup

router = APIRouter()


@router.get("/setup")
def setup_page(request: Request):
    with db.session() as conn:
        items = square.item_meanings(conn)
        series = conn.execute("SELECT id, name, code, tracked, last_synced FROM series ORDER BY name").fetchall()
        last_sent = mail._last_sent(conn)
    day, hour = config.reminder_when()
    return render(request, "setup.html", items=items, meanings=square.MEANINGS, square_setup=square_setup(),
                  parry_key=bool(config.api_key()), series=series, sync_status=sync.status, square_status=square.status,
                  auto_pull_hours=config.square_auto_pull_hours(), reminders_on=config.reminders_on(),
                  reminder_to=config.reminder_to(), reminder_day=config.DAYS[day], reminder_hour=hour,
                  last_sent=last_sent)


@router.get("/square/items", include_in_schema=False)
def square_items():  # moved to Setup (#46)
    return RedirectResponse("/setup#items", status_code=301)


@router.get("/square/categories", include_in_schema=False)
def square_categories():  # meanings were per category before #33
    return RedirectResponse("/setup#items", status_code=301)


@router.post("/square/items")
def save_square_item(key: str = Form(...), meaning: str = Form(""), name: str = Form("")):
    try:
        with db.session() as conn:
            square.set_meaning(conn, key, meaning or None, name)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back("/setup#items", str(e))
    return back("/setup#items")


@router.get("/setup/styleguide", include_in_schema=False)
def styleguide(request: Request):
    """Every component of the design (#40), with example data, for checking it."""
    return render(request, "styleguide.html")
