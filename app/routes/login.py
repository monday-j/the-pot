"""Logging in and out."""

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from .. import auth, config
from ..web import back, client_key, render, safe_next

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=safe_next(next, "/"), no_password=not config.password())


@router.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    to = safe_next(next, "/")
    who = client_key(request)
    if auth.locked_out(who):
        return PlainTextResponse("Too many failed logins: try again in 15 minutes", status_code=429)
    expected = config.password()
    if not expected:
        return back("/login", "No password set: set POT_PASSWORD (see README)")
    if not auth.password_ok(password, expected):
        auth.record_failure(who)
        return back("/login?" + urlencode({"next": to}), "Wrong password")
    auth.clear_failures(who)
    response = back(to)
    response.set_cookie(auth.COOKIE, auth.make_session(expected), max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="lax", secure=config.cookie_secure())
    return response


@router.post("/logout")
def logout():
    response = back("/login")
    response.delete_cookie(auth.COOKIE, httponly=True, samesite="lax", secure=config.cookie_secure())
    return response
