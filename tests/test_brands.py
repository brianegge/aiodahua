"""Brand identification tests.

The Amcrest strings below are real: captured from an NV4116-HS, NV5232 and
NV4432E-HS recorder and IP8M/IP5M cameras on a live network.
"""

from __future__ import annotations

import pytest

from aiodahua import Brand
from aiodahua import extract_oem_code
from aiodahua import identify_brand


class TestExtractOemCode:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("4.000.00AC000.0,build:2020-05-21", "AC"),
            ("2.622.00AC000.0.R,build:2020-10-22", "AC"),
            ("2.800.00AC000.0.R,build:2019-11-13", "AC"),
            ("3.140.0000000.0,build:2021-02-01", None),
            ("", None),
            (None, None),
            ("not a version string", None),
        ],
    )
    def test_extraction(self, version, expected):
        assert extract_oem_code(version) == expected

    def test_lower_case_code_is_normalised(self):
        assert extract_oem_code("4.000.00ac000.0") == "AC"


class TestIdentifyAmcrest:
    """Every string here was observed on real hardware."""

    def test_nvr_reports_abbreviated_vendor(self):
        # NV4116-HS, NV5232 and NV4432E-HS all answer "AC", not "Amcrest".
        match = identify_brand(
            vendor="AC",
            version="4.000.00AC000.0,build:2020-05-21",
            serial="AMR014207899855347",
        )
        assert match.brand is Brand.AMCREST
        assert match.matched_on == ("oem_code", "serial", "vendor")
        assert match.is_confident

    def test_camera_reports_full_vendor(self):
        match = identify_brand(
            vendor="Amcrest",
            version="2.622.00AC000.0.R,build:2020-10-22",
            serial="AMC04142ED468DC93B",
        )
        assert match.brand is Brand.AMCREST
        assert match.oem_code == "AC"

    def test_identified_from_version_alone(self):
        match = identify_brand(version="4.000.00AC000.0,build:2020-05-21")
        assert match.brand is Brand.AMCREST
        assert match.matched_on == ("oem_code",)
        assert not match.is_confident

    def test_identified_from_serial_alone(self):
        assert identify_brand(serial="AMC0606057FFF57044").brand is Brand.AMCREST

    def test_profile_is_hardware_verified(self):
        assert identify_brand(vendor="AC").profile.hardware_verified is True


class TestIdentifyOtherBrands:
    def test_dahua(self):
        match = identify_brand(vendor="Dahua", version="4.000.00DH000.0")
        assert match.brand is Brand.DAHUA

    def test_lorex_prefers_backchannel(self):
        """Lorex firmware is widely reported to reset audio.cgi."""
        match = identify_brand(vendor="Lorex")
        assert match.brand is Brand.LOREX
        assert match.profile.prefers_audio_backchannel is True

    def test_amcrest_does_not_prefer_backchannel(self):
        assert identify_brand(vendor="AC").profile.prefers_audio_backchannel is False

    def test_unverified_profiles_are_flagged(self):
        """Brands we have not confirmed on hardware must say so."""
        assert identify_brand(vendor="Lorex").profile.hardware_verified is False


class TestIdentifyUnknown:
    def test_never_raises_on_garbage(self):
        match = identify_brand(vendor="Acme", version="???", serial="XYZ")
        assert match.brand is Brand.UNKNOWN

    def test_no_signals_at_all(self):
        match = identify_brand()
        assert match.brand is Brand.UNKNOWN
        assert match.matched_on == ()

    def test_raw_strings_are_preserved_for_debugging(self):
        match = identify_brand(vendor="Acme", version="9.9.99ZZ000.0")
        assert match.raw_vendor == "Acme"
        assert match.oem_code == "ZZ"

    def test_str_is_display_name(self):
        assert str(identify_brand(vendor="AC")) == "Amcrest"


class TestConflictingSignals:
    def test_more_agreement_wins(self):
        """A rebranded unit flashed with Dahua firmware: OEM code should win."""
        match = identify_brand(
            vendor="Dahua", version="4.000.00DH000.0", serial="AMC123"
        )
        assert match.brand is Brand.DAHUA
        assert set(match.matched_on) == {"vendor", "oem_code"}

    def test_single_conflicting_signals_are_deterministic(self):
        first = identify_brand(vendor="Lorex", serial="AMC123")
        second = identify_brand(vendor="Lorex", serial="AMC123")
        assert first.brand is second.brand
