"""Error hierarchy and the transient/deterministic split.

The retry layer only ever retries transient errors. Anything else is a
deterministic failure that would recur, so it goes straight to
fallback/on_error/dead-letter handling.
"""

from __future__ import annotations


class GraphxError(Exception):
    pass


class TransientError(GraphxError):
    """Retryable: rate limits, timeouts, connection failures, crashed servers.

    retry_after: seconds the provider asked us to wait (e.g. HTTP Retry-After).
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NodeTimeout(TransientError):
    pass


class ConfigError(GraphxError):
    """Bad node config discovered at runtime; never retried."""


class ValidationFailed(GraphxError):
    """Output schema validation still failing after all re-asks."""


class GuardTripped(GraphxError):
    def __init__(self, guard: str, detail: str):
        super().__init__(f"guard '{guard}' tripped: {detail}")
        self.guard = guard
        self.detail = detail


class InterruptSignal(GraphxError):
    """Raised by ctx.interrupt(); caught by the executor, never by nodes."""

    def __init__(self, payload: object):
        super().__init__("interrupt")
        self.payload = payload


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    # Duck-typing escape hatch so node handlers can flag custom exceptions.
    return bool(getattr(exc, "transient", False))
