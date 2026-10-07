"""Exposure: reading it, and capping how slow the shutter may go.

A camera that is allowed a 1/30 s shutter will take one whenever the scene is
dim -- which, indoors, is always -- and anything moving through the frame
smears. Capping the slowest shutter trades that blur for gain (noise).

The firmware keeps exposure in ``VideoInExposure[channel][profile]``, one
entry per day/night profile. Older builds also expose the same settings as
``VideoInOptions[channel].Exposure*`` (top level, ``NightOptions`` and
``NormalOptions``). On an Amcrest IP5M-T1179E the two are one store: writing
either table changes what the other reads back, in both directions.
``VideoInExposure`` is the one used here.

``Value1``/``Value2`` are the shutter range in milliseconds, fastest first.
Mode ``4`` makes the camera honour that range while still choosing exposure
and gain automatically; in mode ``0`` the range is ignored. Those are the only
two modes confirmed on hardware, so they are the only two named here.
"""

from __future__ import annotations

import re
from fractions import Fraction

from .exceptions import DahuaValueError

#: Profile index -> name, as for ``Lighting[channel][profile]``.
EXPOSURE_PROFILES = {0: "day", 1: "night", 2: "normal"}

#: Exposure modes confirmed on hardware.
EXPOSURE_AUTO = 0
EXPOSURE_SHUTTER_RANGE = 4
EXPOSURE_MODES = {EXPOSURE_AUTO: "auto", EXPOSURE_SHUTTER_RANGE: "shutter range"}

#: The fastest shutter written when only the slowest is given.
DEFAULT_FASTEST_MS = 0.1

_KEY_RE = re.compile(r"(?:table\.)?VideoInExposure\[(\d+)\]\[(\d+)\]\.(\w+)$")


def parse_shutter(value: str | float) -> float:
    """A shutter speed in milliseconds, from ``"1/60"`` or a number of ms.

    >>> round(parse_shutter("1/60"), 2)
    16.67
    >>> parse_shutter(8.33)
    8.33

    Raises:
        DahuaValueError: Not a positive fraction of a second or ms value.
    """
    try:
        if isinstance(value, str) and "/" in value:
            ms = float(Fraction(value.strip())) * 1000
        else:
            ms = float(value)
    except (ValueError, ZeroDivisionError) as err:
        raise DahuaValueError(f"not a shutter speed: {value!r}") from err
    if ms <= 0:
        raise DahuaValueError(f"shutter speed must be positive: {value!r}")
    return ms


def _as_fraction(ms: float) -> str:
    """``16.67`` -> ``"1/60"``: how a shutter speed is usually written."""
    return f"1/{round(1000 / ms)}" if ms > 0 else "?"


def parse_exposure(raw: dict[str, str], channel: int = 0) -> list[dict]:
    """Exposure settings per profile, from a ``VideoInExposure`` getConfig.

    Returns one entry per profile, in profile order, with the shutter range
    both in ms and as the usual "1/N" fraction. The range is reported even in
    auto mode, where the camera ignores it -- ``shutter_range_applies`` says
    which.
    """
    profiles: dict[int, dict[str, str]] = {}
    for key, value in raw.items():
        match = _KEY_RE.match(key)
        if match and int(match.group(1)) == channel:
            profiles.setdefault(int(match.group(2)), {})[match.group(3)] = value

    result = []
    for index in sorted(profiles):
        fields = profiles[index]
        mode = int(fields.get("Mode", "0"))
        fastest = float(fields.get("Value1", "0"))
        slowest = float(fields.get("Value2", "0"))
        result.append(
            {
                "profile": index,
                "name": EXPOSURE_PROFILES.get(index, str(index)),
                "mode": mode,
                "mode_name": EXPOSURE_MODES.get(mode, f"unknown ({mode})"),
                "shutter_range_applies": mode == EXPOSURE_SHUTTER_RANGE,
                "fastest_ms": fastest,
                "slowest_ms": slowest,
                "slowest": _as_fraction(slowest),
                "gain_min": int(fields.get("GainMin", "0")),
                "gain_max": int(fields.get("GainMax", "0")),
            }
        )
    return result


def _profiles(profiles) -> list[int]:
    chosen = sorted(set(EXPOSURE_PROFILES if profiles is None else profiles))
    unknown = [p for p in chosen if p not in EXPOSURE_PROFILES]
    if unknown:
        raise DahuaValueError(f"unknown exposure profile(s): {unknown}")
    return chosen


def shutter_range_config(
    slowest: str | float,
    fastest: str | float = DEFAULT_FASTEST_MS,
    channel: int = 0,
    profiles=None,
) -> dict[str, str]:
    """The ``setConfig`` pairs that cap the shutter at ``slowest``.

    Args:
        slowest: Slowest shutter allowed, ``"1/60"`` or ms.
        fastest: Fastest shutter allowed (default 0.1 ms).
        channel: Video input channel, 0-based.
        profiles: Profile indexes to change (default all: day, night, normal).

    Raises:
        DahuaValueError: Bad speeds, ``fastest`` slower than ``slowest``, or
            an unknown profile. Nothing should be sent in that case.
    """
    slow_ms, fast_ms = parse_shutter(slowest), parse_shutter(fastest)
    if fast_ms > slow_ms:
        raise DahuaValueError(
            f"fastest shutter ({fast_ms:g} ms) is slower than slowest ({slow_ms:g} ms)"
        )
    params = {}
    for profile in _profiles(profiles):
        prefix = f"VideoInExposure[{channel}][{profile}]"
        params[f"{prefix}.Mode"] = str(EXPOSURE_SHUTTER_RANGE)
        params[f"{prefix}.Value1"] = f"{fast_ms:.2f}"
        params[f"{prefix}.Value2"] = f"{slow_ms:.2f}"
    return params


def auto_exposure_config(channel: int = 0, profiles=None) -> dict[str, str]:
    """The ``setConfig`` pairs that hand exposure back to the camera.

    Only the mode is written. The shutter range stays as it was, and is
    ignored in auto mode.
    """
    return {
        f"VideoInExposure[{channel}][{profile}].Mode": str(EXPOSURE_AUTO)
        for profile in _profiles(profiles)
    }
