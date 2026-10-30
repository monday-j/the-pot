"""Read-only access to Square, through OAuth.

Square's personal access tokens can't be limited ("unrestricted Square API access"), so the app doesn't accept them.
Instead the account owner authorises our own Square app once, granting only READ_SCOPES (the code flow; see
`python -m app.square_oauth`). The server then holds the app's client id and secret and that grant's refresh token,
and swaps them for short-lived access tokens kept in memory only. Square only ever issues those tokens with the
granted read scopes, so nothing on the server can create, change or refund anything, and each new token's scopes
are checked with Square before use anyway. All requests are GETs apart from the two OAuth calls (only_reads).
"""
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from . import config

BASE_URLS = {"production": "https://connect.squareup.com", "sandbox": "https://connect.squareupsandbox.com"}
# All the app uses: ListPayments, ListPayouts, ListPayoutEntries; RetrieveOrder and ListCatalog for line items (#31).
READ_SCOPES = ("PAYMENTS_READ", "PAYOUTS_READ", "ORDERS_READ", "ITEMS_READ")
BASE_SCOPES = READ_SCOPES[:2]  # what grants from before #31 have; line items need authorising again
TOKEN_PATH, STATUS_PATH = "/oauth2/token", "/oauth2/token/status"
RENEW_AFTER = timedelta(days=6)  # Square: "renew OAuth access tokens every 7 days or less"; they last 30 days

_lock = threading.Lock()
_cache: dict = {}  # (env, client id, hash of refresh token) -> {"token", "scopes", "fetched", "expires"}


class SquareConfigError(RuntimeError):
    pass


class MissingSquareToken(SquareConfigError):
    pass


def only_reads(request: httpx.Request):
    """httpx request hook: the app only ever reads from Square."""
    if request.method == "GET" or (request.method == "POST" and request.url.path in (TOKEN_PATH, STATUS_PATH)):
        return
    raise SquareConfigError(f"Refusing to send {request.method} {request.url.path} to Square: this app only reads")


def credentials(need_refresh_token: bool = True) -> dict:
    """client_id, client_secret (and refresh_token) from the environment, or a clear error."""
    if config.square_legacy_key_set():
        raise SquareConfigError(
            "SQUARE_API_KEY holds a full-access Square token, which this app no longer accepts. Remove it from .env, "
            "replace it in the Square Developer Console so the old one stops working, and set up read-only access "
            "(see README)")
    creds = config.square_oauth()
    wanted = config.SQUARE_OAUTH_VARS if need_refresh_token else config.SQUARE_OAUTH_VARS[:2]
    missing = [name for name in wanted if name.removeprefix("SQUARE_").lower() not in creds]
    if missing:
        raise MissingSquareToken(f"Square isn't set up yet: missing {', '.join(missing)} (see README)")
    return creds


def check_scopes(scopes) -> list:
    """Refuse a token that can do anything but read payments, payouts, orders and the item library."""
    extra = sorted(set(scopes) - set(READ_SCOPES))
    if extra:
        raise SquareConfigError(f"Refusing a Square token that can do more than read (it has {', '.join(extra)}). "
                                f"Authorise again with only {' and '.join(READ_SCOPES)} (see README)")
    if not scopes:
        raise SquareConfigError("Square reported no permissions for this token")
    return sorted(scopes)


def _detail(r: httpx.Response) -> str:
    try:
        errors = r.json().get("errors") or []
        return "; ".join(e.get("detail") or e.get("code", "") for e in errors) or r.json().get("message", "") \
            or r.reason_phrase
    except ValueError:
        return r.reason_phrase


def token_scopes(http: httpx.Client, access_token: str) -> list:
    r = http.post(STATUS_PATH, headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        raise SquareConfigError(f"Square couldn't check the access token ({r.status_code}: {_detail(r)})")
    return r.json().get("scopes") or []


def _parse(stamp: str):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def access_token(http: httpx.Client, env: str, now: datetime | None = None) -> dict:
    """A verified read-only access token: cached in memory, renewed after RENEW_AFTER or a day before it expires."""
    creds = credentials()
    key = (env, creds["client_id"], hashlib.sha256(creds["refresh_token"].encode()).hexdigest())
    now = now or datetime.now(timezone.utc)
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached["fetched"] < RENEW_AFTER and cached["expires"] - now > timedelta(days=1):
            return cached
        for scopes in (READ_SCOPES, BASE_SCOPES):  # can only narrow what was granted: older grants lack line items
            r = http.post(TOKEN_PATH, json={"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                                            "grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
                                            "scopes": list(scopes)})
            if r.status_code == 200:
                break
        if r.status_code != 200:
            raise SquareConfigError(f"Square ({env}) refused the refresh token ({r.status_code}: {_detail(r)}). "
                                    "If access was revoked or the app's secret changed, authorise again (see README)")
        body = r.json()
        scopes = check_scopes(token_scopes(http, body["access_token"]))
        entry = {"token": body["access_token"], "scopes": scopes, "fetched": now,
                 "expires": _parse(body.get("expires_at", "")) or now + timedelta(days=30)}
        _cache[key] = entry
        return entry


def forget():
    """Drop cached tokens, e.g. after Square rejects one (revoked), so the next request refreshes."""
    with _lock:
        _cache.clear()


def authorize_url(env: str, client_id: str, state: str) -> str:
    """Where the account owner signs in to Square and approves read-only access (session=false: always sign in)."""
    query = urlencode({"client_id": client_id, "scope": " ".join(READ_SCOPES), "session": "false", "state": state})
    return f"{BASE_URLS[env]}/oauth2/authorize?{query}"


def exchange_code(http: httpx.Client, env: str, code: str, redirect_url: str) -> dict:
    """Swap the one-time authorisation code for tokens, refusing them unless they're read-only."""
    creds = credentials(need_refresh_token=False)
    r = http.post(TOKEN_PATH, json={"client_id": creds["client_id"], "client_secret": creds["client_secret"],
                                    "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_url})
    if r.status_code != 200:
        raise SquareConfigError(f"Square ({env}) refused the authorisation code ({r.status_code}: {_detail(r)}). "
                                "Codes are single-use and expire after 5 minutes: start again")
    body = r.json()
    body["scopes"] = check_scopes(token_scopes(http, body["access_token"]))
    return body


def http_client(env: str, transport: httpx.BaseTransport | None = None, **kwargs) -> httpx.Client:
    """An httpx client for Square that can only read (see only_reads)."""
    from .square_client import SQUARE_VERSION
    return httpx.Client(base_url=BASE_URLS[env], timeout=30, transport=transport,
                        headers={"Square-Version": SQUARE_VERSION, **kwargs.pop("headers", {})},
                        event_hooks={"request": [only_reads]}, **kwargs)
