import pytest

from app import auth, square_auth

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def login_settings(monkeypatch):
    """Tests run with a known password and none of the real environment's login settings."""
    monkeypatch.setenv("POT_PASSWORD", PASSWORD)
    for name in ("POT_PASSWORD_FILE", "POT_ALLOWED_HOSTS", "POT_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture(autouse=True)
def no_square_settings(monkeypatch):
    """Tests start with no Square settings from the real environment and no cached access token."""
    for name in ("SQUARE_API_KEY", "SQUARE_CLIENT_ID", "SQUARE_CLIENT_SECRET", "SQUARE_REFRESH_TOKEN", "SQUARE_ENV",
                 "SQUARE_REDIRECT_URL", "SQUARE_AUTO_PULL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    square_auth.forget()
    yield
    square_auth.forget()


@pytest.fixture(autouse=True)
def no_mail_settings(monkeypatch):
    """Tests never pick up the real environment's SMTP or reminder settings."""
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_PASSWORD_FILE", "SMTP_FROM",
                 "REMINDER_TO", "REMINDER_DAY", "REMINDER_HOUR", "POT_URL"):
        monkeypatch.delenv(name, raising=False)
