"""Broker connections: login flows, encrypted token storage, gateway construction."""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.brokers.base import AdapterContext, BrokerAdapter
from app.brokers.gateway import BrokerGateway
from app.brokers.ratelimit import RateLimiterRegistry
from app.brokers.registry import create_adapter, get_adapter_class, supported_brokers
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.logging import get_logger
from app.core.security import TokenVault
from app.db.models import BrokerConnection, utcnow
from app.domain.errors import (
    BrokerError,
    BrokerNotConfigured,
    BrokerProtocolError,
    BrokerRateLimited,
    BrokerUnavailable,
    InvalidConnectionParams,
    InvalidCredentials,
    LiveTradingDisabled,
    ReauthRequired,
)
from app.domain.models import AuthFlow, BrokerSession, ConnectionStatus

log = get_logger(__name__)


def api_error_from_broker(exc: BrokerError) -> ApiError:
    """Map a broker error raised outside an order (login, pre-flight, reads) to an HTTP error."""
    if isinstance(exc, ReauthRequired):
        return ApiError(409, "BROKER_REAUTH_REQUIRED", "Broker session expired; reconnect the broker account")
    if isinstance(exc, InvalidCredentials):
        return ApiError(401, "INVALID_BROKER_CREDENTIALS", exc.message)
    if isinstance(exc, InvalidConnectionParams):
        return ApiError(422, "INVALID_CONNECTION_PARAMS", exc.message)
    if isinstance(exc, BrokerNotConfigured):
        return ApiError(503, "BROKER_NOT_CONFIGURED", exc.message)
    if isinstance(exc, LiveTradingDisabled):
        return ApiError(403, "LIVE_TRADING_DISABLED", exc.message)
    if isinstance(exc, (BrokerUnavailable, BrokerRateLimited, BrokerProtocolError)):
        return ApiError(503, "BROKER_UNAVAILABLE", f"Broker unavailable: {exc.message}")
    return ApiError(502, exc.code, exc.message)


class ConnectionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        vault: TokenVault,
        settings: Settings,
        adapter_ctx: AdapterContext,
        limiters: RateLimiterRegistry,
    ) -> None:
        self._sm = sessionmaker
        self._vault = vault
        self._settings = settings
        self._ctx = adapter_ctx
        self._limiters = limiters

    # ------------------------------------------------------------------ login

    def _adapter_class(self, broker: str) -> type[BrokerAdapter]:
        cls = get_adapter_class(broker.lower())
        if cls is None:
            raise ApiError(
                422,
                "UNSUPPORTED_BROKER",
                f"Unsupported broker {broker!r}",
                [{"field": "broker", "supported": supported_brokers()}],
            )
        return cls

    async def connect(self, owner_id: str, broker: str, credentials: dict[str, Any]) -> tuple[BrokerConnection, str | None]:
        """Credential brokers log in immediately (-> ACTIVE). Redirect brokers get a PENDING_LOGIN
        connection and a login URL; the broker redirects back to the callback with a code."""
        cls = self._adapter_class(broker)
        adapter = cls(self._ctx)
        if cls.capabilities.auth_flow is AuthFlow.REDIRECT:
            state = secrets.token_urlsafe(24)
            try:
                url = adapter.login_url(state)
            except BrokerError as exc:
                raise api_error_from_broker(exc) from exc
            async with self._sm() as db:
                row = BrokerConnection(
                    owner_id=owner_id, broker=cls.name, status=ConnectionStatus.PENDING_LOGIN.value, oauth_state=state
                )
                db.add(row)
                await db.commit()
            log.info("connection.login_started", broker=cls.name, connection_id=row.id)
            return row, url

        try:
            session = await adapter.complete_login(credentials)
            config = adapter.connection_config(credentials)
        except BrokerError as exc:
            log.info("connection.login_failed", broker=cls.name, error_code=exc.code)
            raise api_error_from_broker(exc) from exc
        row = await self._upsert_active(owner_id, cls.name, session, config, pending_id=None)
        return row, None

    async def complete_redirect(self, broker: str, params: dict[str, Any]) -> BrokerConnection:
        cls = self._adapter_class(broker)
        state = params.get("state")
        if not state:
            raise ApiError(400, "INVALID_STATE", "missing state parameter")
        async with self._sm() as db:
            pending = (
                await db.execute(
                    select(BrokerConnection).where(
                        BrokerConnection.oauth_state == state,
                        BrokerConnection.broker == cls.name,
                        BrokerConnection.status == ConnectionStatus.PENDING_LOGIN.value,
                    )
                )
            ).scalar_one_or_none()
        if pending is None:
            raise ApiError(400, "INVALID_STATE", "unknown or already used login state")
        try:
            session = await cls(self._ctx).complete_login(params)
        except BrokerError as exc:
            raise api_error_from_broker(exc) from exc
        return await self._upsert_active(pending.owner_id, cls.name, session, {}, pending_id=pending.id)

    async def _upsert_active(
        self,
        owner_id: str,
        broker: str,
        session: BrokerSession,
        config: dict[str, Any],
        pending_id: str | None,
    ) -> BrokerConnection:
        """One connection per (owner, broker, broker user): reconnecting updates it in place."""
        async with self._sm() as db:
            existing = (
                await db.execute(
                    select(BrokerConnection).where(
                        BrokerConnection.owner_id == owner_id,
                        BrokerConnection.broker == broker,
                        BrokerConnection.broker_user_id == session.broker_user_id,
                    )
                )
            ).scalar_one_or_none()
            pending = await db.get(BrokerConnection, pending_id) if pending_id else None
            row = existing or pending or BrokerConnection(owner_id=owner_id, broker=broker)
            if pending is not None and existing is not None:
                await db.delete(pending)
            row.broker_user_id = session.broker_user_id
            row.status = ConnectionStatus.ACTIVE.value
            row.oauth_state = None
            self._store_session(row, session)
            row.config_enc = self._vault.encrypt_json(config)
            row.updated_at = utcnow()
            db.add(row)
            await db.commit()
        log.info("connection.active", broker=broker, connection_id=row.id)
        return row

    def _store_session(self, row: BrokerConnection, session: BrokerSession) -> None:
        row.access_token_enc = self._vault.encrypt(session.access_token)
        row.refresh_token_enc = self._vault.encrypt(session.refresh_token)
        row.session_extra_enc = self._vault.encrypt_json(session.extra or {})
        row.token_expires_at = session.expires_at

    # ------------------------------------------------------------------ queries

    async def get(self, owner_id: str, connection_id: str) -> BrokerConnection:
        async with self._sm() as db:
            row = await db.get(BrokerConnection, connection_id)
        if row is None or row.owner_id != owner_id:
            raise ApiError(404, "CONNECTION_NOT_FOUND", "Broker connection not found")
        return row

    async def list_for_owner(self, owner_id: str) -> list[BrokerConnection]:
        async with self._sm() as db:
            rows = await db.execute(
                select(BrokerConnection)
                .where(BrokerConnection.owner_id == owner_id)
                .order_by(BrokerConnection.created_at.desc())
            )
            return list(rows.scalars())

    async def revoke(self, owner_id: str, connection_id: str) -> None:
        row = await self.get(owner_id, connection_id)
        async with self._sm() as db:
            row = await db.get(BrokerConnection, row.id)
            row.status = ConnectionStatus.REVOKED.value
            row.access_token_enc = row.refresh_token_enc = row.session_extra_enc = None
            row.token_expires_at = None
            await db.commit()
        log.info("connection.revoked", connection_id=connection_id)

    # ------------------------------------------------------------------ gateways

    def gateway_for(self, row: BrokerConnection) -> BrokerGateway:
        if row.status != ConnectionStatus.ACTIVE.value or row.access_token_enc is None:
            raise ReauthRequired(f"connection is {row.status}")
        try:
            session = BrokerSession(
                access_token=self._vault.decrypt(row.access_token_enc),
                refresh_token=self._vault.decrypt(row.refresh_token_enc),
                broker_user_id=row.broker_user_id or "",
                expires_at=row.token_expires_at,
                extra=self._vault.decrypt_json(row.session_extra_enc),
            )
            config = self._vault.decrypt_json(row.config_enc)
        except ValueError as exc:  # TOKEN_ENCRYPTION_KEY changed or data tampered
            raise ReauthRequired("stored broker session cannot be decrypted; reconnect the broker") from exc
        adapter = create_adapter(row.broker, self._ctx, session=session, config=config)
        connection_id = row.id

        async def on_refreshed(new_session: BrokerSession) -> None:
            async with self._sm() as db:
                r = await db.get(BrokerConnection, connection_id)
                if r is not None:
                    self._store_session(r, new_session)
                    await db.commit()

        async def on_expired() -> None:
            await self.mark_expired(connection_id)

        return BrokerGateway(
            adapter,
            settings=self._settings,
            limiters=self._limiters,
            account_key=connection_id,
            on_session_refreshed=on_refreshed,
            on_session_expired=on_expired,
        )

    async def mark_expired(self, connection_id: str) -> None:
        async with self._sm() as db:
            r = await db.get(BrokerConnection, connection_id)
            if r is None or r.status != ConnectionStatus.ACTIVE.value:
                return
            r.status = ConnectionStatus.EXPIRED.value
            await db.commit()
        log.warning("connection.expired", connection_id=connection_id)

    async def gateway_for_owner(self, owner_id: str, connection_id: str) -> tuple[BrokerConnection, BrokerGateway]:
        row = await self.get(owner_id, connection_id)
        try:
            return row, self.gateway_for(row)
        except ReauthRequired as exc:
            await self.mark_expired(row.id)  # no-op unless it was ACTIVE
            raise api_error_from_broker(exc) from exc
