"""The only code that talks to Square: lists payments over the Payments API (REST)."""
from dataclasses import dataclass
from typing import Protocol

import httpx

from . import config

SQUARE_VERSION = "2026-09-16"
BASE_URLS = {"production": "https://connect.squareup.com", "sandbox": "https://connect.squareupsandbox.com"}


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


class SquareClient(Protocol):
    def list_payments(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayment]: ...


class SquareConfigError(RuntimeError):
    pass


class MissingSquareToken(SquareConfigError):
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
    return SquarePayment(
        id=p["id"], created_at=p.get("created_at", ""), status=p.get("status", ""), amount_cents=amount,
        source_type=p.get("source_type", ""), card_brand=card.get("card_brand", ""), last_4=card.get("last_4", ""),
        fingerprint=card.get("fingerprint", ""), cardholder_name=card.get("cardholder_name", ""),
        note=p.get("note", ""), receipt_url=p.get("receipt_url", ""), location_id=p.get("location_id", ""))


class HttpSquareClient:
    def __init__(self, transport: httpx.BaseTransport | None = None):  # transport: tests only
        token = config.square_token()
        if not token:
            raise MissingSquareToken("No Square API key configured (SQUARE_API_KEY)")
        self.env = environment()
        self._http = httpx.Client(base_url=BASE_URLS[self.env], timeout=30,
                                  headers={"Authorization": f"Bearer {token}", "Square-Version": SQUARE_VERSION},
                                  transport=transport)

    def close(self):
        self._http.close()

    def list_payments(self, begin_time: str, end_time: str, location_id: str = "") -> list[SquarePayment]:
        params = {"begin_time": begin_time, "end_time": end_time, "sort_order": "ASC", "limit": 100}
        if location_id:
            params["location_id"] = location_id
        out = []
        while True:
            r = self._http.get("/v2/payments", params=params)
            if r.status_code != 200:  # Square's error detail, never the request headers
                detail = "; ".join(e.get("detail", e.get("code", "")) for e in r.json().get("errors", [])) \
                    if r.headers.get("content-type", "").startswith("application/json") else r.reason_phrase
                hint = (f" Check that SQUARE_API_KEY is a {self.env} access token, or set SQUARE_ENV to match it."
                        if r.status_code == 401 else "")
                raise RuntimeError(f"Square ({self.env}) returned {r.status_code}: {detail.rstrip('.')}.{hint}")
            body = r.json()
            out += [parse_payment(p) for p in body.get("payments", [])]
            if not body.get("cursor"):
                return out
            params["cursor"] = body["cursor"]
