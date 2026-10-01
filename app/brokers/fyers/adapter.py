"""Fyers API v3.

Built against the official `fyers-apiv3` Python SDK (endpoints, auth hash, order fields) and FYERS
community documentation for order status codes; NOT verified against a live account.

- Login: GET api-t1.fyers.in/api/v3/generate-authcode (client_id, redirect_uri, response_type=code,
  state) -> redirect with `auth_code` -> POST /validate-authcode {grant_type, appIdHash, code} with
  appIdHash = sha256("<app_id>:<secret>"). Refresh requires the user's PIN, which we never store, so
  refresh is unsupported here.
- Auth header: "<app_id>:<access_token>".
- Place: POST /orders/sync {symbol "NSE:<SYM>-EQ", qty, type 2 (market), side 1/-1, productType CNC,
  validity DAY, orderTag}. Response {"s": "ok", "id": ...}.
- Order status codes: 1 cancelled, 2 traded, 4 transit, 5 rejected, 6 pending, 7 expired.
- Assumption (unverified): the order book may echo tags with a "<n>:" prefix; we compare the part
  after the last ':'.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.brokers.base import BrokerAdapter
from app.brokers.http import next_ist, parse_ist, parse_json, raise_for_rate_limit_or_server_error, send, to_int
from app.brokers.registry import register_broker
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerNotConfigured,
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

API = "https://api-t1.fyers.in/api/v3"
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
# Token-error codes seen in FYERS community threads (unverified); HTTP 401/403 also map to re-login.
_TOKEN_CODES = {-8, -15, -16, -17}


def map_status(code: Any, filled: int) -> OrderState:
    c = to_int(code, default=-1)
    if c == 2:
        return OrderState.FILLED
    if c == 5:
        return OrderState.REJECTED
    if c in (1, 7):
        return OrderState.CANCELLED
    return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN  # 4 transit, 6 pending, unknown


def _plain_symbol(fyers_symbol: str | None) -> str | None:
    if not fyers_symbol:
        return None
    s = fyers_symbol.split(":", 1)[-1]
    return s[:-3] if s.endswith("-EQ") else s


def _plain_tag(tag: Any) -> str | None:
    return str(tag).rsplit(":", 1)[-1] if tag else None


def to_snapshot(o: dict[str, Any]) -> OrderSnapshot:
    filled = to_int(o.get("filledQty"))
    side = {1: Side.BUY, -1: Side.SELL}.get(to_int(o.get("side"), 0))
    traded = o.get("tradedPrice")
    return OrderSnapshot(
        broker_order_id=str(o["id"]),
        client_order_id=_plain_tag(o.get("orderTag")),
        status=map_status(o.get("status"), filled),
        symbol=_plain_symbol(o.get("symbol")),
        side=side,
        quantity=to_int(o.get("qty")) or None,
        filled_qty=filled,
        pending_qty=to_int(o.get("remainingQuantity")),
        avg_price=Decimal(str(traded)) if traded else None,
        status_message=o.get("message"),
        raw_status=str(o.get("status")),
        placed_at=parse_ist(o.get("orderDateTime"), "%d-%b-%Y %H:%M:%S"),
    )


@register_broker("fyers")
class FyersAdapter(BrokerAdapter):
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.REDIRECT,
        supports_refresh=False,  # needs the user's PIN
        supports_order_tag=True,
        max_tag_len=20,
        rate_limits=RateLimits(orders_per_sec=8, reads_per_sec=5),
    )

    def _app(self) -> tuple[str, str, str]:
        s = self.ctx.settings
        if not (s.fyers_app_id and s.fyers_secret_key and s.fyers_redirect_uri):
            raise BrokerNotConfigured("FYERS_APP_ID / FYERS_SECRET_KEY / FYERS_REDIRECT_URI are not set")
        return s.fyers_app_id, s.fyers_secret_key, s.fyers_redirect_uri

    async def _call(self, method: str, path: str, *, write: bool, auth: bool = True, **kwargs: Any) -> dict[str, Any]:
        app_id, _, _ = self._app()
        headers = {"version": "3", "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"{app_id}:{self._require_session().access_token}"
        resp = await send(self.ctx.http, method, API + path, write=write, headers=headers, **kwargs)
        if resp.status_code in (401, 403):
            raise ReauthRequired("Fyers token invalid or expired")
        raise_for_rate_limit_or_server_error(resp, write=write)
        body = parse_json(resp, write=write)
        if not isinstance(body, dict):
            raise AmbiguousSubmission("unexpected response shape") if write else BrokerProtocolError("unexpected response shape")
        if body.get("s") == "ok" and resp.status_code < 400:
            return body
        code, message = to_int(body.get("code"), 0), body.get("message") or "Fyers error"
        if code in _TOKEN_CODES:
            raise ReauthRequired(message, broker_code=str(code))
        if resp.status_code < 400:
            raise AmbiguousSubmission(message, broker_code=str(code)) if write else BrokerProtocolError(message)
        if write:
            raise OrderRejected(message, broker_code=str(code))
        raise BrokerProtocolError(message, broker_code=str(code))

    # ---- auth ----
    def login_url(self, state: str) -> str:
        app_id, _, redirect = self._app()
        q = urlencode({"client_id": app_id, "redirect_uri": redirect, "response_type": "code", "state": state})
        return f"{API}/generate-authcode?{q}"

    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        app_id, secret, _ = self._app()
        auth_code = params.get("auth_code") or params.get("code")
        if not auth_code:
            raise InvalidConnectionParams("auth_code missing from the Fyers redirect")
        app_hash = hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()
        try:
            body = await self._call(
                "POST", "/validate-authcode", write=False, auth=False,
                json={"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code},
            )
        except (ReauthRequired, BrokerProtocolError) as exc:
            raise InvalidCredentials(exc.message) from exc
        if not body.get("access_token"):
            raise InvalidCredentials("Fyers did not return an access token")
        session = BrokerSession(
            access_token=body["access_token"], broker_user_id="", refresh_token=body.get("refresh_token"),
            expires_at=next_ist(6, 0),
        )
        self.session = session
        profile = await self._call("GET", "/profile", write=False)
        user_id = (profile.get("data") or {}).get("fy_id")
        if not user_id:
            raise BrokerProtocolError("profile response missing fy_id")
        return BrokerSession(access_token=session.access_token, broker_user_id=user_id,
                             refresh_token=session.refresh_token, expires_at=session.expires_at)

    async def validate_session(self) -> None:
        await self._call("GET", "/profile", write=False)

    # ---- reads ----
    async def get_holdings(self) -> list[Holding]:
        body = await self._call("GET", "/holdings", write=False)
        totals: dict[str, tuple[int, Decimal | None]] = {}
        for h in body.get("holdings") or []:
            sym = _plain_symbol(h.get("symbol"))
            if not sym or not str(h.get("symbol", "")).startswith(("NSE:", "BSE:")):
                continue
            qty = to_int(h.get("remainingQuantity", h.get("quantity")))  # what's left to sell today
            cost = h.get("costPrice")
            prev_qty, prev_cost = totals.get(sym, (0, None))
            totals[sym] = (prev_qty + qty, prev_cost or (Decimal(str(cost)) if cost else None))
        return [Holding(s, q, c) for s, (q, c) in totals.items() if q > 0]

    async def list_orders(self) -> list[OrderSnapshot]:
        body = await self._call("GET", "/orders", write=False)
        return [to_snapshot(o) for o in body.get("orderBook") or []]

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        body = await self._call("GET", "/orders", write=False, params={"id": broker_order_id})
        book = body.get("orderBook") or []
        if not book:
            raise BrokerProtocolError(f"order {broker_order_id} not found")
        return to_snapshot(book[0])

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        if exchange != "NSE" or not _SYMBOL_RE.match(symbol):
            raise InvalidInstrument(f"unsupported instrument {exchange}:{symbol}")
        return BrokerInstrument(symbol=symbol, exchange=exchange, broker_symbol=f"NSE:{symbol}-EQ")

    # ---- writes ----
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        body = {
            "symbol": self.resolve_symbol(req.symbol, req.exchange).broker_symbol,
            "qty": req.quantity,
            "type": 2,  # market
            "side": 1 if req.side is Side.BUY else -1,
            "productType": "CNC",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": req.client_order_id,
        }
        resp = await self._call("POST", "/orders/sync", write=True, json=body)
        if not resp.get("id"):
            raise AmbiguousSubmission("success response without an order id")
        return PlaceOrderAck(broker_order_id=str(resp["id"]))
