"""Login flows and broker-specific edge cases."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.container import _ssl_context

from app.domain.errors import (
    AmbiguousSubmission,
    BrokerNotConfigured,
    BrokerRateLimited,
    InvalidConnectionParams,
    InvalidCredentials,
    ReauthRequired,
)
from app.domain.models import OrderRequest, Side
from tests.adapters.fixtures import ANGELONE, FYERS, GROWW, TAG, UPSTOX, ZERODHA, form, jbody
from tests.adapters.test_contract import adapter_for

REQ = OrderRequest(symbol="SBIN", side=Side.BUY, quantity=5, client_order_id=TAG)


@pytest.fixture
async def http():
    async with httpx.AsyncClient(verify=_ssl_context()) as client:  # reuse the process-wide context
        yield client


# ------------------------------------------------------------------ zerodha

async def test_zerodha_login_url_carries_state_in_redirect_params(http):
    url = adapter_for(ZERODHA, http).login_url("st4te")
    q = parse_qs(urlsplit(url).query)
    assert url.startswith("https://kite.zerodha.com/connect/login?")
    assert q["v"] == ["3"] and q["api_key"] == ["kite-key"] and q["redirect_params"] == ["state=st4te"]


async def test_zerodha_session_exchange_uses_the_documented_checksum(http):
    a = adapter_for(ZERODHA, http)
    a.session = None
    with respx.mock() as router:
        route = router.post("https://api.kite.trade/session/token").mock(return_value=httpx.Response(
            200, json={"status": "success", "data": {"user_id": "AB1234", "access_token": "AT2", "refresh_token": ""}}))
        session = await a.complete_login({"request_token": "rt123", "status": "success", "state": "x"})
    f = form(route.calls.last.request)
    assert f["checksum"] == hashlib.sha256(b"kite-keyrt123kite-secret").hexdigest()
    assert (f["api_key"], f["request_token"]) == ("kite-key", "rt123")
    assert "Authorization" not in route.calls.last.request.headers
    assert (session.access_token, session.broker_user_id, session.refresh_token) == ("AT2", "AB1234", None)
    assert session.expires_at is not None


async def test_zerodha_bad_request_token_is_invalid_credentials(http):
    with respx.mock() as router:
        router.post("https://api.kite.trade/session/token").mock(return_value=httpx.Response(
            403, json={"status": "error", "message": "Token is invalid or has expired.", "error_type": "TokenException"}))
        with pytest.raises(InvalidCredentials):
            await adapter_for(ZERODHA, http).complete_login({"request_token": "used"})
    with pytest.raises(InvalidCredentials):
        await adapter_for(ZERODHA, http).complete_login({"status": "cancelled"})
    with pytest.raises(InvalidConnectionParams):
        await adapter_for(ZERODHA, http).complete_login({})


async def test_missing_app_credentials_is_a_clear_config_error(http):
    with pytest.raises(BrokerNotConfigured):
        adapter_for(ZERODHA, http, zerodha_api_key="").login_url("s")


async def test_zerodha_network_exception_on_place_is_ambiguous(http):
    """NetworkException = the API couldn't talk to the OMS; the order may or may not exist."""
    with respx.mock() as router:
        router.post(ZERODHA.place_url).mock(return_value=httpx.Response(
            502, json={"status": "error", "message": "OMS unreachable", "error_type": "NetworkException"}))
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(ZERODHA, http).place_order(REQ)


async def test_zerodha_get_order_uses_latest_history_entry(http):
    with respx.mock() as router:
        router.get("https://api.kite.trade/orders/9").mock(return_value=httpx.Response(200, json={
            "status": "success", "data": [
                {"order_id": "9", "status": "PUT ORDER REQ RECEIVED", "filled_quantity": 0, "quantity": 1},
                {"order_id": "9", "status": "OPEN", "filled_quantity": 0, "quantity": 1},
                {"order_id": "9", "status": "COMPLETE", "filled_quantity": 1, "quantity": 1, "average_price": 10}]}))
        snap = await adapter_for(ZERODHA, http).get_order("9")
    assert snap.status.value == "FILLED"


# ------------------------------------------------------------------ upstox

async def test_upstox_login(http):
    a = adapter_for(UPSTOX, http)
    q = parse_qs(urlsplit(a.login_url("st4te")).query)
    assert q == {"response_type": ["code"], "client_id": ["up-key"], "state": ["st4te"],
                 "redirect_uri": ["http://localhost:8000/broker-connections/upstox/callback"]}
    with respx.mock() as router:
        route = router.post("https://api.upstox.com/v2/login/authorization/token").mock(
            return_value=httpx.Response(200, json={"access_token": "AT2", "user_id": "UP123", "email": "x@y"}))
        s = await a.complete_login({"code": "c0de", "state": "st4te"})
    f = form(route.calls.last.request)
    assert f == {"code": "c0de", "client_id": "up-key", "client_secret": "up-secret", "grant_type": "authorization_code",
                 "redirect_uri": "http://localhost:8000/broker-connections/upstox/callback"}
    assert (s.access_token, s.broker_user_id) == ("AT2", "UP123")


async def test_upstox_sliced_response_is_ambiguous_not_silently_tracked(http):
    with respx.mock() as router:
        router.post(UPSTOX.place_url).mock(return_value=httpx.Response(
            200, json={"status": "success", "data": {"order_ids": ["1", "2"]}}))
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(UPSTOX, http).place_order(REQ)


# ------------------------------------------------------------------ fyers

async def test_fyers_login_uses_app_id_hash_and_profile_id(http):
    a = adapter_for(FYERS, http)
    a.session = None
    q = parse_qs(urlsplit(a.login_url("st4te")).query)
    assert q["state"] == ["st4te"] and q["client_id"] == ["FY-APP-100"] and q["response_type"] == ["code"]
    with respx.mock() as router:
        auth = router.post("https://api-t1.fyers.in/api/v3/validate-authcode").mock(return_value=httpx.Response(
            200, json={"s": "ok", "code": 200, "access_token": "AT2", "refresh_token": "RT2"}))
        router.get("https://api-t1.fyers.in/api/v3/profile").mock(return_value=httpx.Response(
            200, json={"s": "ok", "code": 200, "data": {"fy_id": "XF1234"}}))
        s = await a.complete_login({"auth_code": "ac0de", "state": "st4te", "s": "ok"})
    assert jbody(auth.calls.last.request) == {
        "grant_type": "authorization_code", "code": "ac0de",
        "appIdHash": hashlib.sha256(b"FY-APP-100:fy-secret").hexdigest()}
    assert (s.access_token, s.broker_user_id) == ("AT2", "XF1234")


# ------------------------------------------------------------------ angelone

async def test_angelone_login_never_keeps_the_pin(http):
    a = adapter_for(ANGELONE, http)
    a.session = None
    with respx.mock() as router:
        route = router.post("https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword").mock(
            return_value=httpx.Response(200, json={"status": True, "message": "SUCCESS", "errorcode": "",
                                                   "data": {"jwtToken": "JWT", "refreshToken": "RT", "feedToken": "F"}}))
        s = await a.complete_login({"client_code": "a123", "pin": "1234", "totp": "654321"})
    assert jbody(route.calls.last.request) == {"clientcode": "A123", "password": "1234", "totp": "654321"}
    assert route.calls.last.request.headers["X-PrivateKey"] == "angel-key"
    assert (s.access_token, s.refresh_token, s.broker_user_id) == ("JWT", "RT", "A123")
    assert "1234" not in repr(s) and "654321" not in repr(s)


async def test_angelone_failed_login_status_string_false(http):
    a = adapter_for(ANGELONE, http)
    with respx.mock() as router:
        router.post("https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword").mock(
            return_value=httpx.Response(200, json={"status": "false", "message": "Login Id or password is invalid",
                                                   "errorcode": "AB1007", "data": "null"}))
        with pytest.raises(InvalidCredentials):
            await a.complete_login({"client_code": "A123", "pin": "0000", "totp": "000000"})


async def test_angelone_403_without_token_code_is_a_rate_limit_not_an_expiry(http):
    with respx.mock() as router:
        router.post(ANGELONE.place_url).mock(return_value=httpx.Response(403, text="Access denied because of exceeding rate limit"))
        with pytest.raises(BrokerRateLimited):
            await adapter_for(ANGELONE, http).place_order(REQ)
    with respx.mock() as router:
        router.get(ANGELONE.orders_url).mock(return_value=httpx.Response(403, json={"status": False, "message": "x", "errorcode": ""}))
        with pytest.raises(BrokerRateLimited):
            await adapter_for(ANGELONE, http).list_orders()


async def test_angelone_internal_error_on_place_is_ambiguous(http):
    with respx.mock() as router:
        router.post(ANGELONE.place_url).mock(return_value=httpx.Response(
            200, json={"status": False, "message": "Something Went Wrong, Please Try After Sometime", "errorcode": "AB1004"}))
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(ANGELONE, http).place_order(REQ)


async def test_angelone_empty_order_book_is_null(http):
    with respx.mock() as router:
        router.get(ANGELONE.orders_url).mock(return_value=httpx.Response(
            200, json={"status": True, "message": "SUCCESS", "errorcode": "", "data": None}))
        assert await adapter_for(ANGELONE, http).list_orders() == []


async def test_angelone_refresh(http):
    with respx.mock() as router:
        route = router.post("https://apiconnect.angelone.in/rest/auth/angelbroking/jwt/v1/generateTokens").mock(
            return_value=httpx.Response(200, json={"status": True, "message": "SUCCESS", "errorcode": "",
                                                   "data": {"jwtToken": "JWT2", "refreshToken": "RT2"}}))
        s = await adapter_for(ANGELONE, http).refresh_session()
    assert jbody(route.calls.last.request) == {"refreshToken": "RT"}
    assert (s.access_token, s.refresh_token, s.extra["api_key"]) == ("JWT2", "RT2", "angel-key")
    with respx.mock() as router:
        router.post("https://apiconnect.angelone.in/rest/auth/angelbroking/jwt/v1/generateTokens").mock(
            return_value=httpx.Response(200, json={"status": False, "message": "Refresh Token Expired", "errorcode": "AB8051"}))
        with pytest.raises(ReauthRequired):
            await adapter_for(ANGELONE, http).refresh_session()


# ------------------------------------------------------------------ groww

async def test_groww_totp_login(http):
    a = adapter_for(GROWW, http)
    a.session = None
    with respx.mock() as router:
        tok = router.post("https://api.groww.in/v1/token/api/access").mock(
            return_value=httpx.Response(200, json={"token": "AT2", "tokenRefId": "r"}))
        router.get("https://api.groww.in/v1/user/detail").mock(
            return_value=httpx.Response(200, json={"status": "SUCCESS", "payload": {"ucc": "GW123"}}))
        s = await a.complete_login({"api_key": "user-key", "totp": "123456"})
    assert tok.calls.last.request.headers["Authorization"] == "Bearer user-key"
    assert jbody(tok.calls.last.request) == {"key_type": "totp", "totp": "123456"}
    assert (s.access_token, s.broker_user_id) == ("AT2", "GW123")


async def test_groww_approval_login_checksum(http):
    a = adapter_for(GROWW, http)
    with respx.mock() as router:
        tok = router.post("https://api.groww.in/v1/token/api/access").mock(
            return_value=httpx.Response(200, json={"token": "AT2"}))
        router.get("https://api.groww.in/v1/user/detail").mock(
            return_value=httpx.Response(200, json={"status": "SUCCESS", "payload": {}}))
        s = await a.complete_login({"api_key": "user-key", "api_secret": "sec"})
    body = jbody(tok.calls.last.request)
    assert body["key_type"] == "approval"
    assert body["checksum"] == hashlib.sha256(f"sec{body['timestamp']}".encode()).hexdigest()
    assert s.broker_user_id.startswith("groww-")  # stable id derived without exposing the key


async def test_groww_login_needs_exactly_one_factor(http):
    with pytest.raises(InvalidConnectionParams):
        await adapter_for(GROWW, http).complete_login({"api_key": "k", "totp": "1", "api_secret": "s"})
    with pytest.raises(InvalidConnectionParams):
        await adapter_for(GROWW, http).complete_login({"api_key": "k"})


async def test_groww_order_book_paginates(http):
    page0 = [{"groww_order_id": str(i), "order_status": "EXECUTED", "quantity": 1, "filled_quantity": 1} for i in range(100)]
    page1 = [{"groww_order_id": "last", "order_status": "ACKED", "order_reference_id": TAG, "quantity": 1}]
    with respx.mock() as router:
        route = router.get(GROWW.orders_url).mock(side_effect=[
            httpx.Response(200, json={"status": "SUCCESS", "payload": {"order_list": page0}}),
            httpx.Response(200, json={"status": "SUCCESS", "payload": {"order_list": page1}}),
        ])
        snaps = await adapter_for(GROWW, http).list_orders()
    assert len(snaps) == 101 and snaps[-1].client_order_id == TAG
    assert [c.request.url.params["page"] for c in route.calls] == ["0", "1"]
