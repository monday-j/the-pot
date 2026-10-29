"""The only code that talks to Square: lists payments over the Payments API (REST)."""
import logging
import random
import time
from dataclasses import dataclass
from typing import Protocol

from urllib.parse import quote

import httpx

from . import config

SQUARE_VERSION = "2026-09-16"
BASE_URLS = {"production": "https://connect.squareup.com", "sandbox": "https://connect.squareupsandbox.com"}

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


class SquareClient(Protocol):
    def list_payments(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayment]: ...
    def list_payouts(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayout]: ...
    def list_payout_entries(self, payout_id: str) -> list[SquarePayoutEntry]: ...


class SquareConfigError(RuntimeError):
    pass


class MissingSquareToken(SquareConfigError):
    pass


class SquareRateLimited(RuntimeError):
    pass


def environment() -> str:
    """The Square environment to call. Never guessed: sandbox and production tokens only work against their own host."""
    env = config.square_env()
    if not env:
        raise SquareConfigError("Set SQUARE_ENV to sandbox or production (it must match the SQUARE_API_KEY token)")
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
        fee_cents=sum((f.get("amount_money") or {}).get("amount", 0) for f in fees) if fees else None)


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


class HttpSquareClient:
    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=time.sleep, clock=time.monotonic):
        """transport, sleep and clock are for tests."""
        token = config.square_token()
        if not token:
            raise MissingSquareToken("No Square API key configured (SQUARE_API_KEY)")
        self.env = environment()
        self._http = httpx.Client(base_url=BASE_URLS[self.env], timeout=30,
                                  headers={"Authorization": f"Bearer {token}", "Square-Version": SQUARE_VERSION},
                                  transport=transport)
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

    def _list(self, path: str, key: str, params: dict, permission: str) -> list[dict]:
        """Every item of a paged list endpoint."""
        params = {**params, "limit": 100}
        out = []
        while True:
            r = self._get(path, params)
            if r.status_code != 200:  # Square's error detail, never the request headers
                detail = "; ".join(e.get("detail", e.get("code", "")) for e in r.json().get("errors", [])) \
                    if r.headers.get("content-type", "").startswith("application/json") else r.reason_phrase
                hint = {401: f" Check that SQUARE_API_KEY is a {self.env} access token, or set SQUARE_ENV to match it.",
                        403: f" The SQUARE_API_KEY token needs the {permission} permission for this."}.get(r.status_code, "")
                raise RuntimeError(f"Square ({self.env}) returned {r.status_code}: {detail.rstrip('.')}.{hint}")
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
