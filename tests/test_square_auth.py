import json
import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import square_auth, square_oauth
from app.square_auth import SquareConfigError
from tests.test_square import oauth_env


class FakeOAuth:
    """Square's OAuth endpoints plus a payments endpoint, recording every request."""
    def __init__(self, scopes=("PAYMENTS_READ", "PAYOUTS_READ"), refresh_status=200, cant_widen=False):
        self.scopes, self.refresh_status, self.requests = list(scopes), refresh_status, []
        self.cant_widen = cant_widen  # refuse a refresh asking for scopes the grant doesn't have, like Square

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            body = json.loads(request.content)
            if body["grant_type"] == "refresh_token" and self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"errors": [{"detail": "refresh token revoked"}]})
            if self.cant_widen and body["grant_type"] == "refresh_token" and not set(body["scopes"]) <= set(self.scopes):
                return httpx.Response(400, json={"errors": [{"detail": "scopes not granted"}]})  # can't widen
            return httpx.Response(200, json={"access_token": f"EAAA-{len(self.requests)}",
                                             "expires_at": "2099-01-01T00:00:00Z", "refresh_token": "refresh-new",
                                             "merchant_id": "M1"})
        if request.url.path == "/oauth2/token/status":
            return httpx.Response(200, json={"scopes": self.scopes})
        return httpx.Response(200, json={"payments": []})

    def token_calls(self):
        return [r for r in self.requests if r.url.path == "/oauth2/token"]


def square_http(fake):
    return square_auth.http_client("sandbox", httpx.MockTransport(fake))


def test_only_get_requests_reach_square():
    fake = FakeOAuth()
    with square_http(fake) as http:
        assert http.get("/v2/payments").status_code == 200
        for method, path in (("POST", "/v2/payments"), ("POST", "/v2/refunds"), ("PUT", "/v2/payments/X"),
                             ("DELETE", "/v2/catalog/object/X"), ("POST", "/oauth2/revoke")):
            with pytest.raises(SquareConfigError, match="this app only reads"):
                http.request(method, path, json={})
        http.post("/oauth2/token", json={"grant_type": "refresh_token"})  # the two OAuth calls are allowed
        http.post("/oauth2/token/status")
    sent = [(r.method, r.url.path) for r in fake.requests]
    assert sent == [("GET", "/v2/payments"), ("POST", "/oauth2/token"), ("POST", "/oauth2/token/status")]


def test_the_old_full_access_key_is_refused(monkeypatch):
    oauth_env(monkeypatch)
    monkeypatch.setenv("SQUARE_API_KEY", "EAAA-full-access")
    with pytest.raises(SquareConfigError, match="full-access Square token, which this app no longer accepts"):
        square_auth.credentials()


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setenv("SQUARE_CLIENT_ID", "sq0idp-test")
    with pytest.raises(square_auth.MissingSquareToken, match="missing SQUARE_CLIENT_SECRET, SQUARE_REFRESH_TOKEN"):
        square_auth.credentials()


ALL_READ = ["PAYMENTS_READ", "PAYOUTS_READ", "ORDERS_READ", "ITEMS_READ"]


def test_access_token_is_refreshed_read_only_and_checked(monkeypatch):
    oauth_env(monkeypatch)
    fake = FakeOAuth(ALL_READ)
    with square_http(fake) as http:
        token = square_auth.access_token(http, "sandbox")
    assert token["scopes"] == sorted(ALL_READ)
    refresh = json.loads(fake.token_calls()[0].content)
    assert refresh == {"client_id": "sq0idp-test", "client_secret": "sq0csp-secret", "grant_type": "refresh_token",
                       "refresh_token": "refresh-1", "scopes": ALL_READ}
    status = [r for r in fake.requests if r.url.path == "/oauth2/token/status"][0]
    assert status.headers["authorization"] == f"Bearer {token['token']}"  # the new token's own scopes


def test_a_grant_from_before_line_items_still_refreshes(monkeypatch):
    oauth_env(monkeypatch)
    fake = FakeOAuth(cant_widen=True)  # payments and payouts only
    with square_http(fake) as http:
        token = square_auth.access_token(http, "sandbox")
    assert token["scopes"] == ["PAYMENTS_READ", "PAYOUTS_READ"]
    assert [json.loads(r.content)["scopes"] for r in fake.token_calls()] == [ALL_READ, ALL_READ[:2]]


@pytest.mark.parametrize("scopes", [("PAYMENTS_READ", "PAYMENTS_WRITE"), ("PAYMENTS_READ", "PAYOUTS_READ", "ORDERS_WRITE"),
                                    ("MERCHANT_PROFILE_READ", "PAYMENTS_READ"), ()])
def test_a_token_that_can_do_more_than_read_is_refused_and_not_kept(monkeypatch, scopes):
    oauth_env(monkeypatch)
    fake = FakeOAuth(scopes=scopes)
    with square_http(fake) as http:
        with pytest.raises(SquareConfigError, match="can do more than read|no permissions"):
            square_auth.access_token(http, "sandbox")
        fake.scopes = ["PAYMENTS_READ"]
        square_auth.access_token(http, "sandbox")
    assert len(fake.token_calls()) == 2  # the refused token wasn't cached


def test_tokens_are_cached_and_renewed(monkeypatch):
    oauth_env(monkeypatch)
    fake, now = FakeOAuth(), datetime(2026, 1, 1, tzinfo=timezone.utc)
    with square_http(fake) as http:
        first = square_auth.access_token(http, "sandbox", now)
        assert square_auth.access_token(http, "sandbox", now + timedelta(days=5)) is first
        renewed = square_auth.access_token(http, "sandbox", now + timedelta(days=6, seconds=1))
        assert renewed["token"] != first["token"] and len(fake.token_calls()) == 2
        monkeypatch.setenv("SQUARE_REFRESH_TOKEN", "refresh-2")  # a different grant: not served from the cache
        square_auth.access_token(http, "sandbox", now + timedelta(days=6, seconds=2))
        assert len(fake.token_calls()) == 3
        square_auth.forget()
        square_auth.access_token(http, "sandbox", now + timedelta(days=6, seconds=3))
        assert len(fake.token_calls()) == 4


def test_a_rejected_refresh_token_says_to_authorise_again(monkeypatch):
    oauth_env(monkeypatch)
    with square_http(FakeOAuth(refresh_status=401)) as http:
        with pytest.raises(SquareConfigError, match="refused the refresh token .*refresh token revoked.*authorise again"):
            square_auth.access_token(http, "sandbox")


def test_a_401_from_square_drops_the_cached_token(monkeypatch):
    from app.square_client import HttpSquareClient
    oauth_env(monkeypatch, "sandbox")
    fake = FakeOAuth()

    def handler(request):
        if request.url.path.startswith("/v2/"):
            return httpx.Response(401, json={"errors": [{"detail": "revoked"}]})
        return fake(request)

    c = HttpSquareClient(httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="returned 401: revoked"):
        c.list_payments("a", "b")
    assert square_auth._cache == {}


def test_authorize_url_asks_for_read_scopes_only():
    url = square_auth.authorize_url("production", "sq0idp-test", "STATE")
    assert url.startswith("https://connect.squareup.com/oauth2/authorize?")
    assert "scope=PAYMENTS_READ+PAYOUTS_READ+ORDERS_READ+ITEMS_READ" in url and "session=false" in url and "state=STATE" in url
    assert "WRITE" not in url


@pytest.fixture
def setup_tool(monkeypatch, tmp_path):
    oauth_env(monkeypatch, "production")
    monkeypatch.delenv("SQUARE_REFRESH_TOKEN")
    monkeypatch.setattr(square_oauth, "STATE_FILE", tmp_path / "state")
    fake = FakeOAuth()
    monkeypatch.setattr(square_auth, "http_client",
                        lambda env, transport=None, **kw: httpx.Client(base_url=square_auth.BASE_URLS[env],
                                                                       transport=httpx.MockTransport(fake),
                                                                       event_hooks={"request": [square_auth.only_reads]}))
    return fake


def test_setup_tool_url_and_exchange(setup_tool, capsys):
    square_oauth.main(["url"])
    out = capsys.readouterr().out
    state = square_oauth.STATE_FILE.read_text()
    assert stat.S_IMODE(os.stat(square_oauth.STATE_FILE).st_mode) == 0o600
    assert f"state={state}" in out and "scope=PAYMENTS_READ+PAYOUTS_READ+ORDERS_READ+ITEMS_READ" in out
    assert "https://monday-j.github.io/the-pot/square-oauth.html" in out

    with pytest.raises(SystemExit, match="state doesn't match"):
        square_oauth.main(["exchange", "sq0cgp-code", "forged-state"])
    square_oauth.main(["exchange", "sq0cgp-code", state])
    out = capsys.readouterr().out
    assert "SQUARE_REFRESH_TOKEN=refresh-new" in out and "Read-only access granted (PAYMENTS_READ, PAYOUTS_READ)" in out
    exchange = json.loads(setup_tool.token_calls()[0].content)
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "sq0cgp-code"
    assert exchange["redirect_uri"] == "https://monday-j.github.io/the-pot/square-oauth.html"
    assert not square_oauth.STATE_FILE.exists()  # single use
    with pytest.raises(SystemExit, match="state doesn't match"):
        square_oauth.main(["exchange", "sq0cgp-code", state])


def test_setup_tool_refuses_a_grant_with_write_access(setup_tool, capsys):
    setup_tool.scopes = ["PAYMENTS_READ", "PAYMENTS_WRITE"]
    square_oauth.main(["url"])
    state = square_oauth.STATE_FILE.read_text()
    capsys.readouterr()
    with pytest.raises(SystemExit, match="can do more than read"):
        square_oauth.main(["exchange", "sq0cgp-code", state])
    assert "SQUARE_REFRESH_TOKEN" not in capsys.readouterr().out  # never printed


def test_pages_refuse_the_old_key(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SQUARE_API_KEY", "EAAA-full-access")
    monkeypatch.setenv("SQUARE_ENV", "production")
    page = client.get("/bank").text
    assert "no longer accepts" in page and '<button class="sec" disabled' in page
    assert "no longer accepts" in client.post("/bank/square/pull", data={"since": "2026-01-01"}).text


from tests.test_web import client  # noqa: E402,F401,F811 (fixture)
