"""HTTP helpers shared by the real broker adapters.

The rule every adapter relies on: for a WRITE (place order), anything that could have happened after
the request left us is AmbiguousSubmission, never BrokerUnavailable. Only failures that provably happen
before sending (connect refused/timeout, DNS, pool exhaustion) are "not placed".

    connect error / connect timeout / pool timeout -> BrokerUnavailable      (not sent)
    read/write timeout, connection dropped, 5xx    -> write: AmbiguousSubmission | read: BrokerUnavailable
    unparseable body                               -> write: AmbiguousSubmission | read: BrokerProtocolError
    429                                            -> BrokerRateLimited (honours Retry-After seconds)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

import httpx

from app.domain.errors import AmbiguousSubmission, BrokerProtocolError, BrokerRateLimited, BrokerUnavailable

IST = timezone(timedelta(hours=5, minutes=30), "IST")


async def send(client: httpx.AsyncClient, method: str, url: str, *, write: bool, **kwargs: Any) -> httpx.Response:
    try:
        return await client.request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol) as exc:
        raise BrokerUnavailable(f"could not connect: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:  # ReadTimeout, WriteTimeout, RemoteProtocolError, ReadError, ...
        if write:
            raise AmbiguousSubmission(f"request may have reached the broker: {type(exc).__name__}") from exc
        raise BrokerUnavailable(f"read failed: {type(exc).__name__}") from exc


def retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to the gateway's backoff


def raise_for_rate_limit_or_server_error(resp: httpx.Response, *, write: bool) -> None:
    if resp.status_code == 429:
        raise BrokerRateLimited("broker rate limit (429)", retry_after=retry_after(resp))
    if resp.status_code >= 500:
        if write:
            raise AmbiguousSubmission(f"broker returned {resp.status_code} after receiving the order")
        raise BrokerUnavailable(f"broker returned {resp.status_code}")


def parse_json(resp: httpx.Response, *, write: bool) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        msg = f"unparseable broker response (HTTP {resp.status_code})"
        if write:
            raise AmbiguousSubmission(msg) from exc
        raise BrokerProtocolError(msg) from exc


def next_ist(hour: int, minute: int = 0, *, now: datetime | None = None) -> datetime:
    """Next occurrence of hh:mm IST, as UTC — for brokers whose tokens expire at a fixed time daily."""
    now_ist = (now or datetime.now(UTC)).astimezone(IST)
    candidate = datetime.combine(now_ist.date(), time(hour, minute), IST)
    if candidate <= now_ist:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def parse_ist(value: Any, fmt: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), fmt).replace(tzinfo=IST).astimezone(UTC)
    except ValueError:
        return None


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
