"""Client tests against a stubbed transport."""

from __future__ import annotations

import pytest

from aiodahua import Brand
from aiodahua import DahuaClient
from aiodahua import DahuaNotSupportedError
from aiodahua import DahuaResponseError
from aiodahua import DahuaValueError


@pytest.fixture
def client():
    return DahuaClient("192.168.4.4", "admin", "secret")


def stub_bytes(client, payload):
    """Replace the binary transport with a canned body."""
    calls = []

    async def _get_bytes(endpoint):
        calls.append(endpoint)
        if isinstance(payload, Exception):
            raise payload
        return payload

    client.async_get_bytes = _get_bytes
    return calls


def stub(client, responses):
    """Replace the transport with a canned {endpoint_substring: body} map."""
    calls = []

    async def _get_text(endpoint):
        calls.append(endpoint)
        for needle, body in responses.items():
            if needle in endpoint:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    client.async_get_text = _get_text
    return calls


class TestIdentify:
    async def test_identifies_amcrest_nvr(self, client):
        stub(
            client,
            {
                "getVendor": "vendor=AC",
                "getSoftwareVersion": "version=4.000.00AC000.0,build:2020-05-21",
                "getSerialNo": "sn=AMR014207899855347",
            },
        )
        match = await client.async_identify()
        assert match.brand is Brand.AMCREST
        assert client.brand is match

    async def test_survives_missing_endpoints(self, client):
        """Older firmware lacks getVendor; identification must still work."""
        stub(
            client,
            {
                "getVendor": DahuaNotSupportedError("no getVendor"),
                "getSoftwareVersion": "version=4.000.00AC000.0",
                "getSerialNo": "sn=AMR014207899855347",
            },
        )
        match = await client.async_identify()
        assert match.brand is Brand.AMCREST
        assert "vendor" not in match.matched_on

    async def test_unknown_device_still_returns(self, client):
        stub(
            client,
            {
                "getVendor": "vendor=Acme",
                "getSoftwareVersion": "version=1.0",
                "getSerialNo": "sn=ZZZ",
            },
        )
        assert (await client.async_identify()).brand is Brand.UNKNOWN


class TestSetConfig:
    async def test_clear_field_sends_space_not_empty(self, client):
        calls = stub(client, {"setConfig": "OK"})
        assert await client.async_set_channel_title(0, "") is True
        assert "ChannelTitle[0].Name=%20" in calls[0]

    async def test_encodes_hash_and_plus(self, client):
        calls = stub(client, {"setConfig": "OK"})
        await client.async_set_channel_title(0, "Lot #2 +Annex")
        assert "Lot%20%232%20%2BAnnex" in calls[0]

    async def test_ampersand_is_rejected_before_sending(self, client):
        calls = stub(client, {"setConfig": "OK"})
        with pytest.raises(DahuaValueError):
            await client.async_set_channel_title(0, "Bill & Ted")
        assert calls == [], "nothing should be sent when the value is invalid"

    async def test_returns_false_when_device_does_not_say_ok(self, client):
        stub(client, {"setConfig": "Error"})
        assert await client.async_set_machine_name("X") is False


class TestRecordings:
    async def test_channel_zero_rejected(self, client):
        with pytest.raises(ValueError, match="1-based"):
            await client.async_find_recordings("a", "b", channel=0)

    async def test_timestamps_are_encoded(self, client):
        """An unencoded space makes the firmware reject the whole request."""
        calls = stub(
            client,
            {
                "factory.create": "result=1",
                "findFile": "OK",
                "findNextFile": "found=0",
                "action=close": "OK",
                "action=destroy": "OK",
            },
        )
        await client.async_find_recordings("2026-09-13 08:00:00", "2026-09-13 23:59:59")
        find = next(c for c in calls if "findFile" in c)
        assert " " not in find
        assert "condition.StartTime=2026-09-13%2008:00:00" in find

    async def test_finder_is_always_cleaned_up(self, client):
        calls = stub(
            client,
            {
                "factory.create": "result=12345",
                "findFile": "OK",
                "findNextFile": "found=1\nitems[0].FilePath=/x/08.20.31[R].dav",
                "action=close": "OK",
                "action=destroy": "OK",
            },
        )
        found, files = await client.async_find_recordings(
            "2026-09-13 00:00:00", "2026-09-13 23:59:59"
        )
        assert found == 1
        assert files[0]["record_type"] == "regular"
        assert any("action=close" in c for c in calls)
        assert any("action=destroy" in c for c in calls)

    async def test_cleanup_runs_even_when_search_fails(self, client):
        calls = stub(
            client,
            {
                "factory.create": "result=999",
                "findFile": RuntimeError("boom"),
                "action=close": "OK",
                "action=destroy": "OK",
            },
        )
        with pytest.raises(RuntimeError):
            await client.async_find_recordings("a", "b")
        assert any("action=destroy" in c for c in calls)


class TestRtspUrl:
    def test_credentials_are_url_escaped(self):
        client = DahuaClient("10.0.0.5", "admin", "p@ss/word")
        url = client.get_rtsp_url(channel=2, subtype=1)
        assert "p%40ss%2Fword" in url
        assert "channel=2&subtype=1" in url

    def test_default_rtsp_port(self, client):
        assert ":554/" in client.get_rtsp_url()


class TestSessionOwnership:
    async def test_does_not_close_a_borrowed_session(self):
        class FakeSession:
            closed = False

            async def close(self):
                self.closed = True

        session = FakeSession()
        client = DahuaClient("h", "u", "p", session=session)
        await client.async_close()
        assert session.closed is False, "must not close a caller-owned session"


class TestHostNormalisation:
    def test_trailing_slash_stripped(self):
        """Otherwise the base URL comes out as http://host/:80."""
        client = DahuaClient("192.168.1.1/", "u", "p")
        assert client._host == "192.168.1.1"
        assert client._base == "http://192.168.1.1:80"

    def test_https_when_port_443(self):
        assert DahuaClient("h", "u", "p", port=443)._base == "https://h:443"


class TestDownloadClip:
    """Pulling recorded video, which ffmpeg has to be able to open."""

    PREAMBLE = bytes(range(256)) * 38  # 9728 bytes, no DHAV anywhere in it
    CLIP = b"DHAV\x00\x01\x02frame-data-here"

    async def test_strips_the_preamble_so_ffmpeg_sees_dhav(self, client):
        """Left in place, ffmpeg reads the file as raw hevc and loses timing."""
        stub_bytes(client, self.PREAMBLE + self.CLIP)
        data = await client.async_download_clip(
            "2026-09-14 03:42:00", "2026-09-14 03:43:30", channel=4
        )
        assert data == self.CLIP
        assert data.startswith(b"DHAV")

    async def test_preamble_length_is_not_assumed(self, client):
        """9733 and 12770 bytes were seen minutes apart on one recorder."""
        for pad in (0, 9733, 12770):
            stub_bytes(client, bytes(pad) + self.CLIP)
            data = await client.async_download_clip(
                "2026-09-14 03:00:00", "2026-09-14 03:01:00"
            )
            assert data == self.CLIP, pad

    async def test_timestamps_are_encoded(self, client):
        calls = stub_bytes(client, self.PREAMBLE + self.CLIP)
        await client.async_download_clip(
            "2026-09-14 03:42:00", "2026-09-14 03:43:30", channel=4
        )
        assert " " not in calls[0]
        assert "startTime=2026-09-14%2003:42:00" in calls[0]
        assert "channel=4" in calls[0]
        assert "subtype=0" in calls[0]

    async def test_substream_can_be_requested(self, client):
        calls = stub_bytes(client, self.CLIP)
        await client.async_download_clip("a 1", "b 2", subtype=1)
        assert "subtype=1" in calls[0]

    async def test_channel_zero_rejected(self, client):
        with pytest.raises(ValueError, match="1-based"):
            await client.async_download_clip("a 1", "b 2", channel=0)

    async def test_inverted_range_rejected_before_the_request(self, client):
        calls = stub_bytes(client, self.CLIP)
        with pytest.raises(ValueError, match="not after"):
            await client.async_download_clip(
                "2026-09-14 04:00:00", "2026-09-14 03:00:00"
            )
        assert not calls

    async def test_empty_range_rejected(self, client):
        with pytest.raises(ValueError, match="not after"):
            await client.async_download_clip(
                "2026-09-14 03:00:00", "2026-09-14 03:00:00"
            )

    async def test_a_range_with_no_recording_is_an_error_not_junk(self, client):
        """The recorder answers 200 with a short body rather than an HTTP error."""
        stub_bytes(client, b"Error\r\nBad Request!")
        with pytest.raises(DahuaResponseError, match="No recording between"):
            await client.async_download_clip(
                "2026-09-14 03:00:00", "2026-09-14 03:01:00"
            )
