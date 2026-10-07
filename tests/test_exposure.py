"""Tests for exposure parsing and the shutter cap.

The response below is an Amcrest IP5M-T1179E after its shutter was capped at
1/60 s, trimmed to the fields the parser reads. Writing ``VideoInExposure``
on that camera was confirmed to update ``VideoInOptions`` too, and the
reverse.
"""

from __future__ import annotations

import pytest

from aiodahua import DahuaClient
from aiodahua import DahuaValueError
from aiodahua import parse_exposure
from aiodahua import parse_kv
from aiodahua import parse_shutter
from aiodahua.exposure import auto_exposure_config
from aiodahua.exposure import shutter_range_config

CAPPED = "\r\n".join(
    f"table.VideoInExposure[0][{p}].{k}={v}"
    for p, gain_max in ((0, 70), (1, 50), (2, 50))
    for k, v in (
        ("GainMax", gain_max),
        ("GainMin", 0),
        ("Mode", 4),
        ("Value1", "0.100000"),
        ("Value2", "16.670000"),
    )
)


def stub(client, responses):
    """Replace the transport with a canned {endpoint_substring: body} map."""
    calls = []

    async def _get_text(endpoint):
        calls.append(endpoint)
        for needle, body in responses.items():
            if needle in endpoint:
                return body
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    client.async_get_text = _get_text
    return calls


class TestParseShutter:
    @pytest.mark.parametrize(
        ("value", "ms"), [("1/60", 16.667), ("1/120", 8.333), (" 1/30 ", 33.333)]
    )
    def test_fractions_of_a_second(self, value, ms):
        assert parse_shutter(value) == pytest.approx(ms, abs=0.001)

    def test_milliseconds(self):
        assert parse_shutter(16.67) == 16.67
        assert parse_shutter("8.33") == 8.33

    @pytest.mark.parametrize("value", ["fast", "1/0", "0", -5, "1/-60"])
    def test_rejects_nonsense(self, value):
        with pytest.raises(DahuaValueError):
            parse_shutter(value)


class TestParseExposure:
    def test_one_entry_per_profile(self):
        profiles = parse_exposure(parse_kv(CAPPED))
        assert [p["name"] for p in profiles] == ["day", "night", "normal"]

    def test_capped_profile(self):
        day = parse_exposure(parse_kv(CAPPED))[0]
        assert day["mode_name"] == "shutter range"
        assert day["shutter_range_applies"] is True
        assert day["fastest_ms"] == 0.1
        assert day["slowest_ms"] == 16.67
        assert day["slowest"] == "1/60"
        assert (day["gain_min"], day["gain_max"]) == (0, 70)

    def test_auto_mode_says_the_range_is_ignored(self):
        """Auto mode still reports a range, which misleads if read alone."""
        raw = parse_kv(CAPPED.replace("Mode=4", "Mode=0"))
        assert not any(p["shutter_range_applies"] for p in parse_exposure(raw))
        assert parse_exposure(raw)[0]["mode_name"] == "auto"

    def test_unconfirmed_mode_is_reported_not_guessed(self):
        raw = parse_kv(CAPPED.replace("[0][0].Mode=4", "[0][0].Mode=6"))
        assert parse_exposure(raw)[0]["mode_name"] == "unknown (6)"

    def test_other_channels_ignored(self):
        raw = parse_kv(CAPPED + "\r\ntable.VideoInExposure[1][0].Mode=0")
        assert len(parse_exposure(raw, channel=0)) == 3
        assert len(parse_exposure(raw, channel=1)) == 1


class TestShutterRangeConfig:
    def test_caps_every_profile_by_default(self):
        params = shutter_range_config("1/60")
        assert params["VideoInExposure[0][0].Mode"] == "4"
        assert params["VideoInExposure[0][2].Value1"] == "0.10"
        assert params["VideoInExposure[0][1].Value2"] == "16.67"
        assert len(params) == 9

    def test_one_profile(self):
        params = shutter_range_config(8.33, profiles=[1], channel=2)
        assert set(params) == {
            "VideoInExposure[2][1].Mode",
            "VideoInExposure[2][1].Value1",
            "VideoInExposure[2][1].Value2",
        }

    def test_fastest_slower_than_slowest_is_refused(self):
        with pytest.raises(DahuaValueError, match="slower than slowest"):
            shutter_range_config("1/120", fastest="1/60")

    def test_unknown_profile_is_refused(self):
        with pytest.raises(DahuaValueError, match="profile"):
            shutter_range_config("1/60", profiles=[3])

    def test_auto_writes_only_the_mode(self):
        assert auto_exposure_config(profiles=[0, 2]) == {
            "VideoInExposure[0][0].Mode": "0",
            "VideoInExposure[0][2].Mode": "0",
        }


class TestClient:
    @pytest.fixture
    def client(self):
        return DahuaClient("192.168.4.4", "admin", "secret")

    async def test_get_exposure(self, client):
        stub(client, {"name=VideoInExposure": CAPPED})
        assert (await client.async_get_exposure())[1]["slowest"] == "1/60"

    async def test_set_shutter_range_sends_one_request(self, client):
        calls = stub(client, {"setConfig": "OK"})
        assert await client.async_set_shutter_range("1/60") is True
        assert len(calls) == 1
        assert "VideoInExposure[0][0].Value2=16.67" in calls[0]

    async def test_bad_speed_sends_nothing(self, client):
        calls = stub(client, {"setConfig": "OK"})
        with pytest.raises(DahuaValueError):
            await client.async_set_shutter_range("soon")
        assert calls == []

    async def test_set_auto(self, client):
        calls = stub(client, {"setConfig": "OK"})
        assert await client.async_set_exposure_auto() is True
        assert "VideoInExposure[0][1].Mode=0" in calls[0]
