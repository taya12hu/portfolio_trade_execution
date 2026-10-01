"""AngelOne SmartAPI.

Built against https://smartapi.angelbroking.com/docs (Response structure, Error Codes, Orders,
Portfolio, RateLimit) and the official `smartapi-python` SDK; NOT verified against a live account.

- Login (credentials): POST loginByPassword {clientcode, password=PIN, totp} -> jwtToken + refreshToken.
  The PIN/TOTP are used once and never stored. Refresh: POST jwt/v1/generateTokens {refreshToken}.
- Every request carries X-PrivateKey (API key), X-UserType USER, X-SourceID WEB and client IP/MAC
  headers. Orders must originate from the static IP registered with AngelOne.
- Envelope: {"status": true|false|"false", "message", "errorcode", "data"}. NOTE: a failed `status`
  may be the *string* "false", so we compare explicitly.
- Rate limits: order APIs 9/s combined; getOrderBook and getHolding 1/s. Exceeding a limit returns
  HTTP 403 — so a 403 WITHOUT a token error code is treated as a rate limit, not an expired session.
- Tags: `ordertag` must be < 20 chars (ours: 18). Instruments need `symboltoken` (NSE exchange token).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.brokers import instruments
from app.brokers.base import BrokerAdapter
from app.brokers.http import parse_ist, parse_json, raise_for_rate_limit_or_server_error, send, to_int
from app.brokers.registry import register_broker
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerNotConfigured,
    BrokerProtocolError,
    BrokerRateLimited,
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

ROOT = "https://apiconnect.angelone.in"
_TOKEN_ERRORS = {"AG8001", "AG8002", "AG8003", "AB1010", "AB1011"}  # invalid/expired/missing token, session
_REFRESH_ERRORS = {"AB8050", "AB8051"}  # invalid / expired refresh token
_UNCLEAR_ERRORS = {"AB1004", "AB2000", "AB2001"}  # "something went wrong" / internal error
_CANCELLED = {"cancelled", "cancelled after market order"}


def _ok(body: Any) -> bool:
    return isinstance(body, dict) and body.get("status") in (True, "true", "True")


def map_status(raw: str | None, filled: int) -> OrderState:
    s = (raw or "").strip().lower()
    if s == "complete":
        return OrderState.FILLED
    if s == "rejected":
        return OrderState.REJECTED
    if s in _CANCELLED:
        return OrderState.CANCELLED
    return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN


def _plain(sym: str | None) -> str | None:
    return sym[:-3] if sym and sym.endswith("-EQ") else sym


def to_snapshot(o: dict[str, Any]) -> OrderSnapshot:
    filled = to_int(o.get("filledshares"))
    side = o.get("transactiontype")
    avg = o.get("averageprice")
    return OrderSnapshot(
        broker_order_id=str(o["orderid"]),
        client_order_id=o.get("ordertag") or None,
        status=map_status(o.get("orderstatus") or o.get("status"), filled),
        symbol=_plain(o.get("tradingsymbol")),
        side=Side(side) if side in ("BUY", "SELL") else None,
        quantity=to_int(o.get("quantity")) or None,
        filled_qty=filled,
        pending_qty=to_int(o.get("unfilledshares")),
        avg_price=Decimal(str(avg)) if avg not in (None, "", "0", 0) else None,
        status_message=o.get("text") or None,
        raw_status=o.get("orderstatus") or o.get("status"),
        placed_at=parse_ist(o.get("updatetime"), "%d-%b-%Y %H:%M:%S"),
    )


@register_broker("angelone")
class AngelOneAdapter(BrokerAdapter):
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.CREDENTIALS,
        supports_refresh=True,
        supports_order_tag=True,
        max_tag_len=19,
        rate_limits=RateLimits(orders_per_sec=8, reads_per_sec=1),
    )

    def _api_key(self) -> str:
        key = (self.session.extra.get("api_key") if self.session else None) or self.ctx.settings.angelone_api_key
        if not key:
            raise BrokerNotConfigured("ANGELONE_API_KEY is not set (or pass credentials.api_key)")
        return key

    def _headers(self, api_key: str, token: str | None) -> dict[str, str]:
        s = self.ctx.settings
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": s.angelone_client_local_ip,
            "X-ClientPublicIP": s.angelone_client_public_ip,
            "X-MACAddress": s.angelone_mac_address,
            "X-PrivateKey": api_key,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _call(
        self, method: str, path: str, *, write: bool, token: str | None = "session", api_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if token == "session":
            token = self._require_session().access_token
        resp = await send(
            self.ctx.http, method, ROOT + path, write=write,
            headers=self._headers(api_key or self._api_key(), token), **kwargs,
        )
        if resp.status_code == 401:
            raise ReauthRequired("AngelOne session invalid")
        raise_for_rate_limit_or_server_error(resp, write=write)
        try:
            body = parse_json(resp, write=write)
        except (AmbiguousSubmission, BrokerProtocolError):
            if resp.status_code == 403:  # "Access denied" page from the rate limiter
                raise BrokerRateLimited("AngelOne rate limit (403)") from None
            raise
        code = body.get("errorcode") if isinstance(body, dict) else None
        if resp.status_code == 403:
            if code in _TOKEN_ERRORS:
                raise ReauthRequired(body.get("message") or "token invalid", broker_code=code)
            raise BrokerRateLimited("AngelOne rate limit (403)")
        if _ok(body) and resp.status_code < 400:
            return body
        message = (body.get("message") if isinstance(body, dict) else None) or "AngelOne error"
        if code in _TOKEN_ERRORS:
            raise ReauthRequired(message, broker_code=code)
        if code in _UNCLEAR_ERRORS or not code:
            if write:
                raise AmbiguousSubmission(message, broker_code=code)
            raise BrokerUnavailable(message, broker_code=code)
        if code == "AB1009":
            raise InvalidInstrument(message, broker_code=code)
        if write:
            raise OrderRejected(message, broker_code=code)
        raise BrokerProtocolError(message, broker_code=code)

    # ---- auth ----
    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        client_code = str(params.get("client_code") or "").strip().upper()
        pin, totp = params.get("pin"), params.get("totp")
        if not (client_code and pin and totp):
            raise InvalidConnectionParams("client_code, pin and totp are required")
        api_key = params.get("api_key") or self.ctx.settings.angelone_api_key
        if not api_key:
            raise BrokerNotConfigured("ANGELONE_API_KEY is not set (or pass credentials.api_key)")
        try:
            body = await self._call(
                "POST", "/rest/auth/angelbroking/user/v1/loginByPassword", write=False, token=None, api_key=api_key,
                json={"clientcode": client_code, "password": str(pin), "totp": str(totp)},
            )
        except (ReauthRequired, BrokerProtocolError) as exc:
            raise InvalidCredentials(exc.message) from exc
        data = body.get("data") or {}
        if not data.get("jwtToken"):
            raise InvalidCredentials("AngelOne did not return a session token")
        return BrokerSession(
            access_token=data["jwtToken"],
            refresh_token=data.get("refreshToken"),
            broker_user_id=client_code,
            extra={"api_key": api_key},
        )

    async def refresh_session(self) -> BrokerSession:
        session = self._require_session()
        if not session.refresh_token:
            raise ReauthRequired("no refresh token")
        try:
            body = await self._call(
                "POST", "/rest/auth/angelbroking/jwt/v1/generateTokens", write=False,
                json={"refreshToken": session.refresh_token},
            )
        except BrokerProtocolError as exc:
            if exc.broker_code in _REFRESH_ERRORS:
                raise ReauthRequired(exc.message, broker_code=exc.broker_code) from exc
            raise
        data = body.get("data") or {}
        if not data.get("jwtToken"):
            raise ReauthRequired("refresh did not return a token")
        return BrokerSession(
            access_token=data["jwtToken"],
            refresh_token=data.get("refreshToken") or session.refresh_token,
            broker_user_id=session.broker_user_id,
            extra=session.extra,
        )

    async def validate_session(self) -> None:
        await self._call("GET", "/rest/secure/angelbroking/user/v1/getProfile", write=False)

    # ---- reads ----
    async def get_holdings(self) -> list[Holding]:
        body = await self._call("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding", write=False)
        out = []
        for h in body.get("data") or []:
            if h.get("exchange") not in (None, "NSE", "BSE"):
                continue
            qty = to_int(h.get("quantity")) + to_int(h.get("t1quantity"))
            sym = _plain(h.get("tradingsymbol"))
            if sym and qty > 0:
                avg = h.get("averageprice")
                out.append(Holding(sym, qty, Decimal(str(avg)) if avg else None))
        return out

    async def list_orders(self) -> list[OrderSnapshot]:
        body = await self._call("GET", "/rest/secure/angelbroking/order/v1/getOrderBook", write=False)
        return [to_snapshot(o) for o in body.get("data") or []]  # data is null when the book is empty

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        for snap in await self.list_orders():
            if snap.broker_order_id == broker_order_id:
                return snap
        raise BrokerProtocolError(f"order {broker_order_id} not found")

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        if exchange != "NSE":
            raise InvalidInstrument(f"unsupported exchange {exchange}")
        eq = instruments.lookup(symbol, live_trading=self.ctx.settings.live_trading_enabled)
        return BrokerInstrument(symbol=symbol, exchange=exchange, broker_symbol=f"{symbol}-EQ", token=eq.token)

    # ---- writes ----
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        inst = self.resolve_symbol(req.symbol, req.exchange)
        body = {
            "variety": "NORMAL",
            "tradingsymbol": inst.broker_symbol,
            "symboltoken": inst.token,
            "transactiontype": req.side.value,
            "exchange": req.exchange,
            "ordertype": "MARKET",  # AngelOne converts to a protected limit order (MPP)
            "producttype": "DELIVERY",
            "duration": "DAY",
            "price": "0",
            "quantity": str(req.quantity),
            "ordertag": req.client_order_id,
        }
        resp = await self._call("POST", "/rest/secure/angelbroking/order/v1/placeOrder", write=True, json=body)
        order_id = (resp.get("data") or {}).get("orderid")
        if not order_id:
            raise AmbiguousSubmission("success response without an orderid")
        return PlaceOrderAck(broker_order_id=str(order_id))
