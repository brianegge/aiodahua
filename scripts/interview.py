#!/usr/bin/env python3
"""Interview a Dahua-derived device and report what its firmware supports.

Run this against your own camera or recorder and paste the output into an
issue: it is how ``docs/hardware.md`` gets filled in, and how brand profiles in
``aiodahua/brands.py`` earn their ``hardware_verified`` flag.

    python scripts/interview.py 192.168.1.50 admin secret
    python scripts/interview.py 192.168.1.50 admin secret --json cam.json
    python scripts/interview.py 192.168.1.50 admin secret --port 443 --insecure

Everything it sends is a read: no configuration is written, nothing is
rebooted, and ``audio.cgi`` is skipped on brands known to crash on it unless
you pass ``--force-audio``.

Secrets are redacted. Serial numbers are truncated to the prefix that brand
identification uses, and SNMP community strings are never printed -- only
whether SNMP is on and which versions are enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from datetime import datetime
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiohttp

from aiodahua import DahuaClient
from aiodahua import DahuaError
from aiodahua import DahuaNotSupportedError
from aiodahua import DahuaUnsafeOperationError
from aiodahua import parse_kv

SUPPORTED, MISSING, ERROR = "yes", "no", "?"


class Interview:
    def __init__(self, client: DahuaClient, force_audio: bool = False) -> None:
        self.client = client
        self.force_audio = force_audio
        self.identity: dict[str, str | None] = {}
        self.capabilities: dict[str, dict] = {}

    async def probe(self, name: str, coro, detail=None) -> None:
        """Run one read and record whether the firmware implements it.

        A missing endpoint answers "Bad Request" (or "Not Implemented" on newer
        builds), never a 404, which is why this goes through the library rather
        than checking status codes by hand.
        """
        try:
            result = await coro
        except DahuaNotSupportedError:
            self.capabilities[name] = {"supported": MISSING, "detail": ""}
        except DahuaUnsafeOperationError as err:
            self.capabilities[name] = {"supported": "skipped", "detail": str(err)}
        except (DahuaError, TimeoutError) as err:
            self.capabilities[name] = {
                "supported": ERROR,
                "detail": f"{type(err).__name__}: {str(err)[:80]}",
            }
        else:
            text = detail(result) if detail else ""
            self.capabilities[name] = {"supported": SUPPORTED, "detail": text}

    async def run(self) -> dict:
        client = self.client
        brand = await client.async_identify()
        serial = await client.async_get_serial_number()
        self.identity = {
            "model": await client.async_get_device_type(),
            "brand": brand.brand.value,
            "brand_signals": ", ".join(brand.matched_on) or "none",
            "vendor": brand.raw_vendor,
            "firmware": await client.async_get_software_version(),
            "hardware": await client.async_get_hardware_version(),
            "oem_code": brand.oem_code,
            # Only the prefix matters for identification, and it is the only
            # part that is not unique to the person running this.
            "serial_prefix": (serial or "")[:3] or None,
        }

        await self.probe(
            "device_class",
            client.async_get_text("magicBox.cgi?action=getDeviceClass"),
            lambda t: parse_kv(t).get("class", ""),
        )
        await self.probe(
            "video_codecs",
            client.async_get_text("encode.cgi?action=getConfigCaps"),
            self._codecs,
        )
        await self.probe(
            "main_stream",
            client.async_get_config("Encode[0].MainFormat[0].Video"),
            self._main_stream,
        )
        await self.probe(
            "extra_streams",
            client.get_max_extra_streams(),
            lambda n: str(n),
        )
        await self.probe(
            "snmp",
            client.async_get_config("SNMP"),
            self._snmp,
        )
        await self.probe(
            "storage",
            client.async_get_storage_info(),
            lambda disks: ", ".join(f"{d['name']} {d['total_human']}" for d in disks),
        )
        await self.probe(
            "smart_info",
            client.async_get_text("storageDevice.cgi?action=getSmartInfo"),
        )
        # The window has to be a plausible one: a recorder that supports
        # mediaFileFind perfectly well answers "Bad Request" -- indistinguishable
        # from a missing endpoint -- when asked about the year 2000. Use the
        # device's own clock, which is often not the same as this machine's.
        start, end = await self._recent_window()
        await self.probe(
            "find_recordings",
            client.async_find_recordings(start, end, channel=1),
            lambda r: f"{r[0]} files in the last 24h",
        )
        await self.probe(
            "snapshot",
            client.async_get_snapshot(1),
            lambda b: f"{len(b)} bytes",
        )
        await self.probe("ptz", client.async_get_ptz_position())
        await self.probe("motorised_lens", client.async_get_zoomfocus_v1())
        await self.probe(
            "coaxial_io",
            client.async_get_coaxial_control_io_status(),
            lambda d: ", ".join(f"{k.split('.')[-1]}={v}" for k, v in d.items()),
        )
        await self.probe("lighting_v1", client.async_get_config_lighting(0, 0))
        await self.probe("lighting_v2", client.async_get_lighting_v2())
        await self.probe("floodlight", client.async_get_light_global_enabled())
        await self.probe("smart_motion", client.async_get_smart_motion_detection())
        await self.probe("ivs_rules", client.async_get_ivs_rules())
        await self.probe("disarming_linkage", client.async_get_disarming_linkage())
        await self.probe("event_notify_config", client.async_get_event_notifications())
        await self.probe("speaker", client.async_get_audio_capabilities())
        await self.probe(
            "audio_encoding",
            client.async_get_audio_encode_enabled(0),
            lambda on: "enabled" if on else "available, off",
        )
        await self.probe(
            "audio_input_probe",
            # 1-based here, unlike the Encode[0] config section above.
            client.async_get_audio_input(1, force=self.force_audio),
            lambda ok: "served" if ok else "refused",
        )
        await self.probe("event_stream", self._event_stream(), lambda n: f"{n} chunks")
        await self.probe("https", self._https(), lambda t: t)

        return {"identity": self.identity, "capabilities": self.capabilities}

    async def _recent_window(self) -> tuple[str, str]:
        fmt = "%Y-%m-%d %H:%M:%S"
        try:
            answer = await self.client.async_get_text(
                "global.cgi?action=getCurrentTime"
            )
            now = datetime.strptime(parse_kv(answer)["result"], fmt)
        except (DahuaError, KeyError, ValueError, TimeoutError):
            now = datetime.now()
        return (now - timedelta(days=1)).strftime(fmt), now.strftime(fmt)

    @staticmethod
    def _codecs(text: str) -> str:
        codecs: set[str] = set()
        for key, value in parse_kv(text).items():
            if key.endswith("Video.CompressionTypes"):
                codecs.update(value.split(","))
        return ", ".join(sorted(codecs))

    @staticmethod
    def _main_stream(config: dict) -> str:
        get = lambda field: config.get(f"table.Encode[0].MainFormat[0].Video.{field}", "?")  # noqa: E731
        return f"{get('Compression')} {get('Width')}x{get('Height')} @{get('FPS')}"

    @staticmethod
    def _snmp(config: dict) -> str:
        """Report SNMP support without ever printing a community string."""
        enabled = config.get("table.SNMP.Enable", "false") == "true"
        versions = [
            name[len("table.SNMP.") : -len("Enable")]
            for name, value in sorted(config.items())
            if name.startswith("table.SNMP.V")
            and name.endswith("Enable")
            and value == "true"
        ]
        port = config.get("table.SNMP.Port", "?")
        state = "enabled" if enabled else "supported, off"
        return f"{state}; versions {','.join(versions) or 'unstated'}; port {port}"

    async def _event_stream(self, seconds: int = 8) -> int:
        chunks = 0

        def on_receive(data: bytes, channel: int) -> None:
            nonlocal chunks
            chunks += 1

        task = asyncio.create_task(self.client.stream_events(on_receive, ["All"], 0))
        await asyncio.sleep(seconds)
        task.cancel()
        # CancelledError is a BaseException, so it needs naming explicitly.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return chunks

    async def _https(self) -> str:
        """Does port 80 redirect to a self-signed HTTPS endpoint?"""
        async with aiohttp.ClientSession() as session, session.get(
            f"http://{self.client.host}/", allow_redirects=False, ssl=False
        ) as response:
            location = response.headers.get("Location", "")
            if response.status in (301, 302, 303, 307, 308) and "https" in location:
                return "port 80 redirects to HTTPS (needs verify_ssl=False)"
            return "plain HTTP"


def render(report: dict) -> str:
    identity, caps = report["identity"], report["capabilities"]
    lines = [f"# {identity['model'] or 'unknown model'}", ""]
    for key, value in identity.items():
        lines.append(f"  {key:<15} {value}")
    lines += ["", "  capability          supported  detail", "  " + "-" * 68]
    for name, result in caps.items():
        lines.append(f"  {name:<20}{result['supported']:<11}{result['detail']}")
    lines += ["", "Markdown row for docs/hardware.md:", "", markdown_row(report)]
    return "\n".join(lines)


def markdown_row(report: dict) -> str:
    identity, caps = report["identity"], report["capabilities"]

    def mark(name: str) -> str:
        return {SUPPORTED: "yes", MISSING: "no", "skipped": "skipped"}.get(
            caps.get(name, {}).get("supported", ERROR), "?"
        )

    codecs = caps.get("video_codecs", {}).get("detail", "") or "?"
    snmp = "yes" if caps.get("snmp", {}).get("supported") == SUPPORTED else mark("snmp")
    return "| {model} | {brand} | {firmware} | {codecs} | {streams} | {snmp} | {storage} | {rec} | {ptz} | {lens} | {coax} | {smd} | {ivs} | {spk} | {audio} |".format(
        model=identity.get("model"),
        brand=identity.get("brand"),
        firmware=(identity.get("firmware") or "").split(",")[0],
        codecs=codecs,
        streams=caps.get("extra_streams", {}).get("detail", "?"),
        snmp=snmp,
        storage=mark("storage"),
        rec=mark("find_recordings"),
        ptz=mark("ptz"),
        lens=mark("motorised_lens"),
        coax=mark("coaxial_io"),
        smd=mark("smart_motion"),
        ivs=mark("ivs_rules"),
        spk=mark("speaker"),
        audio=mark("audio_input_probe"),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host")
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="accept the device's self-signed certificate",
    )
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    parser.add_argument(
        "--force-audio",
        action="store_true",
        help="probe audio.cgi even on brands whose firmware reboots on it",
    )
    args = parser.parse_args()

    async with DahuaClient(
        args.host,
        args.username,
        args.password,
        port=args.port,
        timeout=args.timeout,
        verify_ssl=not args.insecure,
    ) as client:
        report = await Interview(client, force_audio=args.force_audio).run()

    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
