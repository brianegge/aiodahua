# aiodahua

Async Python client for Dahua IP cameras and NVRs — **and for the white-label
brands built on them**: Amcrest, Lorex, EmpireTech and friends.

They all speak the same CGI API, but they identify themselves differently and
their firmware builds diverge in which endpoints actually exist. `aiodahua`
handles both, and encodes the firmware quirks that otherwise cost you an
afternoon each.

```python
import asyncio
from aiodahua import DahuaClient


async def main():
    async with DahuaClient("192.168.4.4", "admin", "secret") as dev:
        brand = await dev.async_identify()
        print(brand, brand.matched_on)  # Amcrest ('oem_code', 'serial', 'vendor')
        print(await dev.async_get_device_type())  # NV4116-HS

        for disk in await dev.async_get_storage_info():
            print(disk["name"], disk["state"], disk["total_human"], disk["healthy"])

        found, files = await dev.async_find_recordings(
            "2026-09-13 00:00:00", "2026-09-13 23:59:59", channel=1
        )
        print(found, files[0]["record_type"])  # 1 regular


asyncio.run(main())
```

## Install

```bash
pip install aiodahua
```

## Brand identification

Dahua devices don't advertise their brand consistently — Amcrest *recorders*
answer `AC` to `getVendor` while Amcrest *cameras* answer `Amcrest`. So three
independent signals are combined:

| Signal | Example | Notes |
|---|---|---|
| `getVendor` | `AC`, `Amcrest`, `Dahua`, `Lorex` | Inconsistent within a brand |
| Firmware OEM code | `4.000.00**AC**000.0` | The two letters after `00` |
| Serial prefix | `AMC…`, `AMR…` | Amcrest cameras / recorders |

```python
from aiodahua import identify_brand

match = identify_brand(vendor="AC", version="4.000.00AC000.0", serial="AMR0142…")
match.brand  # Brand.AMCREST
match.matched_on  # ('oem_code', 'serial', 'vendor')
match.is_confident  # True — more than one signal agreed
match.profile.prefers_audio_backchannel  # False (True for Lorex)
```

Identification **never fails**. An unrecognised device returns `Brand.UNKNOWN`
with the raw strings preserved, and every other feature keeps working — brand
is used to *predict* quirks, never to gate functionality.

Profiles carry a `hardware_verified` flag. Only Amcrest is `True` today: those
strings came off real NV4116-HS / NV5232 / NV4432E-HS recorders and IP8M/IP5M
cameras. Dahua, Lorex and EmpireTech are from community reports — accurate as
far as we know, but say so honestly. **PRs adding verified brands are very
welcome**; `PROFILES` in `brands.py` is a plain dict.

## Firmware quirks this library handles for you

All confirmed against an Amcrest NV4116-HS, not taken from documentation.

**Clearing a config field silently does nothing.** Sending `Key=` returns `OK`
and leaves the value unchanged — so code that checks for `OK` reports success
while nothing happened. Pass `""` and the library sends a single space, which
the device trims back to `""`.

**`&` cannot appear in a config value.** The firmware percent-decodes the whole
query string *before* splitting it on `&`, so even an encoded `&` terminates
the value: HTTP 400, or silent truncation. There is no encoding that survives,
so `DahuaValueError` is raised rather than writing corrupted data.

```python
await dev.async_set_channel_title(0, "Lot #2")  # fine — encoded
await dev.async_set_channel_title(0, "")  # genuinely clears it
await dev.async_set_channel_title(0, "Bill & Ted")  # DahuaValueError
```

`#`, `+`, `%`, `=`, `/` and `'` all round-trip correctly *once encoded*. Left
raw they corrupt silently: `#` truncates at the fragment, `+` arrives as a
space, `%` breaks decoding.

**`used == total` does not mean the disk is full.** A recorder pre-allocates
the whole drive into fixed-size blocks when it formats, so a brand new disk
reports 100% used. `async_get_storage_info()` attaches a `note` explaining
this. Use `async_find_recordings()` to confirm footage is actually landing.

**Missing endpoints return `Bad Request`, not 404.** Older firmware is missing
a lot — an NV4116-HS on 2020 firmware has no `getSmartInfo` and no
`upgrader.cgi`. Those raise `DahuaNotSupportedError` so you can branch on it.

**`find_recordings` channels are 1-based.** Channel `0` is rejected.

## API

| Method | Purpose |
|---|---|
| `async_identify()` | Brand, with the signals that matched |
| `async_get_device_type()` / `async_get_serial_number()` / `async_get_software_version()` / `async_get_machine_name()` / `async_get_system_info()` | Device identity |
| `async_get_config(name)` / `async_set_config(params)` | Raw config read/write |
| `async_set_machine_name()` / `async_set_channel_title()` | Common config writes |
| `async_get_storage_info()` | Disk capacity, state, health |
| `async_find_recordings()` | Confirm a recorder is recording |
| `async_get_snapshot(channel)` | JPEG bytes |
| `get_rtsp_url(channel, subtype)` | Stream URL (credentials escaped) |
| `async_get_text()` / `async_get()` / `async_get_bytes()` | Escape hatch for any CGI endpoint |

The client does not close a session you pass in, so it is safe to hand it Home
Assistant's shared `aiohttp` session.

## Full device API

v0.2.0 carries across the complete client from the Dahua Home Assistant
integration — 89 methods in total — so the integration can adopt this library
without a behavioural diff. That includes the parts that were the hardest to
get right:

- **Speaker audio out** — `async_post_audio()` (multipart MIME with per-frame
  ADTS delivery, digest priming, explicit `Content-Length` because many cameras
  reject chunked encoding) and `async_post_audio_backchannel()` (RTSP ONVIF
  backchannel over TCP-interleaved RTP, for firmware that resets `audio.cgi` —
  notably Lorex).
- **Event streaming** — `stream_events()` long-poll.
- **PTZ**, lighting v1/v2, floodlight and siren, IVS rules, privacy masking,
  day/night switching, video overlays, coaxial control, disarming linkage,
  record mode, door open.

Keys are returned **exactly as the device sends them**, including the `table.`
prefix on `configManager` reads. Renaming them would quietly break callers that
index the literal response; pass `strip_prefix=True` to `parse_kv` if you want
them trimmed.

> The audio paths and event streaming are carried over unchanged and are
> exercised in production by the HA integration, but this library's own live
> testing covered identity, config, storage, recordings, snapshots and RTSP
> against Amcrest hardware. The speaker paths need a device with a speaker.

## Credits

The digest-auth implementation and a good deal of protocol knowledge come from
[rroller/dahua](https://github.com/rroller/dahua) (MIT), the Dahua Home
Assistant integration. The config-encoding and storage/recording work came out
of [dahua-mcp](https://github.com/brianegge/dahua-mcp).

## License

MIT
