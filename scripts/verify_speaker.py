#!/usr/bin/env python3
"""Verify speaker playback against a real camera.

The two speaker paths -- ``audio.cgi`` multipart and the RTSP ONVIF
backchannel -- are the only parts of this library that have never been
exercised against hardware, because the devices it was developed on have
microphones but no speakers. This script closes that gap.

**It makes audible noise.** Point it at a camera where a short beep is not
going to alarm anybody.

Usage::

    python scripts/verify_speaker.py 192.168.1.50 admin secret
    python scripts/verify_speaker.py 192.168.1.50 admin secret --path backchannel
    python scripts/verify_speaker.py 192.168.1.50 admin secret --file chime.mp3

Requires ``ffmpeg`` on PATH unless ``--file`` is already 8 kHz mono AAC/ADTS.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiodahua import DahuaClient
from aiodahua import DahuaError
from aiodahua import DahuaNotSupportedError

OK, BAD, INFO = "PASS", "FAIL", "  ->"


def _make_tone(seconds: float) -> bytes:
    """Render a short beep as 8 kHz mono AAC in an ADTS container."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:duration={seconds}",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ar",
        "8000",
        "-ac",
        "1",
        "-f",
        "adts",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:400])
    return result.stdout


def _convert(path: Path) -> bytes:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ar",
        "8000",
        "-ac",
        "1",
        "-f",
        "adts",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:400])
    return result.stdout


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("--channel", type=int, default=0, help="0-based channel")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--file", type=Path, help="Audio file instead of a beep")
    parser.add_argument(
        "--path",
        choices=("cgi", "backchannel", "both"),
        default="both",
        help="Which speaker transport to exercise",
    )
    args = parser.parse_args()

    audio = _convert(args.file) if args.file else _make_tone(args.seconds)
    duration = args.seconds if not args.file else 0
    print(f"{INFO} audio payload: {len(audio)} bytes of 8 kHz mono AAC/ADTS\n")

    failures = 0
    async with DahuaClient(args.host, args.username, args.password) as dev:
        brand = await dev.async_identify()
        print(f"{INFO} device : {await dev.async_get_device_type()}")
        print(
            f"{INFO} brand  : {brand} (matched on {', '.join(brand.matched_on) or 'nothing'})"
        )
        if brand.profile.prefers_audio_backchannel:
            print(
                f"{INFO} note   : this brand is known to reset audio.cgi;"
                " the backchannel is the expected path"
            )

        try:
            caps = await dev.async_get_audio_capabilities()
            print(f"{INFO} audio out caps: {caps}")
        except DahuaNotSupportedError:
            print(
                f"{INFO} audio out caps: endpoint absent"
                " (not conclusive -- some speaker cameras lack it)"
            )

        try:
            enabled = await dev.async_get_audio_encode_enabled(args.channel)
            print(f"{INFO} audio encode enabled: {enabled}")
            if not enabled:
                print(f"{INFO} enabling audio encode (needed for the backchannel)")
                await dev.async_set_audio_encode_enabled(args.channel, True)
        except DahuaError as err:
            print(f"{INFO} audio encode state unknown: {err}")

        print()
        if args.path in ("cgi", "both"):
            print("audio.cgi multipart ... listen for a beep")
            try:
                await dev.async_post_audio(
                    audio, args.channel, encoding="AAC", duration=duration
                )
                print(f"  {OK} no error from audio.cgi")
            except Exception as err:
                failures += 1
                print(f"  {BAD} {type(err).__name__}: {err}")

        if args.path in ("backchannel", "both"):
            print("\nRTSP ONVIF backchannel ... listen for a beep")
            try:
                await dev.async_post_audio_backchannel(
                    audio, args.channel, duration=duration
                )
                print(f"  {OK} no error from the backchannel")
            except Exception as err:
                failures += 1
                print(f"  {BAD} {type(err).__name__}: {err}")

    print(
        "\nNeither path can confirm the speaker actually sounded -- the camera "
        "accepts the stream either way. Trust your ears, not the exit code."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
