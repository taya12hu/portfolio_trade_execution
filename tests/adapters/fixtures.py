"""Per-broker fixtures for the adapter contract suite. Response bodies follow the shapes in each
broker's published documentation / official SDK (see the adapter module docstrings)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from app.domain.models import BrokerSession, OrderState

TAG = "KXABCDEFGHIJKLMNOP"
SYMBOL = "SBIN"  # present in the instrument map with doc-verified identifiers


def form(request: httpx.Request) -> dict[str, str]:
    from urllib.parse import parse_qsl

    return dict(parse_qsl(request.content.decode()))


def jbody(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content)


@dataclass
class BrokerSpec:
    name: str
    settings: dict[str, Any]
    session: BrokerSession
    place_url: str
    place_ok: dict[str, Any]
    expected_order_id: str
    check_place: Callable[[httpx.Request], None]
    place_without_id: dict[str, Any]
    reject: tuple[int, dict[str, Any]]
    auth_fail: tuple[int, dict[str, Any]]
    orders_url: str
    orders_body: dict[str, Any]
    expected_orders: list[tuple[str, str | None, OrderState, int]]  # (id, tag, state, filled)
    holdings_url: str
    holdings_body: dict[str, Any]
    expected_holdings: list[tuple[str, int]]
    uses_instrument_map: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------------- zerodha

def _check_zerodha(req: httpx.Request) -> None:
    f = form(req)
    assert req.headers["Authorization"] == "token kite-key:AT"
    assert req.headers["X-Kite-Version"] == "3"
    assert f == {
        "tradingsymbol": "SBIN", "exchange": "NSE", "transaction_type": "BUY", "order_type": "MARKET",
        "quantity": "5", "product": "CNC", "validity": "DAY", "market_protection": "-1", "tag": TAG,
    }


ZERODHA = BrokerSpec(
    name="zerodha",
    settings={"zerodha_api_key": "kite-key", "zerodha_api_secret": "kite-secret"},
    session=BrokerSession(access_token="AT", broker_user_id="AB1234"),
    place_url="https://api.kite.trade/orders/regular",
    place_ok={"status": "success", "data": {"order_id": "151220000000000"}},
    expected_order_id="151220000000000",
    check_place=_check_zerodha,
    place_without_id={"status": "success", "data": {}},
    reject=(400, {"status": "error", "message": "Insufficient funds. Required margin is 95417.84",
                  "error_type": "MarginException"}),
    auth_fail=(403, {"status": "error", "message": "Incorrect `api_key` or `access_token`.",
                     "error_type": "TokenException"}),
    orders_url="https://api.kite.trade/orders",
    orders_body={"status": "success", "data": [
        {"order_id": "1", "status": "COMPLETE", "tag": TAG, "tradingsymbol": "SBIN", "transaction_type": "BUY",
         "quantity": 5, "filled_quantity": 5, "pending_quantity": 0, "average_price": 812.4,
         "order_timestamp": "2026-09-30 10:15:00"},
        {"order_id": "2", "status": "REJECTED", "tag": None, "quantity": 200, "filled_quantity": 0,
         "status_message": "Insufficient funds.", "transaction_type": "BUY", "tradingsymbol": "SBIN"},
        {"order_id": "3", "status": "OPEN PENDING", "tag": None, "quantity": 1, "filled_quantity": 0},
        {"order_id": "4", "status": "OPEN", "tag": None, "quantity": 10, "filled_quantity": 3},
        {"order_id": "5", "status": "CANCELLED", "tag": None, "quantity": 1, "filled_quantity": 0},
        {"order_id": "6", "status": "SOMETHING NEW", "tag": None, "quantity": 1, "filled_quantity": 0},
    ]},
    expected_orders=[
        ("1", TAG, OrderState.FILLED, 5), ("2", None, OrderState.REJECTED, 0), ("3", None, OrderState.OPEN, 0),
        ("4", None, OrderState.PARTIALLY_FILLED, 3), ("5", None, OrderState.CANCELLED, 0),
        ("6", None, OrderState.OPEN, 0),  # unknown status: keep watching, never guess terminal
    ],
    holdings_url="https://api.kite.trade/portfolio/holdings",
    holdings_body={"status": "success", "data": [
        {"tradingsymbol": "SBIN", "exchange": "NSE", "quantity": 10, "t1_quantity": 2, "used_quantity": 3,
         "average_price": 500.5},
        {"tradingsymbol": "GOLDBEES", "exchange": "NSE", "quantity": 0, "t1_quantity": 0, "used_quantity": 0},
    ]},
    expected_holdings=[("SBIN", 9)],
)

# ----------------------------------------------------------------------------------- upstox

def _check_upstox(req: httpx.Request) -> None:
    b = jbody(req)
    assert req.headers["Authorization"] == "Bearer AT"
    assert b["instrument_token"] == "NSE_EQ|INE062A01020"
    assert (b["tag"], b["product"], b["order_type"], b["transaction_type"], b["quantity"]) == (TAG, "D", "MARKET", "BUY", 5)
    assert (b["validity"], b["slice"], b["market_protection"], b["price"]) == ("DAY", False, -1, 0)


UPSTOX = BrokerSpec(
    name="upstox",
    settings={"upstox_api_key": "up-key", "upstox_api_secret": "up-secret",
              "upstox_redirect_uri": "http://localhost:8000/broker-connections/upstox/callback"},
    session=BrokerSession(access_token="AT", broker_user_id="UP123"),
    place_url="https://api-hft.upstox.com/v3/order/place",
    place_ok={"status": "success", "data": {"order_ids": ["1644490272000"]}, "metadata": {"latency": 30}},
    expected_order_id="1644490272000",
    check_place=_check_upstox,
    place_without_id={"status": "success", "data": {"order_ids": []}},
    reject=(400, {"status": "error", "errors": [{"errorCode": "UDAPI1052", "message": "Order quantity cannot be zero",
                                                 "propertyPath": None, "invalidValue": None}]}),
    auth_fail=(401, {"status": "error", "errors": [{"error_code": "UDAPI100050",
                                                    "message": "Invalid token used to access API"}]}),
    orders_url="https://api.upstox.com/v2/order/retrieve-all",
    orders_body={"status": "success", "data": [
        {"order_id": "1", "status": "complete", "tag": TAG, "trading_symbol": "SBIN-EQ", "transaction_type": "BUY",
         "quantity": 5, "filled_quantity": 5, "pending_quantity": 0, "average_price": 812.4,
         "order_timestamp": "2026-09-30 10:15:00"},
        {"order_id": "2", "status": "rejected", "tag": None, "quantity": 1, "filled_quantity": 0,
         "status_message": "Insufficient funds"},
        {"order_id": "3", "status": "open pending", "tag": None, "quantity": 1, "filled_quantity": 0},
        {"order_id": "4", "status": "open", "tag": None, "quantity": 10, "filled_quantity": 3},
        {"order_id": "5", "status": "cancelled after market order", "tag": None, "quantity": 1, "filled_quantity": 0},
    ]},
    expected_orders=[
        ("1", TAG, OrderState.FILLED, 5), ("2", None, OrderState.REJECTED, 0), ("3", None, OrderState.OPEN, 0),
        ("4", None, OrderState.PARTIALLY_FILLED, 3), ("5", None, OrderState.CANCELLED, 0),
    ],
    holdings_url="https://api.upstox.com/v2/portfolio/long-term-holdings",
    holdings_body={"status": "success", "data": [
        {"trading_symbol": "SBIN", "exchange": "NSE", "quantity": 10, "t1_quantity": 0, "cnc_used_quantity": 3,
         "average_price": 500.5, "isin": "INE062A01020"},
    ]},
    expected_holdings=[("SBIN", 7)],
    uses_instrument_map=True,
)

# ----------------------------------------------------------------------------------- fyers

def _check_fyers(req: httpx.Request) -> None:
    b = jbody(req)
    assert req.headers["Authorization"] == "FY-APP-100:AT"
    assert b == {"symbol": "NSE:SBIN-EQ", "qty": 5, "type": 2, "side": 1, "productType": "CNC", "limitPrice": 0,
                 "stopPrice": 0, "validity": "DAY", "disclosedQty": 0, "offlineOrder": False, "orderTag": TAG}


FYERS = BrokerSpec(
    name="fyers",
    settings={"fyers_app_id": "FY-APP-100", "fyers_secret_key": "fy-secret",
              "fyers_redirect_uri": "http://localhost:8000/broker-connections/fyers/callback"},
    session=BrokerSession(access_token="AT", broker_user_id="XF1234"),
    place_url="https://api-t1.fyers.in/api/v3/orders/sync",
    place_ok={"s": "ok", "code": 1101, "message": "Order submitted successfully", "id": "25093000001234"},
    expected_order_id="25093000001234",
    check_place=_check_fyers,
    place_without_id={"s": "ok", "code": 1101, "message": "Order submitted successfully"},
    reject=(400, {"s": "error", "code": -99, "message": "RMS: insufficient funds"}),
    auth_fail=(401, {"s": "error", "code": -16, "message": "Could not authenticate the user"}),
    orders_url="https://api-t1.fyers.in/api/v3/orders",
    orders_body={"s": "ok", "code": 200, "orderBook": [
        {"id": "1", "status": 2, "orderTag": f"1:{TAG}", "symbol": "NSE:SBIN-EQ", "side": 1, "qty": 5,
         "filledQty": 5, "remainingQuantity": 0, "tradedPrice": 812.4, "orderDateTime": "30-Sep-2026 10:15:00"},
        {"id": "2", "status": 5, "orderTag": "", "symbol": "NSE:SBIN-EQ", "side": 1, "qty": 1, "filledQty": 0,
         "message": "RMS: insufficient funds"},
        {"id": "3", "status": 4, "symbol": "NSE:SBIN-EQ", "side": -1, "qty": 1, "filledQty": 0},
        {"id": "4", "status": 6, "symbol": "NSE:SBIN-EQ", "side": -1, "qty": 10, "filledQty": 3},
        {"id": "5", "status": 7, "symbol": "NSE:SBIN-EQ", "side": 1, "qty": 1, "filledQty": 0},
        {"id": "6", "status": 2, "orderTag": TAG, "symbol": "NSE:INFY-EQ", "side": 1, "qty": 1, "filledQty": 1},
    ]},
    expected_orders=[
        ("1", TAG, OrderState.FILLED, 5), ("2", None, OrderState.REJECTED, 0), ("3", None, OrderState.OPEN, 0),
        ("4", None, OrderState.PARTIALLY_FILLED, 3), ("5", None, OrderState.CANCELLED, 0),
        ("6", TAG, OrderState.FILLED, 1),
    ],
    holdings_url="https://api-t1.fyers.in/api/v3/holdings",
    holdings_body={"s": "ok", "holdings": [
        {"symbol": "NSE:SBIN-EQ", "holdingType": "HLD", "quantity": 8, "remainingQuantity": 8, "costPrice": 500.5},
        {"symbol": "NSE:SBIN-EQ", "holdingType": "T1", "quantity": 2, "remainingQuantity": 2, "costPrice": 510},
    ]},
    expected_holdings=[("SBIN", 10)],
)

# ----------------------------------------------------------------------------------- angelone

def _check_angel(req: httpx.Request) -> None:
    b = jbody(req)
    assert req.headers["Authorization"] == "Bearer AT"
    assert req.headers["X-PrivateKey"] == "angel-key"
    assert (req.headers["X-UserType"], req.headers["X-SourceID"]) == ("USER", "WEB")
    assert b == {"variety": "NORMAL", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "transactiontype": "BUY",
                 "exchange": "NSE", "ordertype": "MARKET", "producttype": "DELIVERY", "duration": "DAY",
                 "price": "0", "quantity": "5", "ordertag": TAG}


ANGELONE = BrokerSpec(
    name="angelone",
    settings={"angelone_api_key": "angel-key"},
    session=BrokerSession(access_token="AT", broker_user_id="A123", refresh_token="RT", extra={"api_key": "angel-key"}),
    place_url="https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder",
    place_ok={"status": True, "message": "SUCCESS", "errorcode": "",
              "data": {"script": "SBIN-EQ", "orderid": "200910000000111", "uniqueorderid": "34reqfachdfih"}},
    expected_order_id="200910000000111",
    check_place=_check_angel,
    place_without_id={"status": True, "message": "SUCCESS", "errorcode": "", "data": {}},
    reject=(200, {"status": False, "message": "Invalid Product Type", "errorcode": "AB1012", "data": None}),
    auth_fail=(403, {"status": False, "message": "Token Expired", "errorcode": "AG8002", "data": None}),
    orders_url="https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getOrderBook",
    orders_body={"status": True, "message": "SUCCESS", "errorcode": "", "data": [
        {"orderid": "1", "status": "complete", "orderstatus": "complete", "ordertag": TAG, "tradingsymbol": "SBIN-EQ",
         "transactiontype": "BUY", "quantity": "5", "filledshares": "5", "unfilledshares": "0",
         "averageprice": "812.40", "text": "", "updatetime": "30-Sep-2026 10:15:00"},
        {"orderid": "2", "status": "rejected", "orderstatus": "rejected", "ordertag": "", "quantity": "1",
         "filledshares": "0", "averageprice": "0",
         "text": "Your order has been rejected due to Insufficient Funds."},
        {"orderid": "3", "status": "open pending", "orderstatus": "open pending", "quantity": "1", "filledshares": "0"},
        {"orderid": "4", "status": "open", "orderstatus": "open", "quantity": "10", "filledshares": "3"},
        {"orderid": "5", "status": "cancelled", "orderstatus": "cancelled", "quantity": "1", "filledshares": "0"},
    ]},
    expected_orders=[
        ("1", TAG, OrderState.FILLED, 5), ("2", None, OrderState.REJECTED, 0), ("3", None, OrderState.OPEN, 0),
        ("4", None, OrderState.PARTIALLY_FILLED, 3), ("5", None, OrderState.CANCELLED, 0),
    ],
    holdings_url="https://apiconnect.angelone.in/rest/secure/angelbroking/portfolio/v1/getHolding",
    holdings_body={"status": True, "message": "SUCCESS", "errorcode": "", "data": [
        {"tradingsymbol": "SBIN-EQ", "exchange": "NSE", "isin": "INE062A01020", "t1quantity": 2,
         "realisedquantity": 8, "quantity": 8, "averageprice": 573.1, "symboltoken": "3045"},
    ]},
    expected_holdings=[("SBIN", 10)],
    uses_instrument_map=True,
)

# ----------------------------------------------------------------------------------- groww

def _check_groww(req: httpx.Request) -> None:
    b = jbody(req)
    assert req.headers["Authorization"] == "Bearer AT"
    assert req.headers["x-api-version"] == "1.0"
    assert b == {"trading_symbol": "SBIN", "quantity": 5, "price": 0, "trigger_price": None, "validity": "DAY",
                 "exchange": "NSE", "segment": "CASH", "product": "CNC", "order_type": "MARKET",
                 "transaction_type": "BUY", "order_reference_id": TAG}


GROWW = BrokerSpec(
    name="groww",
    settings={},
    session=BrokerSession(access_token="AT", broker_user_id="GW123"),
    place_url="https://api.groww.in/v1/order/create",
    place_ok={"status": "SUCCESS", "payload": {"groww_order_id": "GMK39038RDT490CCVRO", "order_status": "OPEN",
                                               "order_reference_id": TAG, "remark": "Order placed successfully"}},
    expected_order_id="GMK39038RDT490CCVRO",
    check_place=_check_groww,
    place_without_id={"status": "SUCCESS", "payload": {"order_status": "OPEN"}},
    reject=(400, {"status": "FAILURE", "error": {"code": "GA001", "message": "Insufficient balance"}}),
    auth_fail=(401, {"status": "FAILURE", "error": {"code": "GA005", "message": "Unauthorised"}}),
    orders_url="https://api.groww.in/v1/order/list",
    orders_body={"status": "SUCCESS", "payload": {"order_list": [
        {"groww_order_id": "1", "order_status": "EXECUTED", "order_reference_id": TAG, "trading_symbol": "SBIN",
         "transaction_type": "BUY", "quantity": 5, "filled_quantity": 5, "remaining_quantity": 0,
         "average_fill_price": 812.4},
        {"groww_order_id": "2", "order_status": "REJECTED", "quantity": 1, "filled_quantity": 0,
         "remark": "Insufficient balance"},
        {"groww_order_id": "3", "order_status": "ACKED", "quantity": 1, "filled_quantity": 0},
        {"groww_order_id": "4", "order_status": "APPROVED", "quantity": 10, "filled_quantity": 3},
        {"groww_order_id": "5", "order_status": "CANCELLED", "quantity": 1, "filled_quantity": 0},
    ]}},
    expected_orders=[
        ("1", TAG, OrderState.FILLED, 5), ("2", None, OrderState.REJECTED, 0), ("3", None, OrderState.OPEN, 0),
        ("4", None, OrderState.PARTIALLY_FILLED, 3), ("5", None, OrderState.CANCELLED, 0),
    ],
    holdings_url="https://api.groww.in/v1/holdings/user",
    holdings_body={"status": "SUCCESS", "payload": {"holdings": [
        {"isin": "INE062A01020", "trading_symbol": "SBIN", "quantity": 5, "average_price": 500.5},
        {"isin": "INE000000000", "trading_symbol": "ZERO", "quantity": 0, "average_price": 0},
    ]}},
    expected_holdings=[("SBIN", 5)],
)

ALL = [ZERODHA, UPSTOX, FYERS, ANGELONE, GROWW]
