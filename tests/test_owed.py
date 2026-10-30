import time

import pytest

from app import bank, db, sync, web
from tests.test_ledger import NIGHT1
from tests.test_square import FakeSquare, TwoNightsClient, oauth_env, pay
from tests.test_web import client  # noqa: F401 (fixture)


@pytest.fixture
def night(client, monkeypatch):  # noqa: F811
    """t1, with a $5 venue fee: Alice paid hers on her card; Bob, Cat and Dan owe $5 each."""
    monkeypatch.setattr(web, "GrpcParryClient", TwoNightsClient)
    monkeypatch.setattr(bank, "today", lambda: "2026-01-20")
    client.post("/series", data={"name": "FNB", "custom_slug": "fnb"})
    client.post("/periods/1", data={"entry_fee": "5"})
    client.post("/series/1/sync")
    for _ in range(50):
        if sync.status[1]["state"] != "running":
            break
        time.sleep(0.05)
    oauth_env(monkeypatch, "sandbox")
    monkeypatch.setattr(web, "HttpSquareClient", lambda: FakeSquare([pay("P1", 500, "fp-a", NIGHT1)]))
    client.post("/tournaments/t1/square/pull")
    client.post("/square-payments/P1/link", data={"player_id": "u1"})
    return client


def who_owes(page):
    return page.split("Who owes")[1].split('id="credit"')[0]


def test_owed_to_us_lists_each_debt_by_age(night):
    page = night.get("/owed").text
    owes = who_owes(page)
    assert all(f'href="/players/{p}"' in owes for p in ("u2", "u3", "u4")) and 'href="/players/u1"' not in owes
    assert '<span class="v">$15.00</span>' in page and "oldest 19 days" in page
    assert owes.count('data-v="500">$5.00</td>') >= 3  # each in 8–30 days
    assert 'href="/owed"><span class="k">Owed to us</span><span class="v">$15.00' in night.get("/").text


def test_a_bank_transfer_pays_what_they_owe_and_counts_toward_the_bank(night):
    r = night.post("/owed/u2/receipt", data={"method": "bank", "reference": "BOB"}, follow_redirects=False)
    assert r.headers["location"] == "/owed"
    page = night.get("/owed").text
    assert 'href="/players/u2"' not in who_owes(page)
    received = page.split('id="received"')[1].split('id="late"')[0]
    assert "bank transfer" in received and "BOB" in received and 'href="/tournaments/t1"' in received
    with db.session() as conn:
        assert bank.movements(conn)["in"] == 500
    assert "That day hasn" in night.post("/owed/u3/receipt", data={"method": "cash", "received_on": "2026-02-01"}).text

    rid = received.split('action="/receipts/')[1].split("/")[0]
    night.post(f"/receipts/{rid}/delete", data={"next": "/owed#received"})
    assert 'href="/players/u2"' in who_owes(night.get("/owed").text)


def test_writing_off_what_someone_owes_needs_a_reason(night):
    assert "Say why" in night.post("/owed/u3/write-off", data={"reason": " "}).text
    night.post("/owed/u3/write-off", data={"reason": "moved away"})
    assert 'href="/players/u3"' not in who_owes(night.get("/owed").text)
    assert "They don&#39;t owe anything" in night.post("/owed/u3/write-off", data={"reason": "again"}).text


def test_credit_is_kept_unless_told_otherwise(night):
    night.post("/owed/u4/receipt", data={"method": "cash", "amount": "20"})  # $5 owed, $15 over
    page = night.get("/owed").text
    credit = page.split('id="credit"')[1].split('id="received"')[0]
    assert '<span class="v">$15.00</span>' in page and "kept for their next tournament" in credit
    night.post("/owed/u4/credit", data={"method": "refunded", "amount": "5", "reason": "gave cash back"})
    credit = night.get("/owed").text.split('id="credit"')[1].split('id="received"')[0]
    assert 'data-v="1000">$10.00' in credit and "refunded" in credit and "gave cash back" in credit
    assert "only have" in night.post("/owed/u4/credit", data={"method": "income", "amount": "50"}).text
    sid = credit.split('action="/settlements/')[1].split("/")[0]
    r = night.post(f"/settlements/{sid}/delete", data={"next": "/owed#credit"}, follow_redirects=False)
    assert r.headers["location"] == "/owed#credit"


def test_the_player_page_shows_what_they_owe_and_takes_payment(night):
    page = night.get("/players/u2").text
    assert '<span class="k">They owe us</span><span class="v">$5.00</span>' in page and "for 1 night" in page
    assert '<span class="chip owing">owing</span>' in page.split('id="nights"')[1]
    r = night.post("/owed/u2/receipt", data={"method": "cash", "next": "/players/u2#nights"}, follow_redirects=False)
    assert r.headers["location"] == "/players/u2#nights"
    page = night.get("/players/u2").text
    assert '<span class="v">$0.00</span>' in page and "by transfer or cash" in page and "Paid outside Square" in page
    assert '<span class="chip paid">paid</span>' in night.get("/players/u1").text  # Alice paid on the night
