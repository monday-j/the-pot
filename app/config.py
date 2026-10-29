import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "pot.db"
PARRY_HOST = os.environ.get("PARRY_HOST", "api.parry.gg:443")


def api_key() -> str | None:
    """Read at call time so the key is never held in module state or written anywhere."""
    key_file = os.environ.get("PARRY_API_KEY_FILE")
    if key_file:
        return Path(key_file).read_text().strip() or None
    return os.environ.get("PARRY_API_KEY", "").strip() or None


def password() -> str | None:
    """The shared login password (POT_PASSWORD or a file named by POT_PASSWORD_FILE), read at call time like
    api_key(). With none set the app refuses every page but /login and /healthz."""
    pw_file = os.environ.get("POT_PASSWORD_FILE")
    if pw_file:
        return Path(pw_file).read_text().strip() or None
    return os.environ.get("POT_PASSWORD", "").strip() or None


def allowed_hosts() -> list[str]:
    """Host names the app answers to (POT_ALLOWED_HOSTS, comma-separated); empty means any (no DNS rebinding check)."""
    return [h.strip() for h in os.environ.get("POT_ALLOWED_HOSTS", "").split(",") if h.strip()]


def cookie_secure() -> bool:
    """Mark the login cookie Secure (POT_COOKIE_SECURE=1), for when the app is served over HTTPS."""
    return os.environ.get("POT_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")


# Square (optional): payments taken at the door, to reconcile against expected entry and bracket fees.
TIMEZONE = os.environ.get("TZ", "UTC")  # local time of the tournaments, for the Square time window


# Square access is OAuth with read-only scopes only (see square_auth.py). These are read at call time, like api_key(),
# and never stored in the database or logs.
SQUARE_OAUTH_VARS = ("SQUARE_CLIENT_ID", "SQUARE_CLIENT_SECRET", "SQUARE_REFRESH_TOKEN")
DEFAULT_SQUARE_REDIRECT_URL = "https://monday-j.github.io/the-pot/square-oauth.html"


def square_oauth() -> dict:
    """The OAuth settings that are set: client_id, client_secret, refresh_token (missing ones left out)."""
    values = {name: os.environ.get(name, "").strip() for name in SQUARE_OAUTH_VARS}
    return {name.removeprefix("SQUARE_").lower(): v for name, v in values.items() if v}


def square_configured() -> bool:
    """True once anything Square-related is set, so a half-done setup is reported instead of silently ignored."""
    return bool(square_oauth()) or bool(os.environ.get("SQUARE_API_KEY", "").strip())


def square_legacy_key_set() -> bool:
    """The old full-access personal access token. It's refused while it's still in the environment."""
    return bool(os.environ.get("SQUARE_API_KEY", "").strip())


def square_redirect_url() -> str:
    return os.environ.get("SQUARE_REDIRECT_URL", "").strip() or DEFAULT_SQUARE_REDIRECT_URL


def square_auto_pull_hours() -> float:
    """How often to pull recent Square payments automatically; 0 (blank, unset or invalid) turns it off."""
    try:
        return max(float(os.environ.get("SQUARE_AUTO_PULL_HOURS", "") or 0), 0.0)
    except ValueError:
        return 0.0


def square_env() -> str:
    """"sandbox" or "production" as configured (no default: a token for one is rejected by the other);
    square_client.environment() validates it."""
    return os.environ.get("SQUARE_ENV", "").strip().lower()
