"""Transport-level tests: firmware error shapes, digest reuse, TLS, audio guard.

Every response body here was copied off a real device on a live network. The
firmware differences are the whole point of this library, so the tests are
written against what the hardware actually says rather than what the protocol
documentation claims.

The fake session below stands in for ``aiohttp.ClientSession``. aioresponses
would be the obvious tool, but its latest release (0.7.9) constructs a
``ClientResponse`` without the ``stream_writer`` argument aiohttp 3.14
requires, so it cannot mock anything at all on a current aiohttp.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from aiodahua import Brand
from aiodahua import DahuaAuthError
from aiodahua import DahuaClient
from aiodahua import DahuaNotSupportedError
from aiodahua import DahuaResponseError
from aiodahua import DahuaTimeoutError
from aiodahua import DahuaUnsafeOperationError
from aiodahua import identify_brand

CHALLENGE = {
    "www-authenticate": 'Digest realm="Login to ND011912041000", qop="auth", '
    'nonce="4220250", opaque="16cceef6c0afb3f828d86b172acc59e1ee756c90"'
}


class FakeResponse:
    """The parts of ``aiohttp.ClientResponse`` this library actually touches."""

    def __init__(self, status=200, body="", headers=None):
        self.status = status
        self._body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.closed = False

    async def text(self):
        return self._body

    async def read(self):
        return self._body if isinstance(self._body, bytes) else self._body.encode()

    def close(self):
        self.closed = True


class FakeSession:
    """Serves queued responses, matching on a substring of the URL.

    Entries are consumed in order, so a 401 challenge followed by the real
    answer models a digest handshake. An entry may be a callable, which is how
    a device that accepts the connection and then never answers is simulated.
    """

    def __init__(self, queue):
        self.queue = [list(item) if isinstance(item, tuple) else item for item in queue]
        self.calls = []

    async def request(self, method, url, headers=None, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, **kwargs}
        )
        for entry in self.queue:
            needle, response = entry[0], entry[1]
            if needle in url:
                self.queue.remove(entry)
                if callable(response):
                    return await response()
                return response
        raise AssertionError(f"unexpected request: {url}")

    def count(self, needle):
        return sum(1 for call in self.calls if needle in call["url"])

    async def close(self):
        pass


def make_client(queue, **kwargs):
    session = FakeSession(queue)
    client = DahuaClient("192.168.4.4", "admin", "secret", session=session, **kwargs)
    return client, session


VENDOR = "getVendor"
TYPE = "getDeviceType"
AUDIO = "audio.cgi"


class TestMissingEndpoints:
    """A missing endpoint is an error body, never a 404."""

    async def test_400_bad_request(self):
        """Older firmware: E891AB 2.622, NV4116-HS 4.000."""
        client, _ = make_client(
            [(VENDOR, FakeResponse(400, "Error\r\nBad Request!\r\n"))]
        )
        with pytest.raises(DahuaNotSupportedError):
            await client.async_get_text("magicBox.cgi?action=getVendor")

    async def test_501_not_implemented(self):
        """Newer firmware answers 501 for the very same missing endpoint.

        Confirmed on IPC-B54IR-ASE-S3 (3.142.15OG000.0.R) and IPC-Color4K-T
        (3.000.0000000.20.R); IPC-T5442TM-AS on 2.840 still answers 400.
        """
        client, _ = make_client(
            [(VENDOR, FakeResponse(501, "Error\r\nNot Implemented!\r\n"))]
        )
        with pytest.raises(DahuaNotSupportedError):
            await client.async_get_text("magicBox.cgi?action=getVendor")

    async def test_501_in_the_bytes_transport(self):
        client, _ = make_client(
            [("snapshot.cgi", FakeResponse(501, "Error\r\nNot Implemented!\r\n"))]
        )
        with pytest.raises(DahuaNotSupportedError):
            await client.async_get_bytes("snapshot.cgi?channel=1")

    async def test_other_4xx_stays_a_response_error(self):
        """A 400 that is not the "missing endpoint" body must not be swallowed."""
        client, _ = make_client(
            [(VENDOR, FakeResponse(400, "something else entirely"))]
        )
        with pytest.raises(DahuaResponseError) as err:
            await client.async_get_text("magicBox.cgi?action=getVendor")
        assert not isinstance(err.value, DahuaNotSupportedError)

    async def test_snapshot_bytes_still_come_back_whole(self):
        client, _ = make_client(
            [("snapshot.cgi", FakeResponse(200, b"\xff\xd8\xffdata"))]
        )
        assert (
            await client.async_get_bytes("snapshot.cgi?channel=1")
            == b"\xff\xd8\xffdata"
        )


class TestErrorBodyWithHttp200:
    async def test_error_body_is_not_returned_as_data(self):
        """An LTN6416 answers 200 with this when a config section is missing.

        It parses into {"Error: Error -1 getting param in name": "Lighting[0][0]"},
        which looks exactly like a successful read.
        """
        client, _ = make_client(
            [
                (
                    VENDOR,
                    FakeResponse(
                        200, "Error: Error -1 getting param in name=Lighting[0][0]"
                    ),
                )
            ]
        )
        with pytest.raises(DahuaResponseError) as err:
            await client.async_get_text("magicBox.cgi?action=getVendor")
        assert "Error -1 getting param" in str(err.value)

    async def test_real_data_is_untouched(self):
        client, _ = make_client([(VENDOR, FakeResponse(200, "vendor=Dahua\r\n"))])
        assert await client.async_get("magicBox.cgi?action=getVendor") == {
            "vendor": "Dahua"
        }


class TestDigestReuse:
    async def test_nonce_is_carried_between_requests(self):
        """The second request must not need its own 401 challenge.

        Each challenge is a second TCP connection (the device answers
        "Connection: close") and its own Login/Logout pair in the device's
        finite audit log.
        """
        client, session = make_client(
            [
                (VENDOR, FakeResponse(401, "", CHALLENGE)),
                (VENDOR, FakeResponse(200, "vendor=Dahua")),
                (TYPE, FakeResponse(200, "type=E891AB")),
            ]
        )
        await client.async_get_text("magicBox.cgi?action=getVendor")
        assert client._digest_state["challenge"]["nonce"] == "4220250"
        assert session.count(VENDOR) == 2  # challenge + answer

        await client.async_get_text("magicBox.cgi?action=getDeviceType")
        assert session.count(TYPE) == 1  # no second challenge
        assert "AUTHORIZATION" in session.calls[-1]["headers"]

    async def test_nonce_count_increments(self):
        client, _ = make_client(
            [
                (VENDOR, FakeResponse(401, "", CHALLENGE)),
                (VENDOR, FakeResponse(200, "vendor=Dahua")),
                (TYPE, FakeResponse(200, "type=E891AB")),
            ]
        )
        await client.async_get_text("magicBox.cgi?action=getVendor")
        first = client._digest_state["nonce_count"]
        await client.async_get_text("magicBox.cgi?action=getDeviceType")
        assert client._digest_state["nonce_count"] == first + 1

    async def test_rejected_credentials_do_not_retry_forever(self):
        """Bad credentials used to re-challenge without bound.

        Every attempt counts towards the device's own lockout -- LockLoginTimes
        is 5 or 10 on the cameras here, with a 300s LoginFailLockTime -- so a
        mistyped password could lock the account out of the device entirely.
        """
        client, session = make_client(
            [
                (VENDOR, FakeResponse(401, "", CHALLENGE)),
                (VENDOR, FakeResponse(401, "", CHALLENGE)),
                (VENDOR, FakeResponse(401, "", CHALLENGE)),
            ]
        )
        with pytest.raises(DahuaAuthError):
            await client.async_get_text("magicBox.cgi?action=getVendor")
        assert session.count(VENDOR) == 2  # the challenge and one retry, no more


class TestTls:
    def test_verification_is_on_by_default(self):
        assert DahuaClient("h", "u", "p")._ssl is None

    def test_verify_ssl_false_disables_it(self):
        assert DahuaClient("h", "u", "p", verify_ssl=False)._ssl is False

    async def test_ssl_setting_reaches_the_request(self):
        """An NV4108E-HS redirects port 80 to its self-signed HTTPS."""
        client, session = make_client(
            [(VENDOR, FakeResponse(200, "vendor=Dahua"))], verify_ssl=False
        )
        await client.async_get_text("magicBox.cgi?action=getVendor")
        assert session.calls[0]["ssl"] is False

    def test_port_443_implies_https(self):
        assert DahuaClient("h", "u", "p", port=443)._base == "https://h:443"


class TestAudioProbeGuard:
    """audio.cgi reboots a Lorex E891AB. The library must not send it."""

    async def test_refused_on_lorex(self):
        client, session = make_client([])
        client._brand = identify_brand(
            vendor="LOREX", version="2.622.00LR000.10.R", serial="ND011912041000"
        )
        assert client._brand.brand is Brand.LOREX
        with pytest.raises(DahuaUnsafeOperationError):
            await client.async_get_audio_input(1)
        assert session.calls == []  # nothing was sent at all

    async def test_force_overrides_the_guard(self):
        client, _ = make_client([(AUDIO, FakeResponse(200, ""))])
        client._brand = identify_brand(vendor="LOREX")
        assert await client.async_get_audio_input(1, force=True) is True

    async def test_allowed_on_amcrest(self):
        """An IP5M-T1179E and an NV4108E-HS both take this safely."""
        client, _ = make_client([(AUDIO, FakeResponse(200, ""))])
        client._brand = identify_brand(vendor="Amcrest", serial="AMC060E2586932DD02")
        assert await client.async_get_audio_input(1) is True

    async def test_identifies_first_when_brand_is_unknown(self):
        """The guard is useless if it only works after an explicit identify."""
        client, session = make_client(
            [
                (VENDOR, FakeResponse(200, "vendor=LOREX")),
                ("getSoftwareVersion", FakeResponse(200, "version=2.622.00LR000.10.R")),
                ("getSerialNo", FakeResponse(200, "sn=ND011912041000")),
            ]
        )
        with pytest.raises(DahuaUnsafeOperationError):
            await client.async_get_audio_input(1)
        assert session.count(AUDIO) == 0

    async def test_never_reads_the_audio_body(self):
        """The response is an endless stream; reading it would never return."""
        response = FakeResponse(200, "")
        client, _ = make_client([(AUDIO, response)])
        client._brand = identify_brand(vendor="Amcrest")
        await client.async_get_audio_input(1)
        assert response.closed is True

    async def test_device_that_refuses_audio_returns_false(self):
        client, _ = make_client(
            [(AUDIO, FakeResponse(400, "Error\r\nBad Request!\r\n"))]
        )
        client._brand = identify_brand(vendor="Amcrest")
        assert await client.async_get_audio_input(1) is False

    async def test_post_audio_is_refused_on_lorex(self):
        """The speaker path uses the same endpoint that reboots the camera.

        Only the GET has been observed rebooting an E891AB -- there is no Lorex
        speaker here to test the POST against -- so this guard is by inference.
        It is the safe direction to be wrong in: the backchannel is the path
        this brand's profile prefers anyway.
        """
        client, session = make_client([])
        client._brand = identify_brand(vendor="LOREX")
        with pytest.raises(DahuaUnsafeOperationError):
            await client.async_post_audio(b"audio", 1)
        assert session.calls == []

    async def test_post_audio_force_overrides_the_guard(self):
        """force=True is the documented escape hatch for someone who owns a
        Lorex and accepts the reboot. It is covered on the probe above but not
        here, and an unhonoured flag on the speaker path would leave the guard
        unconditional with nothing to catch it."""
        client, session = make_client(
            [
                ("getMachineName", FakeResponse(200, "name=cam")),
                (AUDIO, FakeResponse(200, "")),
            ]
        )
        client._brand = identify_brand(vendor="LOREX")
        with contextlib.suppress(Exception):
            await client.async_post_audio(b"audio", 1, force=True)
        assert session.count(AUDIO) >= 1

    async def test_post_audio_allowed_on_other_brands(self):
        # It primes digest on a cheap GET before POSTing the stream.
        client, session = make_client(
            [
                ("getMachineName", FakeResponse(200, "name=cam")),
                (AUDIO, FakeResponse(200, "")),
            ]
        )
        client._brand = identify_brand(vendor="Amcrest")
        with contextlib.suppress(Exception):
            await client.async_post_audio(b"audio", 1)
        assert session.count(AUDIO) >= 1

    async def test_channel_zero_is_rejected_before_sending(self):
        """audio.cgi is 1-based, and channel 0 triggers a stale=TRUE loop.

        The device answers 401 with stale=TRUE, which asks the client to retry
        with the fresh nonce -- then does it again, for as long as the client
        keeps playing along. Confirmed on IPC-B54IR-ASE-S3, IP5M-T1179E and
        LTN6416; every one of them serves channel 1 happily.
        """
        client, session = make_client([])
        client._brand = identify_brand(vendor="Amcrest")
        with pytest.raises(ValueError, match="1-based"):
            await client.async_get_audio_input(0)
        assert session.calls == []

    async def test_stale_challenge_loop_terminates(self):
        """A device that answers stale=TRUE forever must not hang the caller."""
        stale = {
            "www-authenticate": 'Digest realm="Login to AMC0", qop="auth", '
            'nonce="9", algorithm=MD5, stale=TRUE'
        }
        client, session = make_client(
            [
                (AUDIO, FakeResponse(401, "", stale)),
                (AUDIO, FakeResponse(401, "", stale)),
                (AUDIO, FakeResponse(401, "", stale)),
            ]
        )
        client._brand = identify_brand(vendor="Amcrest")
        with pytest.raises(DahuaAuthError):
            await client.async_get_audio_input(1)
        assert session.count(AUDIO) == 2

    async def test_hang_raises_a_dahua_timeout(self):
        """A device that accepts the connection and never sends headers."""

        async def never_answers():
            await asyncio.sleep(5)

        client, _ = make_client([(AUDIO, never_answers)], timeout=1)
        client._brand = identify_brand(vendor="Amcrest")
        with pytest.raises(DahuaTimeoutError):
            await client.async_get_audio_input(1)

    async def test_probe_honours_the_client_timeout(self):
        """It used to use a module constant, ignoring the caller's timeout."""

        async def never_answers():
            await asyncio.sleep(5)

        client, _ = make_client([(AUDIO, never_answers)], timeout=1)
        client._brand = identify_brand(vendor="Amcrest")
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(DahuaTimeoutError):
            await client.async_get_audio_input(1)
        assert loop.time() - started < 3


class TestMotionDetectFallback:
    """Older cameras have no DetectVersion; the probe must not be fatal."""

    async def test_error_body_on_the_probe_falls_back_to_the_legacy_api(self):
        """Since 0.4.0 a 200 with an "Error" body raises, which silently killed
        this fallback: motion detection stopped being settable on any camera
        that rejects the V3.0 form."""
        client, session = make_client(
            [
                ("DetectVersion=V3.0", FakeResponse(200, "Error")),
                ("MotionDetect", FakeResponse(200, "OK")),
            ]
        )
        result = await client.enable_motion_detection(0, True)
        assert "OK" in result
        assert session.count("DetectVersion=V3.0") == 1
        assert len(session.calls) == 2, "the legacy URL must still be tried"

    async def test_error_status_on_the_probe_also_falls_back(self):
        client, session = make_client(
            [
                ("DetectVersion=V3.0", FakeResponse(501, "Error\nNot Implemented!")),
                ("MotionDetect", FakeResponse(200, "OK")),
            ]
        )
        assert "OK" in await client.enable_motion_detection(0, True)
        assert len(session.calls) == 2

    async def test_success_on_the_probe_skips_the_legacy_call(self):
        client, session = make_client([("DetectVersion=V3.0", FakeResponse(200, "OK"))])
        assert "OK" in await client.enable_motion_detection(0, True)
        assert len(session.calls) == 1, "no need for the legacy URL"

    async def test_legacy_call_failing_still_raises(self):
        """Swallowing the probe must not swallow a real failure."""
        client, _ = make_client(
            [
                ("DetectVersion=V3.0", FakeResponse(200, "Error")),
                ("MotionDetect", FakeResponse(200, "Error")),
            ]
        )
        with pytest.raises(DahuaResponseError):
            await client.enable_motion_detection(0, True)
