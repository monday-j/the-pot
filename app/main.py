import threading
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, mail, square
from .routes import bank, home, login, owed, records, series, setup, topay, tournaments
from .web import BASE, square_client


@asynccontextmanager
async def lifespan(app):
    db.init()
    stop = threading.Event()
    hours = config.square_auto_pull_hours()
    if hours:  # one worker only (see the Dockerfile), so a single timer thread
        threading.Thread(target=square.auto_pull_loop, args=(stop, square_client, hours),
                         daemon=True).start()
    if config.reminders_on():
        threading.Thread(target=mail.reminder_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="The Pot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Every request: refuse cross-site posts (CSRF), require the login, and add the security headers."""
    path = request.url.path
    if auth.cross_site(request.method, request.headers):
        response = PlainTextResponse("Cross-site request refused", status_code=403)
    elif auth.is_open(path):
        response = await call_next(request)
    elif not config.password():
        response = PlainTextResponse("No password set: set POT_PASSWORD (see README)", status_code=503)
    elif not auth.valid_session(request.cookies.get(auth.COOKIE), config.password()):
        if request.method in ("GET", "HEAD"):
            here = path + ("?" + request.url.query if request.url.query else "")
            response = RedirectResponse("/login?" + urlencode({"next": here}), status_code=303)
        else:
            response = PlainTextResponse("Log in first", status_code=403)
    else:
        response = await call_next(request)
    for name, value in auth.SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


if config.allowed_hosts():  # outermost: a request for any other host name (DNS rebinding) never reaches the app
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts())


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers ask for this path directly, whatever the page links."""
    return FileResponse(BASE / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """For the container healthcheck: open without a login and touches nothing."""
    return PlainTextResponse("ok")


for _router in (login.router, home.router, owed.router, topay.router, series.router, tournaments.router, bank.router, setup.router,
                records.router):
    app.include_router(_router)
