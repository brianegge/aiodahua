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

| Signal | Group | Example | Notes |
|---|---|---|---|
| Firmware OEM code | firmware | `4.000.00**AC**000.0` | The two letters after `00`; absent on generic builds |
| `getVendor` | firmware | `AC`, `Amcrest`, `Dahua`, `Lorex`, `General` | Weaker: often left at the factory default |
| Serial prefix | hardware | `AMC…`, `AMR…`, `ND…` | Amcrest cameras / recorders, Lorex. Survives a reflash |

`brand` reports the **firmware**, because the firmware is what decides which
endpoints exist and how they misbehave. The serial identifies the metal, which
is not always the same story — this hardware gets cross-flashed constantly:

```python
match = identify_brand(
    vendor="Dahua", version="4.001.0000005.1", serial="AMR013C3556656F6E1"
)
match.brand  # Brand.DAHUA -- generic Dahua firmware
match.hardware_brand  # Brand.AMCREST -- an NV4108E-HS underneath
match.is_cross_flashed  # True
```

That is a real device: an Amcrest recorder reflashed with Dahua firmware. It
answers `getVendor=Dahua`, carries no OEM code, and talks to Dahua's own
easy4ip P2P service, while its `AMR` serial and `NV4108E-HS` model name are
pure Amcrest. Where the firmware gives nothing away at all, the serial decides.

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

Profiles carry a `hardware_verified` flag. Amcrest and Lorex are `True`: those
strings came off real NV4116-HS / NV5232 / NV4432E-HS / NV4108E-HS recorders
and IP8M/IP5M cameras, and off E891AB cameras and an N841A8 recorder. Dahua and
EmpireTech are from community reports — accurate as far as we know, but say so
honestly. **PRs adding verified brands are very welcome**; `PROFILES` in
`brands.py` is a plain dict, and `scripts/interview.py` collects everything a
new entry needs.

See [docs/hardware.md](docs/hardware.md) for the devices this library has
actually been run against, and what their firmware does and does not
implement.

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
`upgrader.cgi`. Newer firmware says `Not Implemented` with HTTP 501 instead: an
IPC-B54IR-ASE-S3 on 2024 firmware answers 501 where the 2019 camera beside it
answers 400, for the same endpoint. Both raise `DahuaNotSupportedError`.

**Some failures arrive with HTTP 200.** An LTN6416 asked for a config section
it does not have answers `Error: Error -1 getting param in name=Lighting[0][0]`
— which parses into a perfectly plausible-looking dict. That raises
`DahuaResponseError` rather than handing you junk.

**Asking a Lorex for `audio.cgi` reboots it.** One GET takes an E891AB off the
network, HTTP and RTSP both, for ~105 seconds; the camera's own log records
`Abort` and then `Start up / Reboot Mark: Abort`. `async_get_audio_input()` and
`async_post_audio()` both raise `DahuaUnsafeOperationError` on brands whose
profile records this, rather than sending the request — use
`async_post_audio_backchannel()` there, which is the path the profile prefers
anyway. Pass `force=True` if you own the device and accept the reboot.

**`find_recordings` and `audio.cgi` channels are 1-based**, unlike the
`Encode[n]` config sections. `find_recordings` rejects channel `0` outright;
`audio.cgi` answers `401 stale=TRUE` and keeps answering it for every retry,
which reads as a hang.

**Devices that speak HTTPS present a self-signed certificate**, and some
redirect port 80 to it — an NV4108E-HS answers `302` to `https://<host>:443/`,
so a request addressed to plain HTTP fails in the TLS handshake. Pass
`verify_ssl=False`:

```python
DahuaClient("192.168.4.4", "admin", "secret", port=443, verify_ssl=False)
```

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
> testing covered identity, config, storage, recordings, snapshots, logs and
> RTSP against Amcrest hardware. **The two speaker paths have not been run
> against a speaker** — the devices here have microphones but no audio output.

To close that gap on hardware that does have a speaker:

```bash
python scripts/verify_speaker.py 192.168.1.50 admin secret
python scripts/verify_speaker.py 192.168.1.50 admin secret --path backchannel
```

It reports the brand, whether audio encoding is on, and exercises
`audio.cgi` and the RTSP backchannel in turn. **It makes audible noise.**
Note that neither transport reports whether the speaker actually sounded —
the camera accepts the stream either way — so judge by ear, not exit code.

## Interviewing a device

`scripts/interview.py` runs every read-only capability probe this library
knows about and prints what the firmware supports, plus a Markdown row for
[docs/hardware.md](docs/hardware.md):

```bash
python scripts/interview.py 192.168.1.50 admin secret --json my-camera.json
```

It covers identity, video codecs, stream counts, SNMP, storage, recordings,
PTZ, motorised lens, white light and siren, smart motion, IVS, audio and the
event stream. Secrets are redacted: serials are truncated to the prefix brand
identification uses, and SNMP community strings are never printed.

## Credits

The digest-auth implementation and a good deal of protocol knowledge come from
[rroller/dahua](https://github.com/rroller/dahua) (MIT), the Dahua Home
Assistant integration. The config-encoding and storage/recording work came out
of [dahua-mcp](https://github.com/brianegge/dahua-mcp).

## License

MIT
