from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep, OwnerDep
from app.api.serializers import execution_list_item, execution_out, planned_out
from app.core.errors import ApiError
from app.schemas.executions import ExecutionList, ExecutionOut, ExecutionRequest, NotifyOut, PreviewOut

router = APIRouter(prefix="/executions", tags=["executions"])

_KEY_RE = re.compile(r"^[\x21-\x7e]{1,255}$")  # printable ASCII, no spaces


@router.post(
    "",
    status_code=202,
    response_model=ExecutionOut,
    responses={200: {"model": ExecutionOut, "description": "Idempotent replay of an existing execution"}},
)
async def create_execution(
    req: Annotated[ExecutionRequest, Body()],
    c: ContainerDep,
    owner: OwnerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """Validate, plan and persist the execution, then run it in the background (202).
    Poll `status_url` or wait for the webhook. Sells always complete before buys start."""
    if not idempotency_key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "The Idempotency-Key header is required")
    if not _KEY_RE.match(idempotency_key):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 1-255 printable ASCII characters")
    execution, orders, replayed = await c.executions.create(owner, idempotency_key, req)
    body = execution_out(execution, orders).model_dump(mode="json")
    if replayed:
        return JSONResponse(status_code=200, content=body, headers={"Idempotent-Replayed": "true"})
    return JSONResponse(status_code=202, content=body, headers={"Location": body["status_url"]})


@router.post("/preview", response_model=PreviewOut)
async def preview_execution(req: Annotated[ExecutionRequest, Body()], c: ContainerDep, owner: OwnerDep):
    """Run every validation (including the live holdings check) and return the order plan. Places nothing."""
    planned, issues = await c.executions.preview(owner, req)
    return PreviewOut(valid=not issues, orders=[planned_out(p) for p in planned], errors=[i.to_dict() for i in issues])


@router.get("", response_model=ExecutionList)
async def list_executions(
    c: ContainerDep,
    owner: OwnerDep,
    connection_id: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows = await c.executions.list_for_owner(owner, connection_id=connection_id, status=status, limit=limit, offset=offset)
    return ExecutionList(items=[execution_list_item(r) for r in rows], limit=limit, offset=offset)


@router.get("/{execution_id}", response_model=ExecutionOut)
async def get_execution(execution_id: str, c: ContainerDep, owner: OwnerDep):
    execution, orders = await c.executions.get_with_orders(owner, execution_id)
    return execution_out(execution, orders)


@router.post("/{execution_id}/reconcile", response_model=ExecutionOut)
async def reconcile_execution(execution_id: str, c: ContainerDep, owner: OwnerDep):
    """Re-check UNKNOWN and still-open orders against the broker order book. Never places orders."""
    execution, orders = await c.executions.reconcile(owner, execution_id)
    return execution_out(execution, orders)


@router.post("/{execution_id}/notify", response_model=NotifyOut)
async def resend_notification(execution_id: str, c: ContainerDep, owner: OwnerDep):
    execution = await c.executions.resend_notification(owner, execution_id)
    return NotifyOut(
        execution_id=execution.id,
        notification_status=execution.notification_status,
        attempts=execution.notification_attempts,
    )
