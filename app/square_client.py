"""The only code that talks to Square (REST): payments, payouts, and the orders and item library behind payments."""
import logging
import random
import time
from dataclasses import dataclass
from typing import Protocol

from urllib.parse import quote

import httpx

from . import config, square_auth
from .square_auth import BASE_URLS, MissingSquareToken, SquareConfigError  # noqa: F401 (re-exported)

SQUARE_VERSION = "2026-09-16"

# Square publishes no rate limits for the Payments API; it answers 429 when throttling and recommends exponential
# backoff with jitter (developer docs, "Handling errors"). So: pace requests, and back off on 429s and blips.
MIN_GAP = 0.25  # seconds between requests from one client
MAX_ATTEMPTS = 5  # per request: waits of about 1, 2, 4 and 8 seconds (±50%) between them
RETRY_STATUSES = {429, 500, 502, 503, 504}

log = logging.getLogger("the_pot.square")


@dataclass
class SquarePayment:
    id: str
    created_at: str  # RFC 3339, UTC
    status: str
    amount_cents: int  # amount_money less refunded_money
    source_type: str = ""
    card_brand: str = ""
    last_4: str = ""
    fingerprint: str = ""
    cardholder_name: str = ""
    note: str = ""
    receipt_url: str = ""
    location_id: str = ""
    fee_cents: int | None = None  # processing fees Square kept; None until Square adds them
    order_id: str = ""  # the order rung up for it: its line items say what it was for (#31)


@dataclass
class SquarePayout:
    """Money Square sent to the bank (status SENT, then PAID; FAILED if the bank bounced it)."""
    id: str
    status: str
    amount_cents: int
    created_at: str
    arrival_date: str = ""
    version: int = 0
    location_id: str = ""


@dataclass
class SquarePayoutEntry:
    """One line of a payout: a CHARGE (a payment), REFUND, FEE, etc. net_cents is what it added to the payout."""
    id: str
    type: str
    payment_id: str = ""
    gross_cents: int = 0
    fee_cents: int = 0
    net_cents: int = 0
    effective_at: str = ""


@dataclass
class SquareLineItem:
    """One line of an order: what was sold. catalog_object_id is the item variation (blank for a custom amount)."""
    uid: str
    name: str
    total_cents: int  # after discounts, with tax
    quantity: str = "1"
    variation_name: str = ""
    item_type: str = "ITEM"  # ITEM, CUSTOM_AMOUNT or GIFT_CARD
    catalog_object_id: str = ""
    note: str = ""


@dataclass
class SquareCatalogItem:
    """An item in the item library, with its variations and the category it reports under."""
    id: str
    name: str
    variation_ids: list
    category_id: str = ""


class SquareClient(Protocol):
    def list_payments(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayment]: ...
    def list_payouts(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayout]: ...
    def list_payout_entries(self, payout_id: str) -> list[SquarePayoutEntry]: ...
    def retrieve_order(self, order_id: str) -> list[SquareLineItem]: ...
    def list_catalog(self) -> tuple[list[SquareCatalogItem], dict]: ...


class SquareRateLimited(RuntimeError):
    pass


def environment() -> str:
    """The Square environment to call. Never guessed: sandbox and production tokens only work against their own host."""
    env = config.square_env()
    if not env:
        raise SquareConfigError("Set SQUARE_ENV to sandbox or production (it must match the Square app's credentials)")
    if env not in BASE_URLS:
        raise SquareConfigError(f"SQUARE_ENV must be sandbox or production, not {env!r}")
    return env


def parse_payment(p: dict) -> SquarePayment:
    card = (p.get("card_details") or {}).get("card") or {}
    amount = (p.get("amount_money") or {}).get("amount", 0) - (p.get("refunded_money") or {}).get("amount", 0)
    fees = p.get("processing_fee") or []  # e.g. an INITIAL fee plus an ADJUSTMENT (tax); added after the payment
    return SquarePayment(
        id=p["id"], created_at=p.get("created_at", ""), status=p.get("status", ""), amount_cents=amount,
        source_type=p.get("source_type", ""), card_brand=card.get("card_brand", ""), last_4=card.get("last_4", ""),
        fingerprint=card.get("fingerprint", ""), cardholder_name=card.get("cardholder_name", ""),
        note=p.get("note", ""), receipt_url=p.get("receipt_url", ""), location_id=p.get("location_id", ""),
        fee_cents=sum((f.get("amount_money") or {}).get("amount", 0) for f in fees) if fees else None,
        order_id=p.get("order_id", ""))


def _cents(money) -> int:
    return (money or {}).get("amount", 0)


def parse_payout(p: dict) -> SquarePayout:
    return SquarePayout(id=p["id"], status=p.get("status", ""), amount_cents=_cents(p.get("amount_money")),
                        created_at=p.get("created_at", ""), arrival_date=p.get("arrival_date", ""),
                        version=p.get("version", 0), location_id=p.get("location_id", ""))


def parse_payout_entry(e: dict) -> SquarePayoutEntry:
    # the payment an entry is about sits in its type-specific details (type_charge_details, type_refund_details, ...)
    details = next((v for k, v in e.items() if k.startswith("type_") and isinstance(v, dict)), {})
    return SquarePayoutEntry(id=e["id"], type=e.get("type", ""), payment_id=details.get("payment_id", ""),
                             gross_cents=_cents(e.get("gross_amount_money")), fee_cents=_cents(e.get("fee_amount_money")),
                             net_cents=_cents(e.get("net_amount_money")), effective_at=e.get("effective_at", ""))


def parse_line_item(li: dict) -> SquareLineItem:
    return SquareLineItem(uid=li.get("uid", ""), name=li.get("name", ""), total_cents=_cents(li.get("total_money")),
                          quantity=li.get("quantity", "1"), variation_name=li.get("variation_name", ""),
                          item_type=li.get("item_type", "ITEM"), catalog_object_id=li.get("catalog_object_id", ""),
                          note=li.get("note", ""))


def parse_catalog_item(o: dict) -> SquareCatalogItem:
    """Its category: the reporting category, else the first of its categories (category_id before 2023-12)."""
    data = o.get("item_data") or {}
    category = ((data.get("reporting_category") or {}).get("id") or
                next((c.get("id", "") for c in data.get("categories") or []), "") or data.get("category_id", ""))
    return SquareCatalogItem(id=o["id"], name=data.get("name", ""), category_id=category or "",
                             variation_ids=[v["id"] for v in data.get("variations") or [] if v.get("id")])


class HttpSquareClient:
    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=time.sleep, clock=time.monotonic):
        """transport, sleep and clock are for tests. Every request is a GET (square_auth.only_reads), made with a
        read-only OAuth access token checked with Square first (square_auth.access_token)."""
        square_auth.credentials()  # a clear error before anything is sent if Square isn't set up (or the old key is)
        self.env = environment()
        self._http = square_auth.http_client(self.env, transport)
        token = square_auth.access_token(self._http, self.env)
        self.scopes = token["scopes"]
        self._http.headers["Authorization"] = f"Bearer {token['token']}"
        self._sleep, self._clock, self._last = sleep, clock, None

    def close(self):
        self._http.close()

    def _pace(self):
        if self._last is not None:
            wait = MIN_GAP - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        try:
            if retry_after:
                return min(float(retry_after), 60.0)
        except ValueError:
            pass
        return 2 ** (attempt - 1) * random.uniform(0.5, 1.5)

    def _get(self, path: str, params: dict) -> httpx.Response:
        """GET with pacing, and retries with backoff on rate limiting, server errors and network blips."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pace()
            try:
                r = self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(f"Couldn't reach Square ({self.env}): {type(e).__name__}")
                log.warning("Square (%s) %s: %s, retry %d of %d", self.env, path, type(e).__name__, attempt,
                            MAX_ATTEMPTS - 1)
                self._sleep(self._backoff(attempt, None))
                continue
            if r.status_code not in RETRY_STATUSES:
                return r
            if attempt == MAX_ATTEMPTS:
                if r.status_code == 429:
                    raise SquareRateLimited(f"Square ({self.env}) is rate limiting requests; try again in a few minutes")
                return r
            log.warning("Square (%s) %s returned %d, retry %d of %d", self.env, path, r.status_code, attempt,
                        MAX_ATTEMPTS - 1)
            self._sleep(self._backoff(attempt, r.headers.get("retry-after")))
        raise AssertionError("unreachable")

    def _check(self, r: httpx.Response, permission: str):
        if r.status_code != 200:  # Square's error detail, never the request headers
            detail = "; ".join(e.get("detail", e.get("code", "")) for e in r.json().get("errors", [])) \
                if r.headers.get("content-type", "").startswith("application/json") else r.reason_phrase
            if r.status_code == 401:
                square_auth.forget()  # revoked or expired: the next client refreshes
            hint = {401: f" Square rejected the access token (revoked or expired?); try again, and authorise again "
                         f"if it keeps happening (see README). SQUARE_ENV must match the Square app's credentials.",
                    403: f" The read-only access doesn't include {permission}: authorise again (see README)."
                    }.get(r.status_code, "")
            raise RuntimeError(f"Square ({self.env}) returned {r.status_code}: {detail.rstrip('.')}.{hint}")

    def _list(self, path: str, key: str, params: dict, permission: str, limit: bool = True) -> list[dict]:
        """Every item of a paged list endpoint."""
        params = {**params, "limit": 100} if limit else dict(params)
        out = []
        while True:
            r = self._get(path, params)
            self._check(r, permission)
            body = r.json()
            out += body.get(key, [])
            if not body.get("cursor"):
                return out
            params["cursor"] = body["cursor"]

    @staticmethod
    def _range(begin_time: str, end_time: str, location_id: str) -> dict:
        params = {"begin_time": begin_time, "end_time": end_time, "sort_order": "ASC"}
        return {**params, "location_id": location_id} if location_id else params

    def list_payments(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayment]:
        return [parse_payment(p) for p in self._list("/v2/payments", "payments",
                                                     self._range(begin_time, end_time, location_id), "PAYMENTS_READ")]

    def list_payouts(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayout]:
        """Payouts created in the range (Square filters on when the payout was sent, not its arrival date)."""
        return [parse_payout(p) for p in self._list("/v2/payouts", "payouts",
                                                    self._range(begin_time, end_time, location_id), "PAYOUTS_READ")]

    def list_payout_entries(self, payout_id: str) -> list[SquarePayoutEntry]:
        return [parse_payout_entry(e) for e in self._list(f"/v2/payouts/{quote(payout_id, safe='')}/payout-entries", "payout_entries",
                                                          {"sort_order": "ASC"}, "PAYOUTS_READ")]

    def line_items_allowed(self) -> bool:
        """Whether the grant includes orders and the item library (authorised again since #31)."""
        return {"ORDERS_READ", "ITEMS_READ"} <= set(self.scopes)

    def retrieve_order(self, order_id: str) -> list[SquareLineItem]:
        """An order's line items (RetrieveOrder: a GET, unlike the batch endpoint)."""
        r = self._get(f"/v2/orders/{quote(order_id, safe='')}", {})
        self._check(r, "ORDERS_READ")
        return [parse_line_item(li) for li in (r.json().get("order") or {}).get("line_items") or []]

    def list_catalog(self) -> tuple[list[SquareCatalogItem], dict]:
        """The item library's items (with their variations) and category names by id."""
        objects = self._list("/v2/catalog/list", "objects", {"types": "ITEM,CATEGORY"}, "ITEMS_READ", limit=False)
        items = [parse_catalog_item(o) for o in objects if o.get("type") == "ITEM"]
        names = {o["id"]: (o.get("category_data") or {}).get("name", "") for o in objects if o.get("type") == "CATEGORY"}
        return items, names
