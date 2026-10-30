"""What each Square payment was for: its order's line items and their categories (#31)."""
from dataclasses import replace

import httpx
import pytest

from app import square
from app.payouts import RuleError
from app.square_client import (HttpSquareClient, SquareCatalogItem, SquareLineItem, parse_catalog_item,
                               parse_line_item, parse_payment)
from tests.test_square import FakeSquare, fnb, oauth_env, oauth_reply, pay  # noqa: F401 (fixtures)
from tests.test_sync import TwoNightsClient, conn  # noqa: F401
from tests.test_web import client  # noqa: F401

ALL_READ = ("PAYMENTS_READ", "PAYOUTS_READ", "ORDERS_READ", "ITEMS_READ")


def test_parse_payment_order_line_items_and_catalog_items():
    assert parse_payment({"id": "P", "order_id": "O1", "amount_money": {"amount": 1500}}).order_id == "O1"
    li = parse_line_item({"uid": "u1", "name": "Venue fee", "quantity": "2", "variation_name": "Regular",
                          "catalog_object_id": "V1", "item_type": "ITEM", "total_money": {"amount": 1000}})
    assert (li.uid, li.name, li.quantity, li.catalog_object_id, li.total_cents) == ("u1", "Venue fee", "2", "V1", 1000)
    custom = parse_line_item({"uid": "u2", "item_type": "CUSTOM_AMOUNT", "total_money": {"amount": 300}})
    assert (custom.item_type, custom.catalog_object_id) == ("CUSTOM_AMOUNT", "")
    item = {"id": "I1", "type": "ITEM", "item_data": {"name": "Coke", "variations": [{"id": "V1"}, {"id": "V2"}],
                                                       "categories": [{"id": "C1"}, {"id": "C2"}]}}
    assert parse_catalog_item(item) == SquareCatalogItem("I1", "Coke", ["V1", "V2"], "C1")
    item["item_data"]["reporting_category"] = {"id": "C2"}  # the one Square reports under wins
    assert parse_catalog_item(item).category_id == "C2"
    assert parse_catalog_item({"id": "I2", "item_data": {"category_id": "C9"}}).category_id == "C9"  # before 2023-12


def test_orders_and_the_item_library_are_read_with_gets_only(monkeypatch):
    oauth_env(monkeypatch, "sandbox")
    sent = []

    def handler(request):
        if (reply := oauth_reply(request, ALL_READ)) is not None:
            return reply
        sent.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.startswith("/v2/orders/"):
            return httpx.Response(200, json={"order": {"id": "O 1", "line_items": [
                {"uid": "a", "name": "Venue fee", "total_money": {"amount": 1000}}]}})
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"objects": [
                {"id": "C1", "type": "CATEGORY", "category_data": {"name": "Entry"}},
                {"id": "I1", "type": "ITEM", "item_data": {"name": "Venue fee", "variations": [{"id": "V1"}],
                                                           "categories": [{"id": "C1"}]}}], "cursor": "next"})
        return httpx.Response(200, json={"objects": [{"id": "C2", "type": "CATEGORY", "category_data": {"name": "Bar"}}]})

    c = HttpSquareClient(httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.line_items_allowed()
    assert c.retrieve_order("O 1") == [SquareLineItem("a", "Venue fee", 1000)]
    items, names = c.list_catalog()
    assert items == [SquareCatalogItem("I1", "Venue fee", ["V1"], "C1")] and names == {"C1": "Entry", "C2": "Bar"}
    assert [(m, p) for m, p, _ in sent] == [("GET", "/v2/orders/O 1"), ("GET", "/v2/catalog/list"),
                                            ("GET", "/v2/catalog/list")]
    assert sent[1][2] == {"types": "ITEM,CATEGORY"} and sent[2][2]["cursor"] == "next"


class OrdersSquare(FakeSquare):
    """FakeSquare with orders and an item library, for a grant that covers them."""
    def __init__(self, payments, orders, items, categories, allowed=True):
        super().__init__(payments)
        self.orders, self.items, self.categories, self.allowed = orders, items, categories, allowed

    def line_items_allowed(self):
        return self.allowed

    def retrieve_order(self, order_id):
        self.calls.append(("order", order_id))
        return self.orders[order_id]

    def list_catalog(self):
        self.calls.append(("catalog",))
        return self.items, self.categories


def line(uid, name, cents, variation="", custom=False):
    return SquareLineItem(uid, name, cents, catalog_object_id=variation, item_type="CUSTOM_AMOUNT" if custom else "ITEM")


ITEMS = [SquareCatalogItem("I1", "Venue fee", ["V-venue"], "C-entry"),
         SquareCatalogItem("I2", "Bracket", ["V-bracket"], "C-entry"),
         SquareCatalogItem("I3", "Coke", ["V-coke"], "C-bar")]
CATEGORIES = {"C-entry": "Entry", "C-bar": "Bar"}


@pytest.fixture
def night(fnb):  # noqa: F811
    """t1, with line items: Alice paid $10 entry and a $5 Coke; Bob paid $10 entry; Cat a $10 custom amount."""
    payments = [replace(pay("P1", 1500, "fp-a"), order_id="O1"), replace(pay("P2", 1000, "fp-b"), order_id="O2"),
                replace(pay("P3", 1000, "fp-c"), order_id="O3"), pay("P4", 1000, "fp-d")]  # P4: no order
    orders = {"O1": [line("a", "Venue fee", 500, "V-venue"), line("b", "Bracket", 500, "V-bracket"),
                     line("c", "Coke", 500, "V-coke")],
              "O2": [line("a", "Venue fee", 500, "V-venue"), line("b", "Bracket", 500, "V-bracket")],
              "O3": [line("a", "", 1000, custom=True)]}
    sq = OrdersSquare(payments, orders, ITEMS, CATEGORIES)
    r = square.pull_tournament(fnb, sq, "t1")
    for pid, player in (("P1", "u1"), ("P2", "u2"), ("P3", "u3"), ("P4", "u4")):
        square.link_payment(fnb, pid, player)
    return fnb, sq, r


def test_line_items_are_fetched_once_with_the_item_library(night):
    conn, sq, r = night
    assert r["orders"] == 3 and sq.calls.count(("catalog",)) == 1
    sq.calls.clear()
    assert square.pull_tournament(conn, sq, "t1")["orders"] == 0  # already fetched: nothing new
    assert not [c for c in sq.calls if c[0] in ("order", "catalog")]


def test_without_the_grant_payments_are_pulled_as_before(fnb):  # noqa: F811
    sq = OrdersSquare([replace(pay("P1", 1500), order_id="O1")], {}, [], {}, allowed=False)
    r = square.pull_tournament(fnb, sq, "t1")
    assert r["new"] == 1 and "orders" not in r and sq.calls == [sq.calls[0]]  # only the payments query


def test_only_fee_lines_count_once_their_categories_have_a_meaning(night):
    conn, _, _ = night
    r = square.reconcile(conn, "t1")  # no meanings yet: everything counts, as before
    assert (r["taken"], r["unmapped"], r["by_meaning"]) == (4500, 3, {None: 3500})
    square.set_meaning(conn, "C-entry", "venue")
    square.set_meaning(conn, "C-bar", "other")
    square.set_meaning(conn, "(custom)", "catchup")  # Cat paying for last week
    r = square.reconcile(conn, "t1")
    assert (r["taken"], r["unmapped"]) == (4000, 0)  # the Coke is left out
    assert r["by_meaning"] == {"venue": 2000, "other": 500, "catchup": 1000}
    assert {p["id"]: p["state"] for p in r["players"]} == {"u1": "ok", "u2": "ok", "u3": "ok", "u4": "ok"}
    assert r["sold"] == 4500 and r["net"] == 4500  # card fees and banking still cover the whole payment
    with pytest.raises(RuleError):
        square.set_meaning(conn, "C-bar", "beer")


def test_a_payment_that_was_all_drinks_is_not_counted_or_asked_to_be_linked(night):
    conn, sq, _ = night
    sq.payments.append(replace(pay("P5", 500, "fp-e", "2026-01-01T09:30:00Z"), order_id="O5"))
    sq.orders["O5"] = [line("a", "Coke", 500, "V-coke")]
    square.pull_tournament(conn, sq, "t1")
    square.set_meaning(conn, "C-bar", "other")
    r = square.reconcile(conn, "t1")
    assert (r["taken"], r["unlinked"], r["count"]) == (4000, 0, 4)  # P5 is neither counted nor to link


def test_a_partly_refunded_or_split_payment_shares_its_lines_pro_rata():
    lines = [{"meaning": "venue", "total_cents": 1000}, {"meaning": "other", "total_cents": 500}]
    assert square._split(1500, lines) == {"venue": 1000, "other": 500}
    assert square._split(1000, lines) == {"venue": 667, "other": 333}  # $5 refunded, or the other $5 paid another way
    assert square._split(1000, []) == {}


def test_categories_page(client, monkeypatch):  # noqa: F811
    from app import db, main
    assert "No line items pulled yet" in client.get("/square/categories").text
    with db.session() as c:
        c.execute("INSERT INTO square_order_line (order_id, uid, name, catalog_object_id, total_cents) VALUES "
                  "('O1', 'a', 'Coke', 'V-coke', 500)")
        c.execute("INSERT INTO square_variation (id, item_name, category_id) VALUES ('V-coke', 'Coke', 'C-bar')")
        c.execute("INSERT INTO square_category (id, name) VALUES ('C-bar', 'Bar')")
    page = client.get("/square/categories").text
    assert "1 category still needs a meaning" in page and "Bar" in page and "Coke" in page
    page = client.post("/square/categories", data={"category_id": "C-bar", "meaning": "other"}).text
    assert "still need" not in page and '<option value="other" selected>' in page
    assert "Unknown meaning" in client.post("/square/categories", data={"category_id": "C-bar", "meaning": "x"}).text
