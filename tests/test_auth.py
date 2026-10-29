import pytest

from app import auth, config
from tests.conftest import PASSWORD
from tests.test_web import client  # noqa: F401 (fixture: logged in)


@pytest.fixture
def anon(client):  # noqa: F811
    client.cookies.clear()
    return client


def test_pages_need_a_login(anon):
    r = anon.get("/players?q=a", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fplayers%3Fq%3Da"
    assert anon.post("/series", data={"name": "X", "pattern": "x"}).status_code == 403
    assert anon.get("/healthz").text == "ok"
    assert anon.get("/static/style.css").status_code == 200
    assert anon.get("/favicon.ico").status_code == 200


def test_login_returns_to_the_page_asked_for(anon):
    r = anon.post("/login", data={"password": PASSWORD, "next": "/players?q=a"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/players?q=a"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    assert anon.get("/players").status_code == 200


def test_login_never_redirects_off_site(anon):
    r = anon.post("/login", data={"password": PASSWORD, "next": "//evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_wrong_password_and_lockout(anon):
    r = anon.post("/login", data={"password": "nope"})
    assert "Wrong password" in r.text and auth.COOKIE not in anon.cookies
    for _ in range(auth.MAX_FAILURES - 1):
        anon.post("/login", data={"password": "nope"})
    assert anon.post("/login", data={"password": PASSWORD}).status_code == 429


def test_logout(client):  # noqa: F811
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_tampered_expired_or_old_password_sessions_are_refused(client, monkeypatch):  # noqa: F811
    good = client.cookies[auth.COOKIE]
    expiry, mac = good.split(".")
    for bad in (f"{int(expiry) + 1}.{mac}", f"{expiry}.{'0' * len(mac)}", "", "x.y"):
        client.cookies.set(auth.COOKIE, bad)
        assert client.get("/", follow_redirects=False).status_code == 303
    assert not auth.valid_session(auth.make_session(PASSWORD, now=0), PASSWORD)
    client.cookies.set(auth.COOKIE, good)
    assert client.get("/").status_code == 200
    monkeypatch.setenv("POT_PASSWORD", "a new password")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_secret_key_is_private(client):  # noqa: F811
    assert (config.DATA_DIR / "session.key").stat().st_mode & 0o077 == 0


def test_no_password_fails_closed(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("POT_PASSWORD")
    assert client.get("/").status_code == 503
    assert "No password is set" in client.get("/login").text


def test_cross_site_posts_are_refused(client):  # noqa: F811
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}, {"Origin": "null"},
                    {"Referer": "http://evil.example/page"}):
        r = client.post("/series", data={"name": "X", "pattern": "x"}, headers=headers)
        assert r.status_code == 403, headers
    r = client.post("/series", data={"name": "X", "pattern": "x"},
                    headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_security_headers_and_no_api_docs(client):  # noqa: F811
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
