import pytest

from app import square_auth


@pytest.fixture(autouse=True)
def no_square_settings(monkeypatch):
    """Tests start with no Square settings from the real environment and no cached access token."""
    for name in ("SQUARE_API_KEY", "SQUARE_CLIENT_ID", "SQUARE_CLIENT_SECRET", "SQUARE_REFRESH_TOKEN", "SQUARE_ENV",
                 "SQUARE_REDIRECT_URL", "SQUARE_AUTO_PULL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    square_auth.forget()
    yield
    square_auth.forget()
