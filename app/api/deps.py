from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import Depends, Header, Request

from app.container import Container
from app.core.errors import ApiError
from app.core.security import owner_for_api_key


def get_container(request: Request) -> Container:
    return request.app.state.container


async def require_owner(
    request: Request, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None
) -> str:
    owner = owner_for_api_key(get_container(request).settings.api_key_map, x_api_key)
    if owner is None:
        raise ApiError(401, "UNAUTHORIZED", "Missing or invalid X-API-Key")
    structlog.contextvars.bind_contextvars(owner_id=owner)
    return owner


ContainerDep = Annotated[Container, Depends(get_container)]
OwnerDep = Annotated[str, Depends(require_owner)]
