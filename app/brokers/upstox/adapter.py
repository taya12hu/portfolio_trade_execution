"""Upstox (API v2 reads/auth, v3 order placement).

Built against https://upstox.com/developer/api-documentation/ (authorize, get-token, v3 place-order,
get-order-book, get-holdings, order-status appendix); NOT verified against a live account.

- Login: OAuth2 code flow; `state` is returned on the redirect. Token valid until 03:30 IST next day.
- Place: POST https://api-hft.upstox.com/v3/order/place, instrument_token = "NSE_EQ|<ISIN>",
  product D (delivery), tag max 40 chars, market_protection -1 (auto), slice=false (one order).
- Errors: {"status": "error", "errors": [{"error_code"|"errorCode", "message"}]}; 401 -> re-login.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.brokers import instruments
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

API = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
_TOKEN_ERRORS = {"UDAPI100050"}  # "Invalid token used to access API" (401 also maps to re-login)

_CANCELLED = {"cancelled", "cancelled after market order"}


def map_status(raw: str | None, filled: int) -> OrderState:
    s = (raw or "").strip().lower()
    if s == "complete":
        return OrderState.FILLED
    if s == "rejected":
        return OrderState.REJECTED
    if s in _CANCELLED:
        return OrderState.CANCELLED
    return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN


def _symbol(o: dict[str, Any]) -> str | None:
    sym = o.get("trading_symbol") or o.get("tradingsymbol")
    return sym[:-3] if sym and sym.endswith("-EQ") else sym


def to_snapshot(o: dict[str, Any]) -> OrderSnapshot:
    filled = to_int(o.get("filled_quantity"))
    side = o.get("transaction_type")
    avg = o.get("average_price")
    return OrderSnapshot(
        broker_order_id=str(o["order_id"]),
        client_order_id=o.get("tag"),
        status=map_status(o.get("status"), filled),
        symbol=_symbol(o),
        side=Side(side) if side in ("BUY", "SELL") else None,
        quantity=to_int(o.get("quantity")) or None,
        filled_qty=filled,
        pending_qty=to_int(o.get("pending_quantity")),
        avg_price=Decimal(str(avg)) if avg else None,
        status_message=o.get("status_message"),
        raw_status=o.get("status"),
        placed_at=parse_ist(o.get("order_timestamp"), "%Y-%m-%d %H:%M:%S"),
    )


@register_broker("upstox")
class UpstoxAdapter(BrokerAdapter):
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.REDIRECT,
        supports_refresh=False,
        supports_order_tag=True,
        max_tag_len=40,
        # Documented: 10 orders/s (unregistered algos), 50 req/s standard APIs.
        rate_limits=RateLimits(orders_per_sec=8, reads_per_sec=10),
    )

    def _app(self) -> tuple[str, str, str]:
        s = self.ctx.settings
        if not (s.upstox_api_key and s.upstox_api_secret and s.upstox_redirect_uri):
            raise BrokerNotConfigured("UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI are not set")
        return s.upstox_api_key, s.upstox_api_secret, s.upstox_redirect_uri

    async def _call(self, method: str, url: str, *, write: bool, auth: bool = True, **kwargs: Any) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._require_session().access_token}"
        resp = await send(self.ctx.http, method, url, write=write, headers=headers, **kwargs)
        if resp.status_code == 401:
            raise ReauthRequired("Upstox token invalid or expired")
        raise_for_rate_limit_or_server_error(resp, write=write)
        body = parse_json(resp, write=write)
        if not isinstance(body, dict):
            raise AmbiguousSubmission("unexpected response shape") if write else BrokerProtocolError("unexpected response shape")
        if resp.status_code < 400 and body.get("status") == "success":
            return body
        errors = body.get("errors") or [{}]
        code = errors[0].get("error_code") or errors[0].get("errorCode")
        message = errors[0].get("message") or "Upstox error"
        if code in _TOKEN_ERRORS:
            raise ReauthRequired(message, broker_code=code)
        if resp.status_code < 400:  # 2xx but not "success": we cannot tell what happened
            raise AmbiguousSubmission(message, broker_code=code) if write else BrokerProtocolError(message)
        if write:
            raise OrderRejected(message, broker_code=code)
        raise BrokerProtocolError(message, broker_code=code)

    # ---- auth ----
    def login_url(self, state: str) -> str:
        api_key, _, redirect = self._app()
        q = urlencode({"response_type": "code", "client_id": api_key, "redirect_uri": redirect, "state": state})
        return f"{API}/v2/login/authorization/dialog?{q}"

    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        api_key, api_secret, redirect = self._app()
        code = params.get("code")
        if not code:
            raise InvalidConnectionParams("code missing from the Upstox redirect")
        resp = await send(
            self.ctx.http, "POST", f"{API}/v2/login/authorization/token", write=False,
            headers={"Accept": "application/json"},
            data={"code": code, "client_id": api_key, "client_secret": api_secret,
                  "redirect_uri": redirect, "grant_type": "authorization_code"},
        )
        raise_for_rate_limit_or_server_error(resp, write=False)
        body = parse_json(resp, write=False)
        if not isinstance(body, dict) or resp.status_code >= 400 or not body.get("access_token"):
            errors = (body.get("errors") if isinstance(body, dict) else None) or [{}]
            raise InvalidCredentials(errors[0].get("message") or "Upstox token exchange failed")
        return BrokerSession(
            access_token=body["access_token"],
            broker_user_id=body.get("user_id") or "",
            expires_at=next_ist(3, 30),
        )

    async def validate_session(self) -> None:
        await self._call("GET", f"{API}/v2/user/profile", write=False)

    # ---- reads ----
    async def get_holdings(self) -> list[Holding]:
        body = await self._call("GET", f"{API}/v2/portfolio/long-term-holdings", write=False)
        out = []
        for h in body.get("data") or []:
            if h.get("exchange") not in (None, "NSE", "BSE"):
                continue
            # `quantity` is documented as the total holding; subtract what is already used by orders.
            sellable = to_int(h.get("quantity")) - to_int(h.get("cnc_used_quantity"))
            sym = h.get("trading_symbol") or h.get("tradingsymbol")
            if sym and sellable > 0:
                avg = h.get("average_price")
                out.append(Holding(sym, sellable, Decimal(str(avg)) if avg else None))
        return out

    async def list_orders(self) -> list[OrderSnapshot]:
        body = await self._call("GET", f"{API}/v2/order/retrieve-all", write=False)
        return [to_snapshot(o) for o in body.get("data") or []]

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        body = await self._call("GET", f"{API}/v2/order/details", write=False, params={"order_id": broker_order_id})
        if not body.get("data"):
            raise BrokerProtocolError(f"order {broker_order_id} not found")
        return to_snapshot(body["data"])

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        if exchange != "NSE":
            raise InvalidInstrument(f"unsupported exchange {exchange}")
        eq = instruments.lookup(symbol, live_trading=self.ctx.settings.live_trading_enabled)
        return BrokerInstrument(symbol=symbol, exchange=exchange, broker_symbol=f"NSE_EQ|{eq.isin}", token=eq.token)

    # ---- writes ----
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        body = {
            "quantity": req.quantity,
            "product": "D",
            "validity": "DAY",
            "price": 0,
            "tag": req.client_order_id,
            "instrument_token": self.resolve_symbol(req.symbol, req.exchange).broker_symbol,
            "order_type": "MARKET",
            "transaction_type": req.side.value,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "slice": False,
            "market_protection": -1,
        }
        resp = await self._call("POST", f"{HFT}/v3/order/place", write=True, json=body)
        ids = (resp.get("data") or {}).get("order_ids") or []
        if len(ids) != 1:
            # Zero ids: we can't tell if it was placed. Several: it was sliced — let reconciliation by
            # tag surface every child order instead of silently tracking one.
            raise AmbiguousSubmission(f"expected one order id, got {len(ids)}")
        return PlaceOrderAck(broker_order_id=str(ids[0]))
