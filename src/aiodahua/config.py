"""Encoding for ``configManager.cgi?action=setConfig`` values.

Writing configuration to a Dahua device looks trivial and is not. Two firmware
behaviours will silently corrupt data if you build the query string by hand,
both confirmed against an Amcrest NV4116-HS:

**An empty value is ignored.** ``&Foo.Name=`` returns ``OK`` and changes
nothing. Callers that check for ``OK`` therefore report success while the field
keeps its old value. A single space *is* accepted and the device trims it back
to ``""``, so that is how a field is genuinely cleared.

**``&`` cannot be escaped.** The firmware percent-decodes the whole query
string *before* splitting it on ``&``, so a percent-encoded ``&`` still
terminates the value. The request either returns HTTP 400 or the value is
truncated at the ampersand. There is no encoding that survives this, so
:func:`encode_config_value` raises instead of writing corrupted data.

Everything else -- ``#``, ``+``, ``%``, ``=``, ``/``, ``'``, ``,`` -- round
trips correctly *once percent-encoded*. Left unencoded they corrupt silently:
``#`` truncates at the fragment, ``+`` arrives as a space, ``%`` breaks
decoding entirely.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .exceptions import DahuaValueError

__all__ = ["CONFIG_VALUE_SAFE", "build_config_query", "encode_config_value"]

# Characters Dahua CGI accepts literally inside a value. Everything else is
# percent-encoded so a value cannot break out of the query string.
CONFIG_VALUE_SAFE = ":/-_.,[]()@'"


def encode_config_value(value: Any) -> str:
    """Encode one setConfig value.

    Args:
        value: The value to write. ``""`` and ``None`` both mean "clear this
            field" and are sent as a single space.

    Returns:
        The percent-encoded value, ready to interpolate into a query string.

    Raises:
        DahuaValueError: If the value contains ``&``, which the firmware cannot
            represent.
    """
    text = "" if value is None else str(value)
    if "&" in text:
        raise DahuaValueError(
            "Dahua config values cannot contain '&': the firmware decodes the "
            "query string before splitting on '&', so the value would be "
            f"truncated or rejected. Offending value: {text!r}"
        )
    if text == "":
        text = " "
    return quote(text, safe=CONFIG_VALUE_SAFE)


def build_config_query(params: Mapping[str, Any]) -> str:
    """Build the ``&``-joined ``key=value`` query string for setConfig.

    Keys are passed through literally: Dahua config paths contain ``[]``, ``:``
    and ``.``, which the firmware expects unencoded.
    """
    return "&".join(f"{k}={encode_config_value(v)}" for k, v in params.items())
