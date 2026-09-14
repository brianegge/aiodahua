"""Exception hierarchy for aiodahua.

These deliberately inherit from the ``aiohttp`` exceptions they replace.
Callers that already write ``except aiohttp.ClientError`` or
``except aiohttp.ClientResponseError`` -- which is how the Dahua Home Assistant
integration does capability detection, probing an endpoint and treating a
failure as "this device does not support it" -- keep working unchanged, while
code that wants precision can catch the specific type instead.
"""

from __future__ import annotations

import aiohttp

__all__ = [
    "DahuaAuthError",
    "DahuaConnectionError",
    "DahuaError",
    "DahuaNotSupportedError",
    "DahuaResponseError",
    "DahuaTimeoutError",
    "DahuaUnsafeOperationError",
    "DahuaValueError",
]


class DahuaError(Exception):
    """Base class for every error raised by this library."""


class DahuaConnectionError(DahuaError, aiohttp.ClientError):
    """The device could not be reached, or the connection dropped.

    Also an :class:`aiohttp.ClientError`.
    """


class DahuaResponseError(DahuaError, aiohttp.ClientResponseError):
    """The device returned an HTTP error or an unparseable response.

    Also an :class:`aiohttp.ClientResponseError`, so ``err.status`` is
    available and existing ``except ClientResponseError`` handlers still fire.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        endpoint: str | None = None,
        body: str | None = None,
    ) -> None:
        aiohttp.ClientResponseError.__init__(
            self,
            None,  # request_info -- not available this far from the request
            (),  # history
            status=status or 0,
            message=message,
        )
        self.endpoint = endpoint
        self.body = body
        self._message = message

    def __str__(self) -> str:
        return self._message


class DahuaAuthError(DahuaResponseError):
    """Authentication failed (bad username or password).

    Carries ``status = 401`` so the integration's reauth trigger, which
    inspects ``exception.status``, fires as it always has.
    """

    def __init__(self, message: str, *, endpoint: str | None = None) -> None:
        super().__init__(message, status=401, endpoint=endpoint)


class DahuaNotSupportedError(DahuaResponseError):
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

    def __init__(self, message: str, *, endpoint: str | None = None) -> None:
        super().__init__(message, status=400, endpoint=endpoint)


class DahuaUnsafeOperationError(DahuaError):
    """The request is known to harm this device, so it was not sent.

    Some firmware does more than refuse a request it cannot serve. A plain GET
    of ``audio.cgi`` reboots a Lorex E891AB on 2.622 firmware, dropping HTTP
    and RTSP for around 105 seconds -- long enough for a recorder to log a
    camera outage. Where a brand profile records that, the library refuses the
    request rather than performing it.

    Pass ``force=True`` to the method if you own the device and accept the
    reboot.
    """


class DahuaValueError(DahuaError, ValueError):
    """A value cannot be represented in the Dahua CGI protocol.

    Raised rather than silently writing corrupted configuration. See
    :func:`aiodahua.config.encode_config_value`.
    """


class DahuaTimeoutError(DahuaConnectionError, TimeoutError):
    """The device did not answer in time.

    Also a builtin :class:`TimeoutError` (which ``asyncio.TimeoutError`` is an
    alias of), so callers that handle timeouts separately from other transport
    failures keep working.
    """
