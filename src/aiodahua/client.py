"""Async client for Dahua and Dahua-derived (white-label) devices."""

from __future__ import annotations

import asyncio
import logging
import socket
from types import TracebackType
from typing import Any
from urllib.parse import quote

import aiohttp

from .brands import BrandMatch
from .brands import identify_brand
from .config import build_config_query
from .digest import DigestAuth
from .exceptions import DahuaAuthError
from .exceptions import DahuaConnectionError
from .exceptions import DahuaNotSupportedError
from .exceptions import DahuaResponseError
from .parsers import is_not_supported_response
from .parsers import parse_kv
from .parsers import parse_media_files
from .parsers import parse_storage_info

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 80
DEFAULT_RTSP_PORT = 554
DEFAULT_TIMEOUT = 20

__all__ = ["DEFAULT_PORT", "DEFAULT_RTSP_PORT", "DEFAULT_TIMEOUT", "DahuaClient"]


class DahuaClient:
    """Talks to one Dahua-derived device.

    Works against Dahua and its white-label rebrands (Amcrest, Lorex,
    EmpireTech and others) -- they share the same CGI API. Call
    :meth:`async_identify` to find out which one you have and what quirks to
    expect; everything else works regardless.

    The client does not own its :class:`aiohttp.ClientSession` unless it
    created one, so it is safe to pass Home Assistant's shared session.

    Example:
        >>> async with DahuaClient("192.168.1.10", "admin", "secret") as dev:
        ...     brand = await dev.async_identify()
        ...     print(brand, await dev.async_get_device_type())
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = DEFAULT_PORT,
        rtsp_port: int = DEFAULT_RTSP_PORT,
        session: aiohttp.ClientSession | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        tls: bool = False,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._rtsp_port = rtsp_port
        self._timeout = timeout
        self._scheme = "https" if tls or port == 443 else "http"
        self._base = f"{self._scheme}://{host}:{port}"
        self._session = session
        self._owns_session = session is None
        self._brand: BrandMatch | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DahuaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.async_close()

    async def async_close(self) -> None:
        """Close the session, but only if this client created it."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    @property
    def host(self) -> str:
        return self._host

    @property
    def brand(self) -> BrandMatch | None:
        """The identified brand, or None until :meth:`async_identify` is called."""
        return self._brand

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def async_get_text(self, endpoint: str) -> str:
        """GET a CGI endpoint and return the raw response body.

        Args:
            endpoint: Path below ``/cgi-bin/``, e.g.
                ``"magicBox.cgi?action=getDeviceType"``.

        Raises:
            DahuaAuthError: Credentials rejected.
            DahuaNotSupportedError: Firmware does not implement the endpoint.
            DahuaConnectionError: Device unreachable or the connection dropped.
            DahuaResponseError: Any other HTTP or protocol failure.
        """
        url = f"{self._base}/cgi-bin/{endpoint}"
        response = None
        try:
            async with asyncio.timeout(self._timeout):
                auth = DigestAuth(self._username, self._password, self._get_session())
                response = await auth.request("GET", url)
                text = await response.text()

                if response.status == 401:
                    raise DahuaAuthError(f"Authentication failed for {self._host}")
                if response.status == 400 and is_not_supported_response(text):
                    raise DahuaNotSupportedError(
                        f"{self._host} firmware does not implement {endpoint}"
                    )
                if response.status >= 400:
                    raise DahuaResponseError(
                        f"HTTP {response.status} on {endpoint}",
                        status=response.status,
                        endpoint=endpoint,
                        body=text[:200],
                    )
                # Some firmware answers 200 with the "Bad Request" body.
                if is_not_supported_response(text):
                    raise DahuaNotSupportedError(
                        f"{self._host} firmware does not implement {endpoint}"
                    )
                return text
        except (DahuaAuthError, DahuaNotSupportedError, DahuaResponseError):
            raise
        except TimeoutError as err:
            raise DahuaConnectionError(f"Timed out talking to {self._host}") from err
        except (aiohttp.ClientError, socket.gaierror) as err:
            raise DahuaConnectionError(f"Cannot reach {self._host}: {err}") from err
        finally:
            if response is not None:
                response.close()

    async def async_get(self, endpoint: str) -> dict[str, str]:
        """GET a CGI endpoint and parse the ``key=value`` body into a dict."""
        return parse_kv(await self.async_get_text(endpoint))

    async def async_get_bytes(self, endpoint: str) -> bytes:
        """GET a CGI endpoint and return the raw bytes (e.g. a snapshot JPEG)."""
        url = f"{self._base}/cgi-bin/{endpoint}"
        response = None
        try:
            async with asyncio.timeout(self._timeout):
                auth = DigestAuth(self._username, self._password, self._get_session())
                response = await auth.request("GET", url)
                if response.status == 401:
                    raise DahuaAuthError(f"Authentication failed for {self._host}")
                if response.status >= 400:
                    raise DahuaResponseError(
                        f"HTTP {response.status} on {endpoint}",
                        status=response.status,
                        endpoint=endpoint,
                    )
                return await response.read()
        except (DahuaAuthError, DahuaResponseError):
            raise
        except TimeoutError as err:
            raise DahuaConnectionError(f"Timed out talking to {self._host}") from err
        except (aiohttp.ClientError, socket.gaierror) as err:
            raise DahuaConnectionError(f"Cannot reach {self._host}: {err}") from err
        finally:
            if response is not None:
                response.close()

    async def _async_get_field(self, endpoint: str, key: str) -> str | None:
        data = await self.async_get(endpoint)
        return data.get(key)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    async def async_identify(self) -> BrandMatch:
        """Work out which white-label brand this device is.

        Queries vendor, firmware version and serial number, then combines them.
        The result is cached on the client and also returned. Endpoints that
        are missing on a given firmware are skipped rather than failing the
        whole call.
        """
        vendor = version = serial = None
        for attr, endpoint, key in (
            ("vendor", "magicBox.cgi?action=getVendor", "vendor"),
            ("version", "magicBox.cgi?action=getSoftwareVersion", "version"),
            ("serial", "magicBox.cgi?action=getSerialNo", "sn"),
        ):
            try:
                value = await self._async_get_field(endpoint, key)
            except (DahuaNotSupportedError, DahuaResponseError):
                _LOGGER.debug("%s: %s unavailable", self._host, endpoint)
                value = None
            if attr == "vendor":
                vendor = value
            elif attr == "version":
                version = value
            else:
                serial = value

        self._brand = identify_brand(vendor=vendor, version=version, serial=serial)
        return self._brand

    async def async_get_device_type(self) -> str | None:
        """Model string, e.g. ``NV4116-HS`` or ``IP8M-2493E``."""
        return await self._async_get_field("magicBox.cgi?action=getDeviceType", "type")

    async def async_get_serial_number(self) -> str | None:
        return await self._async_get_field("magicBox.cgi?action=getSerialNo", "sn")

    async def async_get_software_version(self) -> str | None:
        return await self._async_get_field(
            "magicBox.cgi?action=getSoftwareVersion", "version"
        )

    async def async_get_machine_name(self) -> str | None:
        return await self._async_get_field("magicBox.cgi?action=getMachineName", "name")

    async def async_get_system_info(self) -> dict[str, str]:
        return await self.async_get("magicBox.cgi?action=getSystemInfo")

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def async_get_config(self, name: str) -> dict[str, str]:
        """Read a config section, e.g. ``"Encode[0].MainFormat[0].Video"``."""
        return await self.async_get(f"configManager.cgi?action=getConfig&name={name}")

    async def async_set_config(self, params: dict[str, Any]) -> bool:
        """Write config values.

        Values are percent-encoded, so spaces, ``#``, ``+`` and ``%`` are safe.
        Pass ``""`` to clear a field -- the firmware ignores a bare ``Key=``,
        so it is sent as a space that the device trims.

        Raises:
            DahuaValueError: If any value contains ``&``, which the protocol
                cannot represent. Nothing is sent in that case.
        """
        query = build_config_query(params)
        text = await self.async_get_text(f"configManager.cgi?action=setConfig&{query}")
        return "ok" in text.strip().lower()

    async def async_set_machine_name(self, name: str) -> bool:
        return await self.async_set_config({"General.MachineName": name})

    async def async_set_channel_title(self, channel: int, title: str) -> bool:
        """Set the on-screen channel name (safe for ``#``, ``+``, ``%``)."""
        return await self.async_set_config({f"ChannelTitle[{channel}].Name": title})

    # ------------------------------------------------------------------
    # Storage and recordings
    # ------------------------------------------------------------------

    async def async_get_storage_info(self) -> list[dict]:
        """Disk status and capacity, one entry per physical device.

        Note ``used == total`` is normal even on a new disk; see
        :func:`aiodahua.parsers.parse_storage_info`.

        Raises:
            DahuaNotSupportedError: On firmware without ``storageDevice.cgi``
                (most cameras -- this is a recorder endpoint).
        """
        return parse_storage_info(
            await self.async_get_text("storageDevice.cgi?action=getDeviceAllInfo")
        )

    async def async_find_recordings(
        self,
        start_time: str,
        end_time: str,
        channel: int = 1,
        count: int = 20,
    ) -> tuple[int, list[dict]]:
        """List recorded files, to confirm a recorder is really recording.

        Args:
            start_time: ``"YYYY-MM-DD HH:MM:SS"``.
            end_time: ``"YYYY-MM-DD HH:MM:SS"``.
            channel: 1-based. Channel ``0`` is rejected by the firmware.
            count: Maximum number of files to return.

        Returns:
            ``(found, files)``; each file carries a decoded ``record_type``
            (``regular`` / ``motion`` / ``alarm``).
        """
        if channel < 1:
            raise ValueError("channel is 1-based; channel 0 is rejected by firmware")

        created = await self.async_get_text("mediaFileFind.cgi?action=factory.create")
        if "=" not in created:
            raise DahuaResponseError(
                "Could not create a media file finder",
                endpoint="mediaFileFind.cgi?action=factory.create",
                body=created[:200],
            )
        finder = created.split("=", 1)[1].strip()
        # Timestamps contain a space, which must be percent-encoded or the
        # firmware rejects the whole request as a malformed one. Colons and
        # hyphens are left literal; the device accepts those unencoded.
        start = quote(start_time, safe=":-")
        end = quote(end_time, safe=":-")
        try:
            await self.async_get_text(
                f"mediaFileFind.cgi?action=findFile&object={finder}"
                f"&condition.Channel={channel}"
                f"&condition.StartTime={start}"
                f"&condition.EndTime={end}"
            )
            return parse_media_files(
                await self.async_get_text(
                    f"mediaFileFind.cgi?action=findNextFile"
                    f"&object={finder}&count={count}"
                )
            )
        finally:
            for action in ("close", "destroy"):
                try:
                    await self.async_get_text(
                        f"mediaFileFind.cgi?action={action}&object={finder}"
                    )
                except Exception:
                    _LOGGER.debug("%s: mediaFileFind %s failed", self._host, action)

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------

    async def async_get_snapshot(self, channel: int = 1) -> bytes:
        """Fetch a JPEG snapshot. ``channel`` is 1-based."""
        return await self.async_get_bytes(f"snapshot.cgi?channel={channel}")

    def get_rtsp_url(self, channel: int = 1, subtype: int = 0) -> str:
        """Build the RTSP stream URL.

        Args:
            channel: 1-based channel number.
            subtype: ``0`` main stream, ``1`` sub stream.
        """
        user = quote(self._username, safe="")
        password = quote(self._password, safe="")
        return (
            f"rtsp://{user}:{password}@{self._host}:{self._rtsp_port}"
            f"/cam/realmonitor?channel={channel}&subtype={subtype}"
        )
