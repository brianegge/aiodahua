"""DHDiscover -- Dahua's UDP device-discovery protocol.

Every Dahua-derived device answers an **unauthenticated** JSON query on UDP
37810 and periodically announces itself to the same port. The reply carries the
model, serial, firmware and full IPv4 configuration, which makes this the only
reliable way to find a device that the rest of the network cannot see:

**A factory-default camera is invisible to every other method.** It ships on a
static ``192.168.1.108/24`` with DHCP disabled, so it never requests a lease,
never appears in the ARP table, and does not answer a ping sweep of the subnet
it is physically plugged into. DHDiscover finds it anyway, because the request
and the reply are link-local -- the device answers from an address that is not
routable from the sender and it does not care. This was confirmed against an
uninitialized IPC-B54IR-Z4E-S3 sitting on the IPCAMS VLAN while addressed on
192.168.1.0/24.

Two ways to ask, and the difference matters:

* **Multicast/broadcast** (:func:`async_discover` with no ``targets``) finds
  devices you do not already know about, but only on the sender's own layer-2
  segment. Multicast is not routed here, so a host on another VLAN sees
  nothing.
* **Unicast** (``targets=["192.168.253.25"]``) works across subnets wherever
  normal routing reaches, and is the way to identify a single known address.

Wire format is a 32-byte ``DHIP`` header followed by JSON. The one field that
is easy to get wrong: ``mac`` is a **top-level key**, a sibling of ``method``
and ``params``, not a member of ``params``. Targeted requests addressed the
other way are silently ignored -- no reply, no error.

:func:`async_set_network_config` writes over the same channel. This is how an
NVR re-addresses a camera it cannot route to, and the message format here is
taken byte-for-byte from a Dahua NVR doing exactly that (see
``tests/test_discovery.py``).

.. warning::
   **One piece of this protocol is not known: how the password hash is
   derived.** The authenticated request carries a 32-hex ``password`` that is
   not ``md5(password)``, not ``md5(md5(password))``, not the Dahua 8-character
   hash, and not HTTP-digest ``HA1`` for any realm tried. A single observed
   sample is not enough to recover the construction, so ``password_hash`` is a
   pass-through: the caller supplies a value already in the device's expected
   form. Callers that do not have one can still send the unauthenticated form,
   which is what an NVR tries first, but whether a device accepts it has not
   been confirmed against hardware.

Consequently the write path is **unverified end to end**. Framing, field names
and nesting match a real NVR exchange and are covered by tests; acceptance by a
device is not something this library has yet demonstrated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from .exceptions import DahuaConnectionError
from .exceptions import DahuaValueError

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DISCOVERY_GROUP",
    "DISCOVERY_PORT",
    "DiscoveredDevice",
    "async_discover",
    "async_set_network_config",
    "decode_dhip",
    "encode_dhip",
]

#: UDP port every Dahua device listens on for discovery.
DISCOVERY_PORT = 37810
#: Multicast group Dahua tools and NVRs use. Devices also answer broadcast.
DISCOVERY_GROUP = "239.255.255.251"
_BROADCAST = "255.255.255.255"

_DHIP_MAGIC = b"DHIP"
_HEADER_LEN = 32
_DEFAULT_TIMEOUT = 3.0
#: Grace period after a write before asking the device to identify itself.
_SETTLE_SECONDS = 2.0


def encode_dhip(payload: bytes) -> bytes:
    """Wrap a JSON payload in the 32-byte DHIP header.

    The header repeats the payload length three times. Devices accept it with
    the session and request IDs zeroed, which is what a stateless discovery
    query does.
    """
    n = len(payload)
    return (
        struct.pack("<I", 0x20)
        + _DHIP_MAGIC
        + struct.pack("<II", 0, 0)
        + struct.pack("<II", n, n)
        + struct.pack("<II", n, n)
        + payload
    )


def decode_dhip(data: bytes) -> dict[str, Any] | None:
    """Unwrap a DHIP datagram, returning the decoded JSON body.

    Returns ``None`` for anything that is not well-formed DHIP-framed JSON,
    which includes the sender's own multicast echo and the unrelated chatter
    that shares this port. Discovery is a broadcast conversation, so callers
    see other devices' traffic and must not treat it as an error.
    """
    if len(data) < _HEADER_LEN or data[4:8] != _DHIP_MAGIC:
        return None
    (declared,) = struct.unpack_from("<I", data, 16)
    body = (
        data[_HEADER_LEN : _HEADER_LEN + declared] if declared else data[_HEADER_LEN:]
    )
    try:
        decoded = json.loads(body.decode("utf-8", "replace").rstrip("\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


@dataclass(frozen=True)
class DiscoveredDevice:
    """One device's answer to a discovery query.

    ``ip`` is what the device believes its address to be, which is not
    necessarily reachable from the caller -- that is the whole point of
    discovering a misaddressed camera.
    """

    mac: str
    ip: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    dhcp: bool | None = None
    device_type: str | None = None
    serial: str | None = None
    machine_name: str | None = None
    version: str | None = None
    http_port: int | None = None
    port: int | None = None
    device_class: str | None = None
    #: Address the datagram actually arrived from, which differs from ``ip``
    #: when the device is on a foreign subnet.
    source_ip: str | None = None
    #: Full ``deviceInfo`` as sent, for fields this class does not model.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def reachable(self) -> bool:
        """Whether the device's own address matches where it answered from.

        ``False`` means the device is on a different subnet than it thinks --
        a factory-default camera on someone else's VLAN.
        """
        return bool(self.ip) and self.ip == self.source_ip

    @classmethod
    def from_message(
        cls, message: dict[str, Any], source_ip: str | None = None
    ) -> DiscoveredDevice | None:
        """Build from a decoded ``client.notifyDevInfo`` message.

        Returns ``None`` for messages that are not device announcements --
        notably the caller's own outgoing ``DHDiscover.search``, which comes
        back via the multicast loopback.
        """
        info = message.get("params", {})
        if not isinstance(info, dict):
            return None
        info = info.get("deviceInfo")
        if not isinstance(info, dict):
            return None
        mac = message.get("mac") or info.get("mac")
        if not mac:
            return None
        v4 = info.get("IPv4Address")
        v4 = v4 if isinstance(v4, dict) else {}
        return cls(
            mac=str(mac).lower(),
            ip=v4.get("IPAddress"),
            netmask=v4.get("SubnetMask"),
            gateway=v4.get("DefaultGateway"),
            dhcp=v4.get("DhcpEnable"),
            device_type=info.get("DeviceType"),
            serial=info.get("SerialNo"),
            machine_name=info.get("MachineName"),
            version=info.get("Version"),
            http_port=info.get("HttpPort"),
            port=info.get("Port"),
            device_class=info.get("DeviceClass"),
            source_ip=source_ip,
            raw=info,
        )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collects replies, keyed by MAC so repeat announcements collapse."""

    def __init__(self) -> None:
        self.devices: dict[str, DiscoveredDevice] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        message = decode_dhip(data)
        if message is None:
            return
        device = DiscoveredDevice.from_message(message, source_ip=addr[0])
        if device is not None:
            self.devices[device.mac] = device

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        _LOGGER.debug("Discovery datagram error: %s", exc)


def _open_socket(source_ip: str | None) -> socket.socket:
    """Open the UDP socket both discovery and setConfig send from.

    Binding to ``source_ip`` is what confines traffic to one segment, and
    ``IP_MULTICAST_IF`` does the same for the multicast leg, which otherwise
    ignores the bind and follows the default route. On a multi-homed host,
    leaving it unset makes an entire VLAN look empty.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if source_ip:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source_ip)
            )
        sock.bind((source_ip or "0.0.0.0", 0))
        sock.setblocking(False)
    except OSError as err:
        sock.close()
        raise DahuaConnectionError(
            f"Could not open discovery socket on {source_ip or '0.0.0.0'}: {err}"
        ) from err
    return sock


def _build_query(mac: str | None) -> bytes:
    """The search request, optionally aimed at one device.

    ``mac`` rides at the top level -- inside ``params`` the device ignores the
    request entirely.
    """
    message: dict[str, Any] = {"method": "DHDiscover.search", "params": {"uni": 1}}
    if mac:
        message["mac"] = mac
    return encode_dhip(json.dumps(message).encode())


async def async_discover(
    *,
    targets: list[str] | None = None,
    source_ip: str | None = None,
    mac: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[DiscoveredDevice]:
    """Find Dahua devices by asking them directly.

    Args:
        targets: Addresses to query. Defaults to the multicast group plus
            broadcast, which reaches the local segment only. Pass unicast
            addresses to probe specific hosts across a routed network.
        source_ip: Local address to send from. **Set this on a multi-homed
            host.** Otherwise the datagrams leave via the default route and
            devices on every other segment stay silent -- which looks exactly
            like "there are no cameras here".
        mac: Restrict the query to one device.
        timeout: Seconds to collect replies. Devices answer within
            milliseconds; the wait is for stragglers.

    Returns:
        Devices that answered, one entry per MAC, ordered by address.

    Raises:
        DahuaConnectionError: If the socket cannot be created or bound, which
            on a multi-homed host usually means ``source_ip`` is not a local
            address.
    """
    loop = asyncio.get_running_loop()
    sock = _open_socket(source_ip)
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol, sock=sock
    )
    try:
        query = _build_query(mac)
        for dest in targets or (DISCOVERY_GROUP, _BROADCAST):
            try:
                transport.sendto(query, (dest, DISCOVERY_PORT))
            except OSError as err:
                # One unreachable destination should not abandon the others.
                _LOGGER.debug("Discovery send to %s failed: %s", dest, err)
        await asyncio.sleep(timeout)
        found = list(protocol.devices.values())
    finally:
        transport.close()

    found.sort(key=lambda d: (_ip_sort_key(d.ip), d.mac))
    return found


def _build_set_config(
    *,
    mac: str,
    ip: str,
    netmask: str,
    gateway: str,
    dhcp: bool,
    current_ip: str,
    username: str,
    password_hash: str,
    device_port: int,
) -> bytes:
    """Build one ``DHDiscover.setConfig`` request.

    Field names, nesting and the ``authorityType`` marker are copied from a
    Dahua NVR re-addressing a camera on a foreign subnet. Two details are not
    guesses and matter:

    * ``IPAddressOld`` must carry the address the device currently holds. The
      device uses it to recognise the request as being about itself.
    * ``authorityType`` appears only on the authenticated form.
    """
    params: dict[str, Any] = {
        "userName": username,
        "password": password_hash,
        "deviceConfig": {
            "Port": device_port,
            "IPv4Address": {
                "IPAddress": ip,
                "SubnetMask": netmask,
                "DefaultGateway": gateway,
                "DhcpEnable": dhcp,
                "IPAddressOld": current_ip,
            },
        },
        "uni": 1,
    }
    if password_hash:
        params["authorityType"] = "Default"
    message = {"method": "DHDiscover.setConfig", "mac": mac, "params": params}
    return encode_dhip(json.dumps(message).encode())


async def async_set_network_config(
    *,
    mac: str,
    ip: str,
    netmask: str,
    gateway: str,
    dhcp: bool = False,
    current_ip: str | None = None,
    username: str = "admin",
    password_hash: str = "",
    device_port: int = 37777,
    targets: list[str] | None = None,
    source_ip: str | None = None,
    verify: bool = True,
    timeout: float = _DEFAULT_TIMEOUT,
) -> DiscoveredDevice | None:
    """Re-address a device over the discovery channel.

    This reaches a device that normal routing cannot, which is its whole
    purpose: a camera sitting on 192.168.1.108 while plugged into a different
    subnet can be moved onto that subnet without touching it physically.

    .. warning::
       Acceptance is unverified -- see the module docstring. Without a correct
       ``password_hash`` the request is the unauthenticated form, which a
       device may ignore. Always check the return value rather than assuming
       the write landed.

    Args:
        mac: Device to act on. **Required**, and the only thing restricting the
            request: the message goes to the multicast group by default, where
            every Dahua on the segment receives it and is expected to ignore a
            MAC that is not its own. Pass ``targets=[address]`` to unicast
            instead when the device is already reachable.
        ip: Address to assign. With ``dhcp=True`` this is still sent -- an NVR
            sends the current address -- but the lease wins.
        netmask: Subnet mask to assign.
        gateway: Default gateway to assign.
        dhcp: Whether the device should use DHCP.
        current_ip: The device's present address, sent as ``IPAddressOld``.
            Defaults to ``ip``, which is correct when only ``dhcp`` changes.
        username: Account the request authenticates as.
        password_hash: Device-form password hash. Empty sends the
            unauthenticated form an NVR tries first.
        device_port: The device's service port, normally 37777.
        targets: Where to send. Defaults to multicast plus broadcast.
        source_ip: Local source address, to pick the segment.
        verify: Re-run discovery afterwards and return what the device now
            reports. Set ``False`` to fire and forget.
        timeout: Seconds to allow for the verification sweep.

    Returns:
        The device as re-discovered when ``verify`` is set, so the caller can
        confirm the change took, or ``None`` if it did not answer. Returns
        ``None`` immediately when ``verify`` is ``False``.

    Raises:
        DahuaValueError: If ``mac`` is empty. A request without a MAC would be
            an instruction to every device on the segment at once.
        DahuaConnectionError: If the socket cannot be created or bound.
    """
    if not mac:
        raise DahuaValueError(
            "async_set_network_config requires a mac: the request is multicast "
            "to the whole segment and the MAC is what confines it to one device"
        )

    loop = asyncio.get_running_loop()
    sock = _open_socket(source_ip)
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, sock=sock
    )
    try:
        requests = []
        if password_hash:
            # Mirrors the NVR, which probes with an empty password before
            # sending the authenticated form.
            requests.append("")
        requests.append(password_hash)
        for attempt in requests:
            payload = _build_set_config(
                mac=mac,
                ip=ip,
                netmask=netmask,
                gateway=gateway,
                dhcp=dhcp,
                current_ip=current_ip or ip,
                username=username,
                password_hash=attempt,
                device_port=device_port,
            )
            for dest in targets or (DISCOVERY_GROUP, _BROADCAST):
                try:
                    transport.sendto(payload, (dest, DISCOVERY_PORT))
                except OSError as err:
                    _LOGGER.debug("setConfig send to %s failed: %s", dest, err)
    finally:
        transport.close()

    if not verify:
        return None
    # The device drops its old address and re-runs DHCP or DAD, so give it a
    # moment before asking who it is now.
    await asyncio.sleep(_SETTLE_SECONDS)
    found = await async_discover(
        targets=targets, source_ip=source_ip, mac=mac, timeout=timeout
    )
    return next((d for d in found if d.mac == mac.lower()), None)


def _ip_sort_key(ip: str | None) -> tuple[int, ...]:
    """Sort IPv4 addresses numerically; unknown addresses sort last."""
    if not ip:
        return (256,)
    try:
        return tuple(int(part) for part in ip.split("."))
    except ValueError:
        return (256,)
