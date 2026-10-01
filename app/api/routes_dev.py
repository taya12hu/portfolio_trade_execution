"""Dev-only webhook receiver so the demo can show delivered, signed notifications.
Mounted only when APP_ENV is dev or test."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app.api.deps import ContainerDep, OwnerDep
from app.core.security import verify_signature
from app.db.models import utcnow

router = APIRouter(prefix="/dev/webhook-sink", tags=["dev"])


@router.post("", status_code=204)
async def receive_webhook(request: Request, c: ContainerDep):
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    c.webhook_sink.appendleft(
        {
            "received_at": utcnow().isoformat(),
            "event_id": request.headers.get("x-event-id"),
            "signature_valid": verify_signature(c.settings.webhook_secret, body, request.headers.get("x-signature")),
            "payload": payload,
        }
    )


@router.get("")
async def list_webhooks(c: ContainerDep, _owner: OwnerDep, execution_id: str | None = None):
    events = list(c.webhook_sink)
    if execution_id:
        events = [e for e in events if (e["payload"] or {}).get("execution_id") == execution_id]
    return events
