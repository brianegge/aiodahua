"""Parsers for Dahua CGI responses.

Dahua CGI endpoints return flat ``key=value`` text. Structured data is encoded
by convention in the key -- ``list.info[0].Detail[1].TotalBytes`` -- so these
helpers turn the flat text back into something usable.
"""

from __future__ import annotations

import re

__all__ = [
    "RECORD_TYPES",
    "format_bytes",
    "is_error_response",
    "is_not_supported_response",
    "parse_kv",
    "parse_log_entries",
    "parse_media_files",
    "parse_storage_info",
]

# Dahua marks each recording filename with its trigger.
RECORD_TYPES = {"R": "regular", "M": "motion", "A": "alarm", "I": "intelligent"}

_DEVICE_RE = re.compile(r"list\.info\[(\d+)\]\.(.+)")
_DETAIL_RE = re.compile(r"Detail\[(\d+)\]\.(.+)")
_ITEM_RE = re.compile(r"items\[(\d+)\]\.(.+)")


def is_not_supported_response(text: str) -> bool:
    """True when the device answered with its "endpoint missing" error.

    Dahua returns ``Error\\nBad Request!`` (HTTP 400) for endpoints the
    firmware does not implement, which is indistinguishable from a malformed
    request without this check.

    Newer builds say ``Error\\nNot Implemented!`` with HTTP 501 instead --
    confirmed on an IPC-B54IR-ASE-S3 (3.142.15OG000.0.R) and an
    IPC-Color4K-T (3.000.0000000.20.R), where the older cameras on the same
    network answer 400 for the very same endpoint. Both forms mean the same
    thing to a caller.
    """
    stripped = text.strip().lower()
    return stripped.startswith("error") and (
        "bad request" in stripped or "not implemented" in stripped
    )


def is_error_response(text: str) -> bool:
    """True when the body is an error report rather than ``key=value`` data.

    Some failures come back with HTTP 200 and a body like
    ``Error: Error -1 getting param in name=Lighting[0][0]`` (an LTN6416
    recorder, asked for a config section it does not have). That parses into a
    perfectly plausible-looking dict -- key ``Error: Error -1 getting param in
    name``, value ``Lighting[0][0]`` -- so without this check the caller is
    handed junk and told it succeeded.

    Real data never trips this: every genuine line starts with its key, such as
    ``table.`` or ``status.``.
    """
    return text.strip().lower().startswith("error")


def parse_kv(text: str, strip_prefix: bool = False) -> dict[str, str]:
    """Parse ``key=value`` response text into a flat dict.

    Keys are returned exactly as the device sent them. ``configManager.cgi``
    prefixes config reads with ``table.``, and that prefix is *kept* by default:
    silently renaming keys would break callers that index the literal response,
    and it makes round-tripping a value back through setConfig error-prone.

    Args:
        text: The raw response body.
        strip_prefix: Drop a leading ``table.`` or ``status.`` from each key.
    """
    result: dict[str, str] = {}
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if strip_prefix:
                for prefix in ("table.", "status."):
                    if key.startswith(prefix):
                        key = key[len(prefix) :]
                        break
            result[key] = value
        else:
            result[line] = line
    return result


def format_bytes(num: float) -> str:
    """Render a byte count as a short human-readable string (decimal units)."""
    value = float(num)
    if abs(value) < 1000:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1000
        if abs(value) < 1000 or unit == "PB":
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PB"


def parse_storage_info(text: str) -> list[dict]:
    """Parse ``storageDevice.cgi?action=getDeviceAllInfo`` into device entries.

    Returns one entry per physical disk with its partitions rolled up.

    A recorder reports ``used == total`` on a perfectly healthy disk, because
    the firmware pre-allocates the whole drive into fixed-size blocks when it
    formats. Entries in that state carry a ``note`` saying so -- it is very
    easily mistaken for a full disk.
    """
    raw = parse_kv(text)
    devices: dict[int, dict] = {}

    for key, value in raw.items():
        match = _DEVICE_RE.match(key)
        if not match:
            continue
        index, rest = int(match.group(1)), match.group(2)
        device = devices.setdefault(index, {"partitions": {}})

        detail = _DETAIL_RE.match(rest)
        if detail:
            part = device["partitions"].setdefault(int(detail.group(1)), {})
            field = detail.group(2)
            if field in ("TotalBytes", "UsedBytes"):
                try:
                    part[field] = int(float(value))
                except ValueError:
                    part[field] = 0
            elif field == "IsError":
                part["is_error"] = value.strip().lower() == "true"
            elif field == "Path":
                part["path"] = value
            else:
                part[field.lower()] = value
        elif rest == "Name":
            device["name"] = value
        elif rest == "State":
            device["state"] = value
        else:
            device[rest.lower()] = value

    out = []
    for index in sorted(devices):
        device = devices[index]
        parts = [device["partitions"][i] for i in sorted(device["partitions"])]
        total = sum(p.get("TotalBytes", 0) for p in parts)
        used = sum(p.get("UsedBytes", 0) for p in parts)
        for part in parts:
            part["total_bytes"] = part.pop("TotalBytes", 0)
            part["used_bytes"] = part.pop("UsedBytes", 0)
            part["total_human"] = format_bytes(part["total_bytes"])

        entry = {
            "name": device.get("name", f"device{index}"),
            "state": device.get("state"),
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": max(total - used, 0),
            "total_human": format_bytes(total),
            "free_human": format_bytes(max(total - used, 0)),
            "healthy": device.get("state") == "Success"
            and not any(p.get("is_error") for p in parts),
            "partition_errors": [p.get("path") for p in parts if p.get("is_error")],
            "partitions": parts,
        }
        if total and used >= total:
            entry["note"] = (
                "used == total is normal on a Dahua recorder: the firmware "
                "pre-allocates the whole disk into fixed-size blocks when it "
                "formats, so a freshly formatted drive also reports 100% used. "
                "It does not mean the disk is full. Use find_recordings() to "
                "confirm footage is actually being written."
            )
        out.append(entry)
    return out


def parse_media_files(text: str) -> tuple[int, list[dict]]:
    """Parse a ``mediaFileFind.cgi?action=findNextFile`` response.

    Returns ``(found, files)`` where each file has ``path``, ``start_time``,
    ``end_time`` and a decoded ``record_type``.
    """
    raw = parse_kv(text)
    found = 0
    files: dict[int, dict] = {}

    for key, value in raw.items():
        if key == "found":
            try:
                found = int(value)
            except ValueError:
                found = 0
            continue
        match = _ITEM_RE.match(key)
        if not match:
            continue
        entry = files.setdefault(int(match.group(1)), {})
        field = match.group(2)
        if field == "FilePath":
            entry["path"] = value
        elif field == "StartTime":
            entry["start_time"] = value
        elif field == "EndTime":
            entry["end_time"] = value
        elif field == "Length":
            try:
                entry["size_bytes"] = int(value)
            except ValueError:
                entry["size_bytes"] = 0
        else:
            entry[field[0].lower() + field[1:]] = value

    ordered = [files[i] for i in sorted(files)]
    for entry in ordered:
        path = entry.get("path", "")
        entry["record_type"] = next(
            (name for code, name in RECORD_TYPES.items() if f"[{code}]" in path),
            "unknown",
        )
    return found, ordered


def parse_log_entries(text: str) -> list[dict[str, str]]:
    """Parse a ``log.cgi?action=doFind`` response into one dict per entry.

    The response interleaves indexed fields with bare continuation lines::

        found=2
        items[0].Time=2026-09-13 01:38:20
        items[0].Detail=Event Type:IPC Offline Alarm
        Channel:10
        Start Time:2026-09-13 01:38:20
        items[1].Time=...

    A flat ``key=value`` parse turns those continuation lines into nonsense
    top-level keys such as ``{"Channel:10": "Channel:10"}``, losing which entry
    they belonged to. Here they are appended to the preceding entry's
    ``detail`` instead, and ``detail_fields`` holds them split into pairs where
    they look like ``Key:Value``.
    """
    entries: dict[int, dict[str, str]] = {}
    order: list[int] = []
    current: int | None = None

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("found="):
            continue

        match = _ITEM_RE.match(line.split("=", 1)[0]) if "=" in line else None
        if match:
            index = int(match.group(1))
            field = match.group(2)
            value = line.split("=", 1)[1]
            if index not in entries:
                entries[index] = {"detail_fields": {}}
                order.append(index)
            current = index
            entries[index][field[0].lower() + field[1:]] = value
            continue

        # Continuation of the previous entry's Detail.
        if current is None:
            continue
        entry = entries[current]
        entry["detail"] = (entry.get("detail", "") + "\n" + line).strip()
        if ":" in line:
            key, _, value = line.partition(":")
            entry["detail_fields"][key.strip()] = value.strip()

    return [entries[i] for i in order]
