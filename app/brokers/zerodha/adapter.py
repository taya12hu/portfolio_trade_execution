"""Zerodha Kite Connect v3.

Built against https://kite.trade/docs/connect/v3/ (orders, user, portfolio, exceptions);
NOT verified against a live account. Order placement is blocked unless LIVE_TRADING_ENABLED=true.

- Login: redirect to kite.zerodha.com/connect/login; our `state` rides in `redirect_params`; the
  callback brings `request_token`, exchanged at POST /session/token with
  checksum = sha256(api_key + request_token + api_secret). Tokens expire ~06:00 IST next day; no refresh.
- Tag: alphanumeric, max 20 chars (ours: 18). Market orders carry market_protection=-1 (auto).
- Errors: {"status": "error", "error_type": "...Exception", "message": ...}. TokenException/403 -> re-login.
  NetworkException(502)/DataException/GeneralException and any 5xx on a write are ambiguous.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from app.brokers.base import BrokerAdapter
from app.brokers.http import next_ist, parse_ist, parse_json, raise_for_rate_limit_or_server_error, send, to_int
from app.brokers.registry import register_broker
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerNotConfigured,
    BrokerProtocolError,
    BrokerUnavailable,
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

ROOT = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
_AMBIGUOUS_TYPES = {"NetworkException", "DataException", "GeneralException"}


def map_status(raw: str | None, filled: int) -> OrderState:
    s = (raw or "").upper()
    if s == "COMPLETE":
        return OrderState.FILLED
    if s == "REJECTED":
        return OrderState.REJECTED
    if s in ("CANCELLED", "LAPSED", "EXPIRED"):
        return OrderState.CANCELLED
    # OPEN and every interim state (PUT ORDER REQ RECEIVED, VALIDATION PENDING, OPEN PENDING, ...)
    # and anything unknown: keep watching rather than guess a terminal state.
    return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN


def to_snapshot(o: dict[str, Any]) -> OrderSnapshot:
    filled = to_int(o.get("filled_quantity"))
    side = o.get("transaction_type")
    return OrderSnapshot(
        broker_order_id=str(o["order_id"]),
        client_order_id=o.get("tag"),
        status=map_status(o.get("status"), filled),
        symbol=o.get("tradingsymbol"),
        side=Side(side) if side in ("BUY", "SELL") else None,
        quantity=to_int(o.get("quantity")) or None,
        filled_qty=filled,
        pending_qty=to_int(o.get("pending_quantity")),
        avg_price=Decimal(str(o["average_price"])) if o.get("average_price") else None,
        status_message=o.get("status_message"),
        raw_status=o.get("status"),
        placed_at=parse_ist(o.get("order_timestamp"), "%Y-%m-%d %H:%M:%S"),
    )


@register_broker("zerodha")
class ZerodhaAdapter(BrokerAdapter):
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.REDIRECT,
        supports_refresh=False,
        supports_order_tag=True,
        max_tag_len=20,
        # Documented: 10 orders/s and 10 req/s for other endpoints; stay below both.
        rate_limits=RateLimits(orders_per_sec=8, reads_per_sec=8),
    )

    def _app(self) -> tuple[str, str]:
        s = self.ctx.settings
        if not s.zerodha_api_key or not s.zerodha_api_secret:
            raise BrokerNotConfigured("ZERODHA_API_KEY / ZERODHA_API_SECRET are not set")
        return s.zerodha_api_key, s.zerodha_api_secret

    # ---- transport ----
    async def _call(self, method: str, path: str, *, write: bool, auth: bool = True, **kwargs: Any) -> dict[str, Any]:
        api_key, _ = self._app()
        headers = {"X-Kite-Version": "3"}
        if auth:
            headers["Authorization"] = f"token {api_key}:{self._require_session().access_token}"
        resp = await send(self.ctx.http, method, ROOT + path, write=write, headers=headers, **kwargs)
        if resp.status_code == 403:
            raise ReauthRequired("Kite session expired or invalid")
        raise_for_rate_limit_or_server_error(resp, write=write)
        body = parse_json(resp, write=write)
        if not isinstance(body, dict):
            raise AmbiguousSubmission("unexpected response shape") if write else BrokerProtocolError("unexpected response shape")
        if resp.status_code < 400 and body.get("status") == "success":
            return body
        error_type, message = body.get("error_type", ""), body.get("message") or "Kite error"
        if error_type == "TokenException":
            raise ReauthRequired(message, broker_code=error_type)
        if error_type in _AMBIGUOUS_TYPES or resp.status_code < 400:
            if write:
                raise AmbiguousSubmission(message, broker_code=error_type)
            raise BrokerUnavailable(message, broker_code=error_type)
        if write:  # InputException / OrderException / MarginException / HoldingException / UserException
            raise OrderRejected(message, broker_code=error_type)
        raise BrokerProtocolError(message, broker_code=error_type)

    # ---- auth ----
    def login_url(self, state: str) -> str:
        api_key, _ = self._app()
        return f"{LOGIN_URL}?{urlencode({'v': '3', 'api_key': api_key})}&redirect_params={quote('state=' + state, safe='')}"

    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        api_key, api_secret = self._app()
        if params.get("status") not in (None, "success"):
            raise InvalidCredentials(f"Kite login was not completed (status={params.get('status')})")
        request_token = params.get("request_token")
        if not request_token:
            raise InvalidConnectionParams("request_token missing from the Kite redirect")
        checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
        try:
            body = await self._call(
                "POST", "/session/token", write=False, auth=False,
                data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
            )
        except ReauthRequired as exc:  # a bad/used request_token comes back as TokenException
            raise InvalidCredentials(exc.message) from exc
        data = body.get("data") or {}
        if not data.get("access_token") or not data.get("user_id"):
            raise BrokerProtocolError("session response missing access_token/user_id")
        return BrokerSession(
            access_token=data["access_token"],
            broker_user_id=data["user_id"],
            refresh_token=data.get("refresh_token") or None,
            expires_at=next_ist(6, 0),
        )

    async def validate_session(self) -> None:
        await self._call("GET", "/user/profile", write=False)

    # ---- reads ----
    async def get_holdings(self) -> list[Holding]:
        body = await self._call("GET", "/portfolio/holdings", write=False)
        out = []
        for h in body.get("data") or []:
            if h.get("exchange") not in (None, "NSE", "BSE"):
                continue
            # settled + T1, minus what has already been sold today
            sellable = to_int(h.get("quantity")) + to_int(h.get("t1_quantity")) - to_int(h.get("used_quantity"))
            if sellable > 0:
                avg = h.get("average_price")
                out.append(Holding(h["tradingsymbol"], sellable, Decimal(str(avg)) if avg else None))
        return out

    async def list_orders(self) -> list[OrderSnapshot]:
        body = await self._call("GET", "/orders", write=False)
        return [to_snapshot(o) for o in body.get("data") or []]

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        body = await self._call("GET", f"/orders/{broker_order_id}", write=False)
        history = body.get("data") or []
        if not history:
            raise BrokerProtocolError(f"no history for order {broker_order_id}")
        return to_snapshot(history[-1])  # history is chronological; last entry is current

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        if exchange != "NSE" or not _SYMBOL_RE.match(symbol):
            raise InvalidInstrument(f"unsupported instrument {exchange}:{symbol}")
        return BrokerInstrument(symbol=symbol, exchange=exchange, broker_symbol=symbol)

    # ---- writes ----
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        form = {
            "tradingsymbol": self.resolve_symbol(req.symbol, req.exchange).broker_symbol,
            "exchange": req.exchange,
            "transaction_type": req.side.value,
            "order_type": "MARKET",
            "quantity": str(req.quantity),
            "product": "CNC",
            "validity": "DAY",
            "market_protection": "-1",
            "tag": req.client_order_id,
        }
        body = await self._call("POST", "/orders/regular", write=True, data=form)
        order_id = (body.get("data") or {}).get("order_id")
        if not order_id:
            raise AmbiguousSubmission("success response without an order_id")
        return PlaceOrderAck(broker_order_id=str(order_id))
