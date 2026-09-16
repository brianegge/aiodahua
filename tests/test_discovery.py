"""Tests for DHDiscover framing and parsing.

The payload in :data:`REAL_ANNOUNCEMENT` is a verbatim ``client.notifyDevInfo``
captured off the wire from an uninitialized IPC-B54IR-Z4E-S3 on 2026-09-15,
while it sat on the IPCAMS VLAN still addressed 192.168.1.108/24. It is kept
whole rather than trimmed to the fields under test, so that a parser change is
checked against what a device really sends.
"""

from __future__ import annotations

import json

import pytest

from aiodahua import DISCOVERY_GROUP
from aiodahua import DISCOVERY_PORT
from aiodahua import DahuaValueError
from aiodahua import DiscoveredDevice
from aiodahua import async_set_network_config
from aiodahua import decode_dhip
from aiodahua import encode_dhip
from aiodahua.discovery import _build_query
from aiodahua.discovery import _build_set_config
from aiodahua.discovery import _ip_sort_key

REAL_ANNOUNCEMENT = {
    "mac": "98:f9:cc:af:59:0d",
    "method": "client.notifyDevInfo",
    "params": {
        "deviceInfo": {
            "AbroadInfo": "Oversea",
            "AlarmInputChannels": 2,
            "AlarmOutputChannels": 1,
            "DeviceClass": "IPC",
            "DeviceType": "IPC-B54IR-Z4E-S3",
            "Find": "BC",
            "HttpPort": 80,
            "IPv4Address": {
                "DefaultGateway": "192.168.1.1",
                "DhcpEnable": False,
                "IPAddress": "192.168.1.108",
                "SubnetMask": "255.255.255.0",
            },
            "IPv6Address": {
                "DefaultGateway": "::",
                "IPAddress": "::",
                "LinkLocalAddress": "fe80::9af9:ccff:feaf:590d/64",
            },
            "Init": 3733,
            "MachineName": "CC10BD8PAGCF779",
            "Port": 37777,
            "SerialNo": "CC10BD8PAGCF779",
            "Version": "3.142.0000000.8.R",
            "VideoInputChannels": 1,
        }
    },
}


class TestDhipFraming:
    def test_round_trip(self):
        payload = b'{"method":"DHDiscover.search"}'
        assert decode_dhip(encode_dhip(payload)) == json.loads(payload)

    def test_header_is_32_bytes_with_magic_at_offset_4(self):
        framed = encode_dhip(b"{}")
        assert framed[4:8] == b"DHIP"
        assert len(framed) == 32 + 2

    def test_length_recorded_three_times(self):
        """The device accepts the header only with the length repeated."""
        framed = encode_dhip(b'{"a":1}')
        assert framed[16:20] == framed[24:28] == framed[28:32]

    @pytest.mark.parametrize(
        "data",
        [b"", b"short", b"x" * 40, b"\x20\x00\x00\x00NOPE" + b"\x00" * 24],
        ids=["empty", "truncated", "no-magic", "wrong-magic"],
    )
    def test_non_dhip_returns_none(self, data):
        assert decode_dhip(data) is None

    def test_trailing_nul_tolerated(self):
        """Devices pad the payload; json.loads would choke on the NULs."""
        assert decode_dhip(encode_dhip(b'{"a":1}' + b"\x00" * 5)) == {"a": 1}

    def test_garbage_body_returns_none(self):
        """Other traffic shares this port; it must not raise."""
        assert decode_dhip(encode_dhip(b"not json")) is None

    def test_json_scalar_returns_none(self):
        assert decode_dhip(encode_dhip(b"42")) is None


class TestBuildQuery:
    def test_mac_is_top_level_not_in_params(self):
        """Inside params the device ignores the request entirely -- no reply."""
        message = decode_dhip(_build_query("98:f9:cc:af:59:0d"))
        assert message["mac"] == "98:f9:cc:af:59:0d"
        assert "mac" not in message["params"]

    def test_untargeted_query_omits_mac(self):
        message = decode_dhip(_build_query(None))
        assert "mac" not in message
        assert message["method"] == "DHDiscover.search"

    def test_uni_flag_always_set(self):
        assert decode_dhip(_build_query(None))["params"] == {"uni": 1}


class TestDiscoveredDevice:
    def test_parses_real_announcement(self):
        dev = DiscoveredDevice.from_message(
            REAL_ANNOUNCEMENT, source_ip="192.168.1.108"
        )
        assert dev.mac == "98:f9:cc:af:59:0d"
        assert dev.ip == "192.168.1.108"
        assert dev.netmask == "255.255.255.0"
        assert dev.gateway == "192.168.1.1"
        assert dev.dhcp is False
        assert dev.device_type == "IPC-B54IR-Z4E-S3"
        assert dev.serial == "CC10BD8PAGCF779"
        assert dev.version == "3.142.0000000.8.R"
        assert dev.http_port == 80
        assert dev.device_class == "IPC"

    def test_raw_keeps_unmodelled_fields(self):
        dev = DiscoveredDevice.from_message(REAL_ANNOUNCEMENT)
        assert dev.raw["AlarmInputChannels"] == 2
        assert dev.raw["IPv6Address"]["LinkLocalAddress"].startswith("fe80::")

    def test_mac_normalised_to_lowercase(self):
        message = {**REAL_ANNOUNCEMENT, "mac": "98:F9:CC:AF:59:0D"}
        assert DiscoveredDevice.from_message(message).mac == "98:f9:cc:af:59:0d"

    def test_own_search_echo_is_not_a_device(self):
        """Multicast loops our own query back; it must not become a result."""
        echo = {"method": "DHDiscover.search", "params": {"uni": 1}}
        assert DiscoveredDevice.from_message(echo) is None

    @pytest.mark.parametrize(
        "message",
        [
            {"method": "x", "params": {"deviceInfo": {}}},
            {"method": "x", "params": "notadict"},
            {"method": "x", "params": {"deviceInfo": "notadict"}},
            {"method": "x"},
        ],
        ids=["no-mac", "params-not-dict", "info-not-dict", "no-params"],
    )
    def test_malformed_returns_none(self, message):
        assert DiscoveredDevice.from_message(message) is None

    def test_missing_ipv4_block_does_not_raise(self):
        message = {"mac": "aa:bb:cc:dd:ee:ff", "params": {"deviceInfo": {}}}
        dev = DiscoveredDevice.from_message(message)
        assert dev.ip is None
        assert dev.dhcp is None

    def test_reachable_false_when_on_foreign_subnet(self):
        """The factory-default case: answers from an address it cannot own."""
        dev = DiscoveredDevice.from_message(
            REAL_ANNOUNCEMENT, source_ip="192.168.1.108"
        )
        assert dev.reachable is True
        moved = DiscoveredDevice.from_message(
            REAL_ANNOUNCEMENT, source_ip="192.168.253.7"
        )
        assert moved.reachable is False

    def test_reachable_false_without_source(self):
        assert DiscoveredDevice.from_message(REAL_ANNOUNCEMENT).reachable is False


class TestSortKey:
    def test_numeric_not_lexical(self):
        """'192.168.1.9' must sort before '192.168.1.10'."""
        assert _ip_sort_key("192.168.1.9") < _ip_sort_key("192.168.1.10")

    @pytest.mark.parametrize("ip", [None, "", "not-an-ip", "a.b.c.d"])
    def test_unknown_sorts_last(self, ip):
        assert _ip_sort_key(ip) > _ip_sort_key("255.255.255.255")


def test_protocol_constants():
    assert DISCOVERY_PORT == 37810
    assert DISCOVERY_GROUP == "239.255.255.251"


# The two setConfig requests a Dahua NVR (LTN6416) sent while re-addressing the
# camera above, captured 2026-09-15. It sends the unauthenticated form first,
# then the authenticated one. Reproduced verbatim so that a change to the
# builder is checked against a real device exchange, not against itself.
NVR_SETCONFIG_UNAUTHENTICATED = {
    "method": "DHDiscover.setConfig",
    "mac": "98:f9:cc:af:59:0d",
    "params": {
        "userName": "admin",
        "password": "",
        "deviceConfig": {
            "Port": 37777,
            "IPv4Address": {
                "IPAddress": "192.168.1.108",
                "SubnetMask": "255.255.255.0",
                "DefaultGateway": "192.168.1.1",
                "DhcpEnable": True,
                "IPAddressOld": "192.168.1.108",
            },
        },
        "uni": 1,
    },
}

NVR_SETCONFIG_AUTHENTICATED = {
    **NVR_SETCONFIG_UNAUTHENTICATED,
    "params": {
        **NVR_SETCONFIG_UNAUTHENTICATED["params"],
        "password": "241A0035A7DD942CFFFE68E6571B21AA",
        "authorityType": "Default",
    },
}

# Referenced rather than repeated inline: a literal here reads as a hardcoded
# credential, and it is really one field of the captured request above.
_NVR_HASH = NVR_SETCONFIG_AUTHENTICATED["params"]["password"]

_NVR_ARGS = {
    "mac": "98:f9:cc:af:59:0d",
    "ip": "192.168.1.108",
    "netmask": "255.255.255.0",
    "gateway": "192.168.1.1",
    "dhcp": True,
    "current_ip": "192.168.1.108",
    "username": "admin",
    "device_port": 37777,
}


class TestBuildSetConfig:
    def test_matches_nvr_unauthenticated_request(self):
        built = decode_dhip(_build_set_config(**_NVR_ARGS, password_hash=""))
        assert built == NVR_SETCONFIG_UNAUTHENTICATED

    def test_matches_nvr_authenticated_request(self):
        built = decode_dhip(_build_set_config(**_NVR_ARGS, password_hash=_NVR_HASH))
        assert built == NVR_SETCONFIG_AUTHENTICATED

    def test_authority_type_only_when_authenticated(self):
        """The NVR omits it on the unauthenticated probe."""
        anon = decode_dhip(_build_set_config(**_NVR_ARGS, password_hash=""))
        assert "authorityType" not in anon["params"]

    def test_mac_is_top_level(self):
        built = decode_dhip(_build_set_config(**_NVR_ARGS, password_hash=""))
        assert built["mac"] == "98:f9:cc:af:59:0d"
        assert "mac" not in built["params"]

    def test_address_old_is_carried_separately(self):
        """The device matches the request to itself via IPAddressOld."""
        built = decode_dhip(
            _build_set_config(
                **{**_NVR_ARGS, "ip": "192.168.253.25", "current_ip": "192.168.1.108"},
                password_hash="",
            )
        )
        v4 = built["params"]["deviceConfig"]["IPv4Address"]
        assert v4["IPAddress"] == "192.168.253.25"
        assert v4["IPAddressOld"] == "192.168.1.108"


class TestSetNetworkConfigGuards:
    @pytest.mark.asyncio
    async def test_empty_mac_refused_before_any_send(self):
        """Without a MAC this is an instruction to every device on the segment."""
        with pytest.raises(DahuaValueError, match="requires a mac"):
            await async_set_network_config(
                mac="",
                ip="192.168.253.25",
                netmask="255.255.255.0",
                gateway="192.168.253.1",
            )
