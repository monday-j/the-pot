"""What each Square item sold means for the takings."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import db, square
from ..payouts import RuleError
from ..web import back, render

router = APIRouter()


@router.get("/square/items")
def square_items(request: Request):
    with db.session() as conn:
        items = square.item_meanings(conn)
    return render(request, "square_items.html", items=items, meanings=square.MEANINGS)


@router.get("/square/categories", include_in_schema=False)
def square_categories():  # meanings were per category before #33
    return RedirectResponse("/square/items", status_code=303)


@router.post("/square/items")
def save_square_item(key: str = Form(...), meaning: str = Form(""), name: str = Form("")):
    try:
        with db.session() as conn:
            square.set_meaning(conn, key, meaning or None, name)
            square.lock_clear_tournaments(conn)
    except RuleError as e:
        return back("/square/items", str(e))
    return back("/square/items")
