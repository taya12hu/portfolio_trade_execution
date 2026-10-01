from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import ContainerDep, OwnerDep
from app.brokers.registry import get_adapter_class, supported_brokers
from app.schemas.connections import BrokerInfo

router = APIRouter()


@router.get("/health", tags=["system"])
async def health(c: ContainerDep):
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "db": "unavailable"})


@router.get("/brokers", tags=["brokers"], response_model=list[BrokerInfo])
async def list_brokers(c: ContainerDep, _owner: OwnerDep):
    out = []
    for name in supported_brokers():
        caps = get_adapter_class(name).capabilities
        out.append(
            BrokerInfo(
                name=name,
                auth_flow=caps.auth_flow.value,
                supports_refresh=caps.supports_refresh,
                is_simulator=caps.is_simulator,
                live_enabled=caps.is_simulator or c.settings.live_trading_enabled,
            )
        )
    return out
