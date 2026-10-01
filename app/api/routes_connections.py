from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep, OwnerDep
from app.api.serializers import connection_out
from app.connections.service import api_error_from_broker
from app.domain.errors import BrokerError
from app.domain.models import ConnectionStatus
from app.schemas.connections import ConnectionOut, CreateConnectionRequest, HoldingOut

router = APIRouter(prefix="/broker-connections", tags=["broker-connections"])


@router.post(
    "",
    response_model=ConnectionOut,
    status_code=201,
    responses={200: {"model": ConnectionOut, "description": "Redirect login started (PENDING_LOGIN)"}},
)
async def create_connection(body: CreateConnectionRequest, c: ContainerDep, owner: OwnerDep):
    row, login_url = await c.connections.connect(owner, body.broker, body.credentials)
    out = connection_out(row, login_url)
    if row.status == ConnectionStatus.PENDING_LOGIN.value:
        return JSONResponse(status_code=200, content=out.model_dump(mode="json"))
    return out


@router.get("", response_model=list[ConnectionOut])
async def list_connections(c: ContainerDep, owner: OwnerDep):
    return [connection_out(r) for r in await c.connections.list_for_owner(owner)]


@router.get("/{broker}/callback", response_model=ConnectionOut)
async def oauth_callback(broker: str, request: Request, c: ContainerDep):
    """Broker redirect target. Not API-key protected (it is a browser redirect); the single-use
    `state` value created by POST /broker-connections authenticates it."""
    row = await c.connections.complete_redirect(broker, dict(request.query_params))
    return connection_out(row)


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: str, c: ContainerDep, owner: OwnerDep):
    return connection_out(await c.connections.get(owner, connection_id))


@router.get("/{connection_id}/holdings", response_model=list[HoldingOut])
async def get_holdings(connection_id: str, c: ContainerDep, owner: OwnerDep):
    _row, gateway = await c.connections.gateway_for_owner(owner, connection_id)
    try:
        holdings = await gateway.get_holdings()
    except BrokerError as exc:
        raise api_error_from_broker(exc) from exc
    return [HoldingOut(symbol=h.symbol, quantity=h.quantity, avg_price=h.avg_price) for h in holdings]


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, c: ContainerDep, owner: OwnerDep):
    await c.connections.revoke(owner, connection_id)
    return Response(status_code=204)
