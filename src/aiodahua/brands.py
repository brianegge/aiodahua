"""White-label brand identification for Dahua-derived devices.

Dahua manufactures the hardware and firmware behind a long list of rebranded
products -- Amcrest, Lorex, EmpireTech and others. They speak the same CGI API,
but they identify themselves differently and their firmware builds diverge in
which endpoints actually work. This module works out which brand you are
talking to, and what that implies.

Three independent signals are used, in descending order of reliability:

1. ``magicBox.cgi?action=getVendor``. Note this is *not* consistent even within
   one brand: Amcrest NVRs answer ``AC`` while Amcrest cameras answer
   ``Amcrest``. Both are handled.
2. The OEM code embedded in the firmware version string. Dahua versions look
   like ``4.000.00AC000.0`` or ``2.622.00AC000.0.R``; the two letters after
   ``00`` are the OEM code (``AC`` = Amcrest, ``DH`` = Dahua).
3. The serial number prefix, e.g. ``AMC``/``AMR`` for Amcrest.

Identification never fails. An unrecognised device returns
:attr:`Brand.UNKNOWN` with the raw strings preserved, and every client feature
keeps working -- brand is used to *predict* quirks, never to gate functionality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum

__all__ = [
    "PROFILES",
    "Brand",
    "BrandMatch",
    "BrandProfile",
    "extract_oem_code",
    "identify_brand",
]

# e.g. "4.000.00AC000.0,build:2020-05-21" -> "AC"
#      "2.622.00AC000.0.R,build:2020-10-22" -> "AC"
_OEM_CODE_RE = re.compile(r"\d+\.\d+\.\d{2}([A-Za-z]{2})\d")


class Brand(StrEnum):
    """A known white-label brand built on Dahua hardware."""

    DAHUA = "dahua"
    AMCREST = "amcrest"
    LOREX = "lorex"
    EMPIRETECH = "empiretech"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class BrandProfile:
    """What we know about a brand, and what it implies about the device.

    Attributes:
        brand: The brand this profile describes.
        display_name: Human-readable name.
        oem_codes: OEM codes seen in the firmware version string.
        vendor_aliases: Values ``getVendor`` may return, lower-cased.
        serial_prefixes: Known serial-number prefixes, upper-cased.
        prefers_audio_backchannel: True when ``audio.cgi`` is known to be
            unreliable on this brand and the RTSP ONVIF backchannel should be
            tried first for speaker playback.
        hardware_verified: True only where the identifying strings were
            confirmed against a physical device, rather than taken from
            community reports. Treat unverified entries as best-effort.
    """

    brand: Brand
    display_name: str
    oem_codes: tuple[str, ...] = ()
    vendor_aliases: tuple[str, ...] = ()
    serial_prefixes: tuple[str, ...] = ()
    prefers_audio_backchannel: bool = False
    hardware_verified: bool = False


PROFILES: dict[Brand, BrandProfile] = {
    Brand.DAHUA: BrandProfile(
        brand=Brand.DAHUA,
        display_name="Dahua",
        oem_codes=("DH",),
        vendor_aliases=("dahua", "dh", "general"),
        serial_prefixes=(),
    ),
    Brand.AMCREST: BrandProfile(
        brand=Brand.AMCREST,
        display_name="Amcrest",
        oem_codes=("AC",),
        # NVRs report "AC", cameras report "Amcrest". Both confirmed on
        # NV4116-HS / NV5232 / NV4432E-HS and IP8M/IP5M cameras.
        vendor_aliases=("amcrest", "ac"),
        serial_prefixes=("AMC", "AMR"),
        hardware_verified=True,
    ),
    Brand.LOREX: BrandProfile(
        brand=Brand.LOREX,
        display_name="Lorex",
        oem_codes=("LR",),
        vendor_aliases=("lorex", "lr"),
        serial_prefixes=(),
        # Community reports consistently describe audio.cgi resetting the
        # connection on Lorex firmware, which is why the HA integration grew an
        # RTSP backchannel fallback.
        prefers_audio_backchannel=True,
    ),
    Brand.EMPIRETECH: BrandProfile(
        brand=Brand.EMPIRETECH,
        display_name="EmpireTech",
        oem_codes=(),
        vendor_aliases=("empiretech", "empire"),
        serial_prefixes=(),
    ),
    Brand.UNKNOWN: BrandProfile(
        brand=Brand.UNKNOWN,
        display_name="Unknown",
    ),
}


@dataclass(frozen=True)
class BrandMatch:
    """The outcome of brand identification.

    Attributes:
        brand: The identified brand, or :attr:`Brand.UNKNOWN`.
        profile: The matching :class:`BrandProfile`.
        matched_on: Which signals agreed, e.g. ``("vendor", "oem_code")``.
        oem_code: The OEM code parsed from the firmware version, if any.
        raw_vendor: The unmodified ``getVendor`` response, if any.
    """

    brand: Brand
    profile: BrandProfile
    matched_on: tuple[str, ...] = ()
    oem_code: str | None = None
    raw_vendor: str | None = None
    _extra: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_confident(self) -> bool:
        """True when more than one independent signal agreed."""
        return len(self.matched_on) > 1

    def __str__(self) -> str:
        return self.profile.display_name


def extract_oem_code(version: str | None) -> str | None:
    """Pull the two-letter OEM code out of a Dahua firmware version string.

    >>> extract_oem_code("4.000.00AC000.0,build:2020-05-21")
    'AC'
    >>> extract_oem_code("not a version") is None
    True
    """
    if not version:
        return None
    match = _OEM_CODE_RE.search(version)
    return match.group(1).upper() if match else None


def identify_brand(
    vendor: str | None = None,
    version: str | None = None,
    serial: str | None = None,
) -> BrandMatch:
    """Identify the brand of a device from any combination of its strings.

    All arguments are optional; pass whatever you have. Signals are combined
    rather than short-circuited, so ``matched_on`` tells you how much agreement
    there was.

    Args:
        vendor: Response of ``magicBox.cgi?action=getVendor``.
        version: Response of ``magicBox.cgi?action=getSoftwareVersion``.
        serial: Response of ``magicBox.cgi?action=getSerialNo``.

    Returns:
        A :class:`BrandMatch`. Never raises, never returns None.
    """
    oem_code = extract_oem_code(version)
    vendor_norm = (vendor or "").strip().lower()
    serial_norm = (serial or "").strip().upper()

    votes: dict[Brand, set[str]] = {}

    def vote(brand: Brand, signal: str) -> None:
        votes.setdefault(brand, set()).add(signal)

    for brand, profile in PROFILES.items():
        if brand is Brand.UNKNOWN:
            continue
        if vendor_norm and vendor_norm in profile.vendor_aliases:
            vote(brand, "vendor")
        if oem_code and oem_code in profile.oem_codes:
            vote(brand, "oem_code")
        if serial_norm and any(
            serial_norm.startswith(p) for p in profile.serial_prefixes
        ):
            vote(brand, "serial")

    if not votes:
        return BrandMatch(
            brand=Brand.UNKNOWN,
            profile=PROFILES[Brand.UNKNOWN],
            oem_code=oem_code,
            raw_vendor=vendor,
        )

    # Most agreeing signals wins; ties broken by a stable brand order so the
    # result never depends on dict iteration order.
    order = list(PROFILES)
    best = max(votes, key=lambda b: (len(votes[b]), -order.index(b)))
    return BrandMatch(
        brand=best,
        profile=PROFILES[best],
        matched_on=tuple(sorted(votes[best])),
        oem_code=oem_code,
        raw_vendor=vendor,
    )
