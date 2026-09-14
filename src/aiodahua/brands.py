"""White-label brand identification for Dahua-derived devices.

Dahua manufactures the hardware and firmware behind a long list of rebranded
products -- Amcrest, Lorex, EmpireTech and others. They speak the same CGI API,
but they identify themselves differently and their firmware builds diverge in
which endpoints actually work. This module works out which brand you are
talking to, and what that implies.

Three independent signals are used. They are *weighted*, in descending order
of reliability:

1. The serial number prefix, e.g. ``AMC``/``AMR`` for Amcrest, ``ND`` for
   Lorex. The strongest signal: it is baked in at manufacture and no device
   observed so far lies about it.
2. The OEM code embedded in the firmware version string. Dahua versions look
   like ``4.000.00AC000.0`` or ``2.622.00AC000.0.R``; the two letters after
   ``00`` are the OEM code (``AC`` = Amcrest, ``DH`` = Dahua, ``LR`` = Lorex).
   Absent on some builds -- an NV4108E-HS reports ``4.001.0000005.1``.
3. ``magicBox.cgi?action=getVendor``. The weakest signal, and not consistent
   even within one brand: Amcrest NVRs have been seen answering ``AC`` *and*
   plain ``Dahua``, while Amcrest cameras answer ``Amcrest``. A rebranded
   device answering ``Dahua`` or ``General`` is saying almost nothing, so it
   must never outrank a serial prefix.

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
    "SIGNAL_WEIGHTS",
    "Brand",
    "BrandMatch",
    "BrandProfile",
    "extract_oem_code",
    "identify_brand",
]

# How much each signal counts for. A serial prefix outweighs any single other
# signal because it is the only one a rebranded device has never been seen to
# get wrong; getVendor is weakest because "Dahua"/"General" are the factory
# defaults that many rebrands never change.
SIGNAL_WEIGHTS = {"serial": 3, "oem_code": 2, "vendor": 1}

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
        audio_cgi_reboots: True when a plain GET of ``audio.cgi`` has been
            observed to crash and reboot the device. Guarded against rather
            than merely documented -- see
            :meth:`aiodahua.DahuaClient.async_get_audio_input`.
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
    audio_cgi_reboots: bool = False
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
        # Observed on six Lorex units: five E891AB cameras (ND0119...) and an
        # N841A8 recorder (ND0219...). No Dahua-branded device on the same
        # network uses it.
        serial_prefixes=("ND",),
        # Community reports consistently describe audio.cgi resetting the
        # connection on Lorex firmware, which is why the HA integration grew an
        # RTSP backchannel fallback. On E891AB firmware 2.622 it is worse than
        # a reset: see audio_cgi_reboots.
        prefers_audio_backchannel=True,
        # Confirmed on three E891AB cameras running 2.622.00LR000.10.R. A
        # single GET of audio.cgi takes the camera off the network entirely --
        # HTTP and RTSP both -- and its own log records "Abort" at the moment of
        # the request followed by "Start up / Reboot Mark: Abort" ~105s later.
        audio_cgi_reboots=True,
        # vendor "LOREX" and OEM code "LR" both read off those cameras and the
        # N841A8 recorder.
        hardware_verified=True,
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

    def score(brand: Brand) -> int:
        return sum(SIGNAL_WEIGHTS[signal] for signal in votes[brand])

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

    # Highest total weight wins, so a serial prefix beats a lone generic
    # vendor string: an Amcrest NV4108E-HS answers getVendor="Dahua" and has no
    # OEM code in "4.001.0000005.1", leaving serial "AMR..." as the only signal
    # that is actually about the brand. Ties are broken by a stable brand order
    # so the result never depends on dict iteration order.
    order = list(PROFILES)
    best = max(votes, key=lambda b: (score(b), len(votes[b]), -order.index(b)))
    return BrandMatch(
        brand=best,
        profile=PROFILES[best],
        matched_on=tuple(sorted(votes[best])),
        oem_code=oem_code,
        raw_vendor=vendor,
    )
