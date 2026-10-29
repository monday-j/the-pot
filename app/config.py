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


# Square (optional): payments taken at the door, to reconcile against expected entry and bracket fees.
SQUARE_ENV = os.environ.get("SQUARE_ENV", "sandbox").strip().lower()  # "sandbox" or "production"
TIMEZONE = os.environ.get("TZ", "UTC")  # local time of the tournaments, for the Square time window


def square_token() -> str | None:
    """Read at call time, like api_key()."""
    return os.environ.get("SQUARE_API_KEY", "").strip() or None
