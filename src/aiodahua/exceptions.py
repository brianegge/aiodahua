"""Exception hierarchy for aiodahua."""

from __future__ import annotations

__all__ = [
    "DahuaAuthError",
    "DahuaConnectionError",
    "DahuaError",
    "DahuaNotSupportedError",
    "DahuaResponseError",
    "DahuaValueError",
]


class DahuaError(Exception):
    """Base class for every error raised by this library."""


class DahuaConnectionError(DahuaError):
    """The device could not be reached, or the connection dropped."""


class DahuaAuthError(DahuaError):
    """Authentication failed (bad username or password)."""


class DahuaResponseError(DahuaError):
    """The device returned an HTTP error or an unparseable response.

    Attributes:
        status: HTTP status code, when one was received.
        endpoint: The CGI endpoint that failed.
        body: A truncated copy of the response body, when available.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        endpoint: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.body = body


class DahuaNotSupportedError(DahuaError):
    """The device's firmware does not implement this endpoint.

    Dahua firmware answers ``Error\\nBad Request!`` for endpoints it does not
    have, rather than a 404. Older builds are missing a lot -- an NV4116-HS on
    2020 firmware has no ``storageDevice.cgi?action=getSmartInfo`` and no
    ``upgrader.cgi`` at all.

    Caveat: the firmware returns the *same* response for a genuinely malformed
    request, for example one with an unencoded space in a parameter. The two
    cases are indistinguishable from the response alone, so treat this as
    "the device refused this request" rather than proof the endpoint is absent.
    """


class DahuaValueError(DahuaError, ValueError):
    """A value cannot be represented in the Dahua CGI protocol.

    Raised rather than silently writing corrupted configuration. See
    :func:`aiodahua.config.encode_config_value`.
    """
