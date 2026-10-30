"""The run sheet (#41): the home page."""

from fastapi import APIRouter, Request

from .. import db, ledger, runsheet, square
from ..web import render

router = APIRouter()


@router.get("/")
def home(request: Request):
    with db.session() as conn:
        b = ledger.books(conn)
        lanes = runsheet.stages(conn, books=b)
        unmapped = sum(1 for i in square.item_meanings(conn) if i["meaning"] is None)
        return render(request, "runsheet.html", lanes=lanes, stages=runsheet.STAGES, done_shown=runsheet.DONE_SHOWN,
                      owed=runsheet.owed_to_us(conn, books=b), topay=runsheet.to_pay(conn),
                      bank=runsheet.bank_summary(conn), unmapped=unmapped)
