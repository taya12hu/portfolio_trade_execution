"""Groww Trade API.

Built against https://groww.in/trade-api/docs/python-sdk (orders, portfolio, annexures) and the
official `growwapi` SDK v1.5 (endpoints, headers, auth checksum); NOT verified against a live account.

- Login (credentials, per user): the user's Groww API key plus either a TOTP code or the key's
  secret. POST /v1/token/api/access with Authorization "Bearer <api_key>" and
  {"key_type": "totp", "totp"} or {"key_type": "approval", "checksum": sha256(secret + ts), "timestamp"}.
  Nothing but the resulting access token is stored. No refresh: re-login daily.
- Envelope: {"status": "SUCCESS", "payload": {...}} | {"status": "FAILURE", "error": {"code", "message"}}.
- Place: POST /v1/order/create with `order_reference_id` = our tag (8-20 alphanumeric, <= 2 hyphens).
- Statuses: NEW ACKED TRIGGER_PENDING APPROVED REJECTED FAILED EXECUTED DELIVERY_AWAITED CANCELLED
  CANCELLATION_REQUESTED MODIFICATION_REQUESTED COMPLETED.
"""

from __future__ import annotations

import hashlib
import re
import time
from decimal import Decimal
from typing import Any

from app.brokers.base import BrokerAdapter
from app.brokers.http import next_ist, parse_json, raise_for_rate_limit_or_server_error, send, to_int
from app.brokers.registry import register_broker
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerProtocolError,
    InvalidConnectionParams,
    InvalidCredentials,
    InvalidInstrument,
    OrderRejected,
    ReauthRequired,
)
from app.domain.models import (
    AuthFlow,
    BrokerCapabilities,
    BrokerInstrument,
    BrokerSession,
    Holding,
    OrderRequest,
    OrderSnapshot,
    OrderState,
    PlaceOrderAck,
    RateLimits,
    Side,
)

API = "https://api.groww.in/v1"
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
_FILLED = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}
_REJECTED = {"REJECTED", "FAILED"}
_PAGE_SIZE = 100
_MAX_PAGES = 20


def map_status(raw: str | None, filled: int, quantity: int | None) -> OrderState:
    s = (raw or "").upper()
    if s in _FILLED:
        if quantity and 0 < filled < quantity:
            return OrderState.PARTIALLY_FILLED
        return OrderState.FILLED
    if s in _REJECTED:
        return OrderState.REJECTED
    if s == "CANCELLED":
        return OrderState.CANCELLED
    return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN


def to_snapshot(o: dict[str, Any]) -> OrderSnapshot:
    filled = to_int(o.get("filled_quantity"))
    qty = to_int(o.get("quantity")) or None
    side = o.get("transaction_type")
    avg = o.get("average_fill_price")
    return OrderSnapshot(
        broker_order_id=str(o["groww_order_id"]),
        client_order_id=o.get("order_reference_id"),
        status=map_status(o.get("order_status"), filled, qty),
        symbol=o.get("trading_symbol"),
        side=Side(side) if side in ("BUY", "SELL") else None,
        quantity=qty,
        filled_qty=filled,
        pending_qty=to_int(o.get("remaining_quantity")) if o.get("remaining_quantity") is not None else None,
        avg_price=Decimal(str(avg)) if avg else None,
        status_message=o.get("remark"),
        raw_status=o.get("order_status"),
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-version": "1.0",
    }


@register_broker("groww")
class GrowwAdapter(BrokerAdapter):
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.CREDENTIALS,
        supports_refresh=False,
        supports_order_tag=True,
        max_tag_len=20,
        rate_limits=RateLimits(orders_per_sec=8, reads_per_sec=5),
    )

    async def _call(self, method: str, path: str, *, write: bool, token: str | None = None, **kwargs: Any) -> Any:
        token = token or self._require_session().access_token
        resp = await send(self.ctx.http, method, API + path, write=write, headers=_headers(token), **kwargs)
        if resp.status_code in (401, 403):
            raise ReauthRequired("Groww token invalid or expired")
        raise_for_rate_limit_or_server_error(resp, write=write)
        body = parse_json(resp, write=write)
        if not isinstance(body, dict):
            raise AmbiguousSubmission("unexpected response shape") if write else BrokerProtocolError("unexpected response shape")
        if body.get("status") == "FAILURE" or resp.status_code >= 400:
            err = body.get("error") or {}
            message, code = err.get("message") or f"Groww error (HTTP {resp.status_code})", err.get("code")
            if resp.status_code < 400:
                raise AmbiguousSubmission(message, broker_code=code) if write else BrokerProtocolError(message)
            if write:
                raise OrderRejected(message, broker_code=code)
            raise BrokerProtocolError(message, broker_code=code)
        return body.get("payload", body)

    # ---- auth ----
    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        api_key = params.get("api_key")
        totp, secret = params.get("totp"), params.get("api_secret")
        if not api_key or bool(totp) == bool(secret):
            raise InvalidConnectionParams("api_key and exactly one of totp / api_secret are required")
        if totp:
            data: dict[str, Any] = {"key_type": "totp", "totp": str(totp).strip()}
        else:
            ts = str(int(time.time()))
            data = {"key_type": "approval", "checksum": hashlib.sha256((secret + ts).encode()).hexdigest(),
                    "timestamp": int(ts)}
        resp = await send(self.ctx.http, "POST", f"{API}/token/api/access", write=False,
                          headers=_headers(api_key), json=data)
        raise_for_rate_limit_or_server_error(resp, write=False)
        body = parse_json(resp, write=False)
        if resp.status_code >= 400 or not isinstance(body, dict) or not body.get("token"):
            raise InvalidCredentials("Groww rejected the API key / TOTP / secret")
        token = body["token"]
        profile = await self._call("GET", "/user/detail", write=False, token=token)
        user_id = profile.get("ucc") or profile.get("vendor_user_id")
        if not user_id:
            # Fall back to a stable, non-reversible id derived from the user's API key.
            user_id = "groww-" + hashlib.sha256(str(api_key).encode()).hexdigest()[:12]
        return BrokerSession(access_token=token, broker_user_id=str(user_id), expires_at=next_ist(6, 0))

    async def validate_session(self) -> None:
        await self._call("GET", "/user/detail", write=False)

    # ---- reads ----
    async def get_holdings(self) -> list[Holding]:
        payload = await self._call("GET", "/holdings/user", write=False)
        out = []
        for h in payload.get("holdings") or []:
            qty = to_int(h.get("quantity"))  # documented as the net quantity of the holding
            if h.get("trading_symbol") and qty > 0:
                avg = h.get("average_price")
                out.append(Holding(h["trading_symbol"], qty, Decimal(str(avg)) if avg else None))
        return out

    async def list_orders(self) -> list[OrderSnapshot]:
        snaps: list[OrderSnapshot] = []
        for page in range(_MAX_PAGES):
            payload = await self._call(
                "GET", "/order/list", write=False, params={"segment": "CASH", "page": page, "page_size": _PAGE_SIZE}
            )
            rows = payload.get("order_list") or []
            snaps.extend(to_snapshot(o) for o in rows)
            if len(rows) < _PAGE_SIZE:
                break
        return snaps

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        payload = await self._call("GET", f"/order/status/{broker_order_id}", write=False, params={"segment": "CASH"})
        return to_snapshot({"groww_order_id": broker_order_id, **payload})

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        if exchange != "NSE" or not _SYMBOL_RE.match(symbol):
            raise InvalidInstrument(f"unsupported instrument {exchange}:{symbol}")
        return BrokerInstrument(symbol=symbol, exchange=exchange, broker_symbol=symbol)

    # ---- writes ----
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        body = {
            "trading_symbol": self.resolve_symbol(req.symbol, req.exchange).broker_symbol,
            "quantity": req.quantity,
            "price": 0,
            "trigger_price": None,
            "validity": "DAY",
            "exchange": req.exchange,
            "segment": "CASH",
            "product": "CNC",
            "order_type": "MARKET",
            "transaction_type": req.side.value,
            "order_reference_id": req.client_order_id,
        }
        payload = await self._call("POST", "/order/create", write=True, json=body)
        order_id = payload.get("groww_order_id")
        if not order_id:
            raise AmbiguousSubmission("success response without a groww_order_id")
        return PlaceOrderAck(broker_order_id=str(order_id))
