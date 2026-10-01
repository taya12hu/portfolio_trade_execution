"""Application settings, loaded from environment variables (and `.env` when present)."""

from __future__ import annotations

from functools import cached_property
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    log_json: bool = True
    cors_origins: str = ""  # comma-separated; empty = same-origin only

    # "key1:owner1,key2:owner2". Each API key identifies one consumer (owner).
    api_keys: str = "dev-key:demo-owner"
    # Fernet key for broker tokens at rest. Required outside dev/test.
    token_encryption_key: str | None = None
    webhook_secret: str = "dev-webhook-secret"

    # Kill switch: real (non-simulator) adapters refuse to place orders unless this is true.
    live_trading_enabled: bool = False

    # Fat-finger caps.
    max_qty_per_order: int = Field(default=100_000, gt=0)
    max_orders_per_execution: int = Field(default=50, gt=0)

    # Broker calls.
    submit_concurrency: int = Field(default=5, gt=0)
    broker_connect_timeout_s: float = 5.0
    broker_read_timeout_s: float = 10.0
    broker_max_retries: int = 3  # retries of *safe* errors only (not placed / reads)
    retry_backoff_base_s: float = 0.5
    max_retry_after_s: float = 10.0

    # Order monitoring and reconciliation.
    poll_interval_s: float = 1.0
    order_monitor_timeout_s: float = 60.0
    reconcile_attempts: int = 3
    reconcile_delay_s: float = 3.0
    # Manual reconcile only marks an UNKNOWN order FAILED(NOT_PLACED_CONFIRMED) once it has been
    # absent from the broker order book for at least this long after submission.
    reconcile_confirm_after_s: float = 120.0

    # Webhook delivery.
    webhook_timeout_s: float = 5.0
    webhook_max_attempts: int = 3
    webhook_backoff_base_s: float = 1.0

    # Broker app credentials (the platform's app, not the end user's). Placeholders in .env.example.
    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_redirect_uri: str = ""
    fyers_app_id: str = ""
    fyers_secret_key: str = ""
    fyers_redirect_uri: str = ""
    angelone_api_key: str = ""
    # AngelOne wants client network headers on every call; orders must come from the registered static IP.
    angelone_client_local_ip: str = "127.0.0.1"
    angelone_client_public_ip: str = "127.0.0.1"
    angelone_mac_address: str = "00:00:00:00:00:00"
    groww_api_key: str = ""
    groww_api_secret: str = ""

    @property
    def is_dev(self) -> bool:
        return self.app_env in ("dev", "test")

    @cached_property
    def api_key_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for pair in self.api_keys.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, sep, owner = pair.partition(":")
            if not sep or not key or not owner:
                raise ValueError("API_KEYS must look like 'key1:owner1,key2:owner2'")
            mapping[key.strip()] = owner.strip()
        return mapping

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
