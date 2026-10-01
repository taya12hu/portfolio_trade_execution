"""Broker error taxonomy. Every adapter maps every broker failure onto one of these.

The single most important property of each error is whether the order *might have been placed*:

    placed = False  -> safe to retry (the broker never accepted the request)
    placed = None   -> unknown; must NOT be retried, reconcile against the order book instead
    placed = True   -> the broker received and decided on it (e.g. a rejection); never retry
"""

from __future__ import annotations


class BrokerError(Exception):
    code = "BROKER_ERROR"
    placed: bool | None = False
    retryable = False

    def __init__(self, message: str = "", *, broker_code: str | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.broker_code = broker_code


class ReauthRequired(BrokerError):
    """Access token expired or revoked. The request was refused before anything was placed."""

    code = "AUTH_EXPIRED"


class InvalidCredentials(BrokerError):
    """Login failed (bad PIN / TOTP / request token)."""

    code = "INVALID_BROKER_CREDENTIALS"


class InvalidConnectionParams(BrokerError):
    """Login inputs are malformed (missing field, bad config) — a client error, not a broker answer."""

    code = "INVALID_CONNECTION_PARAMS"


class BrokerNotConfigured(BrokerError):
    """The platform's app credentials for this broker are missing from the environment."""

    code = "BROKER_NOT_CONFIGURED"


class BrokerRateLimited(BrokerError):
    code = "RATE_LIMITED"
    retryable = True

    def __init__(self, message: str = "", *, retry_after: float | None = None, broker_code: str | None = None) -> None:
        super().__init__(message, broker_code=broker_code)
        self.retry_after = retry_after


class BrokerUnavailable(BrokerError):
    """Connection refused / DNS / connect timeout / 503 before the request was processed."""

    code = "BROKER_UNAVAILABLE"
    retryable = True


class OrderRejected(BrokerError):
    """The broker received the order and refused it (funds, RMS, circuit, DDPI...)."""

    code = "ORDER_REJECTED"
    placed = True


class InvalidInstrument(BrokerError):
    code = "INVALID_INSTRUMENT"


class AmbiguousSubmission(BrokerError):
    """We sent place_order but cannot tell whether the broker accepted it (read timeout, 5xx after send,
    unparseable response). Never retried; resolved by looking up our tag in the order book."""

    code = "AMBIGUOUS_SUBMISSION"
    placed = None


class BrokerProtocolError(BrokerError):
    """Unexpected response shape on a read call."""

    code = "BROKER_PROTOCOL_ERROR"
    retryable = True


class LiveTradingDisabled(BrokerError):
    code = "LIVE_TRADING_DISABLED"
