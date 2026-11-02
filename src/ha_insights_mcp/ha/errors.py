"""Typed errors raised by the Home Assistant client.

Messages are written for humans *and* LLMs: they say what went wrong and what to check.
They must never contain the access token.
"""


class HAError(Exception):
    """Base class for all Home Assistant client errors."""


class HAAuthError(HAError):
    """The token was rejected or lacks permissions."""


class HAConnectionError(HAError):
    """Home Assistant is unreachable (DNS, refused connection, TLS, ...)."""


class HATimeoutError(HAConnectionError):
    """Home Assistant did not answer within the configured timeout."""


class HANotFoundError(HAError):
    """The requested object (e.g. an entity) does not exist."""


class HAResponseError(HAError):
    """Home Assistant answered with an unexpected status or payload."""
