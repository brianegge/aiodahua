"""Tests for config encoding and response parsing.

The firmware behaviours asserted here were confirmed against a live Amcrest
NV4116-HS, not inferred from documentation.
"""

from __future__ import annotations

import pytest

from aiodahua import DahuaValueError
from aiodahua import build_config_query
from aiodahua import encode_config_value
from aiodahua import format_bytes
from aiodahua import parse_kv
from aiodahua import parse_media_files
from aiodahua import parse_storage_info
from aiodahua import strip_dhav_preamble
from aiodahua.parsers import is_not_supported_response


class TestEncodeConfigValue:
    def test_empty_becomes_space(self):
        """Firmware ignores a bare 'Key='; a space is how a field is cleared."""
        assert encode_config_value("") == "%20"

    def test_none_becomes_space(self):
        assert encode_config_value(None) == "%20"

    def test_spaces_encoded_colons_preserved(self):
        assert encode_config_value("1 00:00:00-24:00:00") == "1%2000:00:00-24:00:00"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("a#b", "a%23b"), ("a+b", "a%2Bb"), ("a=b", "a%3Db"), ("50%", "50%25")],
    )
    def test_characters_that_need_encoding(self, value, expected):
        """Unencoded these corrupt silently; encoded they round-trip."""
        assert encode_config_value(value) == expected

    def test_ampersand_raises(self):
        """Firmware decodes before splitting on '&', so it cannot be escaped."""
        with pytest.raises(DahuaValueError, match="cannot contain"):
            encode_config_value("Bill & Ted")

    def test_non_string_is_coerced(self):
        assert encode_config_value(15) == "15"


class TestBuildConfigQuery:
    def test_keys_stay_literal(self):
        query = build_config_query({"VideoWidget[0].CustomTitle[0].Text": "Lobby"})
        assert query == "VideoWidget[0].CustomTitle[0].Text=Lobby"

    def test_multiple_pairs(self):
        assert build_config_query({"A": "1", "B": "2"}) == "A=1&B=2"

    def test_nothing_is_sent_when_one_value_is_invalid(self):
        with pytest.raises(DahuaValueError):
            build_config_query({"A": "fine", "B": "a&b"})


class TestParseKv:
    def test_keys_are_device_literal_by_default(self):
        """Renaming keys would break callers that index the raw response."""
        assert parse_kv("table.General.MachineName=CHNVR") == {
            "table.General.MachineName": "CHNVR"
        }

    def test_prefix_stripping_is_opt_in(self):
        assert parse_kv("table.General.MachineName=CHNVR", strip_prefix=True) == {
            "General.MachineName": "CHNVR"
        }

    def test_strips_status_prefix_when_asked(self):
        assert parse_kv("status.x=1", strip_prefix=True) == {"x": "1"}

    def test_keeps_value_containing_equals(self):
        assert parse_kv("a=b=c") == {"a": "b=c"}

    def test_blank_lines_ignored(self):
        assert parse_kv("\n\na=1\n\n") == {"a": "1"}


class TestNotSupportedDetection:
    def test_detects_firmware_error(self):
        assert is_not_supported_response("Error\nBad Request!") is True

    def test_normal_response_is_not_an_error(self):
        assert is_not_supported_response("version=4.000") is False


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (2000324395008, "2.00 TB"),
            (7921284939776, "7.92 TB"),
        ],
    )
    def test_units(self, value, expected):
        assert format_bytes(value) == expected


STORAGE = """list.info[0].Detail[0].TotalBytes=2000324395008.000000
list.info[0].Detail[0].UsedBytes=2000324395008.000000
list.info[0].Detail[0].IsError=false
list.info[0].Detail[0].Path=/dev/sda0
list.info[0].Detail[1].TotalBytes=1920311754752.000000
list.info[0].Detail[1].UsedBytes=1920311754752.000000
list.info[0].Detail[1].IsError=false
list.info[0].Detail[1].Path=/dev/sda1
list.info[0].Name=/dev/sda
list.info[0].State=Success
list.info[0].HealthDataFlag=0"""


class TestParseStorageInfo:
    def test_partitions_rolled_up(self):
        devices = parse_storage_info(STORAGE)
        assert len(devices) == 1
        assert devices[0]["name"] == "/dev/sda"
        assert devices[0]["total_bytes"] == 2000324395008 + 1920311754752
        assert len(devices[0]["partitions"]) == 2

    def test_healthy_when_no_errors(self):
        assert parse_storage_info(STORAGE)[0]["healthy"] is True

    def test_unhealthy_on_partition_error(self):
        bad = STORAGE.replace(
            "list.info[0].Detail[1].IsError=false",
            "list.info[0].Detail[1].IsError=true",
        )
        device = parse_storage_info(bad)[0]
        assert device["healthy"] is False
        assert device["partition_errors"] == ["/dev/sda1"]

    def test_full_disk_carries_preallocation_note(self):
        """A brand new Dahua disk reports 100% used; that must not alarm."""
        assert "pre-allocates" in parse_storage_info(STORAGE)[0]["note"]

    def test_no_note_when_space_free(self):
        partial = STORAGE.replace(
            "list.info[0].Detail[0].UsedBytes=2000324395008.000000",
            "list.info[0].Detail[0].UsedBytes=1000000000000.000000",
        )
        assert "note" not in parse_storage_info(partial)[0]

    def test_empty(self):
        assert parse_storage_info("") == []


MEDIA = """found=2
items[0].Channel=0
items[0].StartTime=2026-09-13 08:20:31
items[0].EndTime=2026-09-13 08:49:43
items[0].FilePath=/mnt/dvr/2026-09-13/000/dav/08/08.20.31-08.49.43[R][0@0][0].dav
items[0].Length=104857600
items[1].StartTime=2026-09-13 00:44:33
items[1].FilePath=/mnt/dvr/2026-09-13/002/dav/00/00.44.33-00.44.57[M][0@0][0].dav"""


class TestParseMediaFiles:
    def test_found_count(self):
        found, _ = parse_media_files(MEDIA)
        assert found == 2

    def test_record_type_decoded(self):
        _, files = parse_media_files(MEDIA)
        assert files[0]["record_type"] == "regular"
        assert files[1]["record_type"] == "motion"

    def test_fields_normalised(self):
        _, files = parse_media_files(MEDIA)
        assert files[0]["start_time"] == "2026-09-13 08:20:31"
        assert files[0]["size_bytes"] == 104857600

    def test_ordering_follows_index(self):
        _, files = parse_media_files(MEDIA)
        assert files[0]["start_time"] > files[1]["start_time"]

    def test_empty_result(self):
        found, files = parse_media_files("found=0")
        assert (found, files) == (0, [])


class TestExceptionCompatibility:
    """Existing aiohttp-based handlers must keep working after migration."""

    def test_connection_error_is_a_client_error(self):
        import aiohttp

        from aiodahua import DahuaConnectionError

        assert issubclass(DahuaConnectionError, aiohttp.ClientError)

    def test_response_error_is_a_client_response_error(self):
        import aiohttp

        from aiodahua import DahuaResponseError

        assert issubclass(DahuaResponseError, aiohttp.ClientResponseError)
        err = DahuaResponseError("boom", status=500, endpoint="x.cgi")
        assert err.status == 500
        assert str(err) == "boom"

    def test_auth_error_carries_401_for_reauth(self):
        from aiodahua import DahuaAuthError

        assert DahuaAuthError("nope").status == 401

    def test_not_supported_is_catchable_as_response_error(self):
        import aiohttp

        from aiodahua import DahuaNotSupportedError

        err = DahuaNotSupportedError("missing")
        assert isinstance(err, aiohttp.ClientResponseError)
        assert err.status == 400


LOG = """found=2
items[0].Time=2026-09-13 01:38:20
items[0].User=default
items[0].Type=IPC Offline Alarm
items[0].Detail=Event Type:IPC Offline Alarm
Channel:10
Start Time:2026-09-13 01:38:20
items[1].Time=2026-09-13 07:33:15
items[1].User=admin
items[1].Type=User logged in.
items[1].Detail=IP Address: 192.168.4.2"""


class TestParseLogEntries:
    def test_one_dict_per_entry(self):
        from aiodahua import parse_log_entries

        entries = parse_log_entries(LOG)
        assert len(entries) == 2
        assert entries[0]["type"] == "IPC Offline Alarm"
        assert entries[1]["user"] == "admin"

    def test_continuation_lines_stay_with_their_entry(self):
        """A flat parse turns these into bogus keys like {'Channel:10': ...}."""
        from aiodahua import parse_log_entries

        first = parse_log_entries(LOG)[0]
        assert "Channel:10" in first["detail"]
        assert first["detail_fields"]["Channel"] == "10"
        assert first["detail_fields"]["Start Time"] == "2026-09-13 01:38:20"

    def test_continuations_do_not_leak_to_the_next_entry(self):
        from aiodahua import parse_log_entries

        second = parse_log_entries(LOG)[1]
        assert "Channel" not in second["detail_fields"]

    def test_empty(self):
        from aiodahua import parse_log_entries

        assert parse_log_entries("found=0") == []


class TestStripDhavPreamble:
    """loadfile.cgi wraps the stream in binary that ffmpeg cannot skip."""

    def test_returns_the_stream_from_the_first_frame_header(self):
        assert strip_dhav_preamble(b"\x00\x01\x02DHAVrest") == b"DHAVrest"

    def test_a_clip_with_no_preamble_is_unchanged(self):
        assert strip_dhav_preamble(b"DHAVrest") == b"DHAVrest"

    def test_only_the_first_header_is_found(self):
        """Later frames must survive; the stream is a run of DHAV records."""
        data = b"pad" + b"DHAV" + b"one" + b"DHAV" + b"two"
        assert strip_dhav_preamble(data) == b"DHAVoneDHAVtwo"

    def test_a_body_with_no_header_is_rejected(self):
        """An error page or an empty range, not a clip."""
        with pytest.raises(ValueError, match="not a clip"):
            strip_dhav_preamble(b"Error\r\nBad Request!")
