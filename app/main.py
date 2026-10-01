"""FastAPI application factory. Run with: uvicorn --factory app.main:create_app"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_connections, routes_dev, routes_executions, routes_system
from app.container import Container
from app.core.config import Settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_json)
    container = Container(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await container.startup()
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(
        title="Portfolio Trade Execution Engine",
        version="0.1.0",
        description="Executes first-time portfolios and explicit rebalances across Indian brokers "
        "through one adapter interface. The `mock` broker is a simulator; real adapters are disabled "
        "for order placement unless LIVE_TRADING_ENABLED=true.",
        lifespan=lifespan,
    )
    app.state.container = container
    install_error_handlers(app)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type", "X-Request-ID"],
            expose_headers=["Idempotent-Replayed", "X-Request-ID", "Location"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id[:64])
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id[:64]
        return response

    app.include_router(routes_system.router)
    app.include_router(routes_connections.router)
    app.include_router(routes_executions.router)
    if settings.is_dev:
        app.include_router(routes_dev.router)

    ui = Path(__file__).resolve().parents[1] / "frontend" / "index.html"
    if ui.is_file():

        @app.get("/ui", include_in_schema=False)
        async def console() -> FileResponse:
            return FileResponse(ui, media_type="text/html")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/ui")

    return app
