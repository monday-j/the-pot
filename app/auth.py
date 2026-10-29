"""The shared-password login and the checks every request goes through (see the README, "Login").

A session is a cookie holding its expiry and an HMAC of it. The HMAC key mixes a random server secret (kept in
DATA_DIR, never in the database) with a hash of the password, so changing the password logs everyone out and nothing
in the cookie is usable on its own.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from . import config

COOKIE = "pot_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_FAILURES, FAILURE_WINDOW = 10, 15 * 60  # failed logins per client before it has to wait
OPEN_PATHS = ("/login", "/healthz", "/favicon.ico")
OPEN_PREFIXES = ("/static/",)

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}

_lock = threading.Lock()
_secrets: dict = {}  # key file path -> bytes
_failures: dict = {}  # client -> [monotonic times of recent failed logins]


def _server_secret() -> bytes:
    """Random bytes in DATA_DIR/session.key, created (readable by the app user only) on first use."""
    path = config.DATA_DIR / "session.key"
    with _lock:
        if path not in _secrets:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
            _secrets[path] = path.read_bytes()
        return _secrets[path]


def _key(password: str) -> bytes:
    return hmac.new(_server_secret(), hashlib.sha256(password.encode()).digest(), hashlib.sha256).digest()


def _sign(password: str, expiry: int) -> str:
    return hmac.new(_key(password), str(expiry).encode(), hashlib.sha256).hexdigest()


def make_session(password: str, now: float | None = None) -> str:
    expiry = int((time.time() if now is None else now) + SESSION_SECONDS)
    return f"{expiry}.{_sign(password, expiry)}"


def valid_session(cookie: str | None, password: str, now: float | None = None) -> bool:
    expiry, _, mac = (cookie or "").partition(".")
    if not expiry.isdigit() or int(expiry) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(mac, _sign(password, int(expiry)))


def password_ok(given: str, password: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(password.encode()).digest())


def locked_out(client: str) -> bool:
    cutoff = time.monotonic() - FAILURE_WINDOW
    with _lock:
        recent = [t for t in _failures.get(client, []) if t > cutoff]
        _failures[client] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(client: str):
    with _lock:
        _failures.setdefault(client, []).append(time.monotonic())


def clear_failures(client: str):
    with _lock:
        _failures.pop(client, None)


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def cross_site(method: str, headers) -> bool:
    """A state-changing request sent by another site's page (CSRF). Browsers send Sec-Fetch-Site and Origin on form
    posts; requests without either (curl, tests) aren't from a browser page and pass."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if headers.get("sec-fetch-site") == "cross-site":
        return True
    source = headers.get("origin") or headers.get("referer")
    if not source:
        return False
    return source == "null" or urlsplit(source).netloc != headers.get("host", "")
