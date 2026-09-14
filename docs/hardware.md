# Tested hardware

Every row here was produced by running `scripts/interview.py` against a
physical device. Nothing in this file is from a datasheet: if a column says
`no`, that device's firmware answered "Bad Request" or "Not Implemented" when
asked.

## Adding your device

```bash
python scripts/interview.py 192.168.1.50 admin secret --json my-camera.json
```

It only reads. It writes no configuration, reboots nothing, and skips
`audio.cgi` on brands whose firmware is known to crash on it. Serial numbers
are truncated to the three-character prefix that brand identification uses, and
SNMP community strings are never printed.

The script ends with a Markdown row ready to paste into the table below --
open a PR with it, and attach the JSON if you want the detail preserved. Rows
for brands not yet in `PROFILES` are especially welcome: that is what promotes
a brand profile to `hardware_verified`.

## Devices

| Model | Brand | Firmware | Video codecs | Extra streams | SNMP | Storage | Recordings | PTZ | Motorised lens | White light / siren | Smart motion | IVS | Speaker | audio.cgi |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E891AB | lorex | 2.622.00LR000.10.R | H.264, H.265, MJPG | 1 | yes | no | no | no | no | no | no | yes | no | skipped |
| IP5M-T1179E | amcrest | 2.800.00AC001.0.R | H.264, H.265, MJPG | 1 | yes | no | no | no | no | no | no | yes | no | yes |
| IP Camera | dahua | 2.800.0000000.24.R | H.264, H.265, MJPG | 1 | yes | no | no | no | no | no | no | yes | no | yes |
| IPC-B54IR-ASE-2.8MM-S3 | dahua | 3.142.15OG000.0.R | H.264, H.265, MJPG | 3 | yes | no | no | no | no | yes | yes | yes | no | yes |
| IPC-T5442TM-AS-6.0mm | dahua | 2.840.15OG00D.0.R | H.264, H.265, MJPG | 2 | yes | no | no | no | no | yes | yes | yes | no | yes |
| IPC-Color4K-T-3.6mm | dahua | 3.000.0000000.20.R | H.264, H.265, MJPG | 2 | yes | no | no | no | no | yes | yes | yes | no | yes |
| N841A8 | lorex | 3.216.00LR035.0 | H.264, H.265, MJPG | 2 | yes | yes | yes | no | no | yes | no | yes | no | skipped |
| NV4108E-HS | amcrest | 4.001.0000005.1 | H.264, H.265, MJPG | 2 | yes | yes | yes | no | no | no | yes | yes | no | yes |
| LTN6416 | dahua | 4.001.0000005.4.R | H.264, H.265, MJPG | 2 | yes | yes | yes | no | no | yes | yes | yes | no | yes |

`skipped` means the library refused to send the request because the brand
profile says it reboots the device. `no` on *Storage* and *Recordings* for a
camera means it has no SD card fitted, not that the firmware lacks the
endpoint.

`IP Camera` is what an IPC-HDW2431TP-AS reports as its model. Several rebadged
units answer `getDeviceType` with something that generic; the `updateSerial`
field in `getSystemInfo` is usually more specific.

## What these devices taught the library

**An Amcrest recorder can answer `getVendor=Dahua`.** The NV4108E-HS above
does, and its version string `4.001.0000005.1` carries no OEM code, so the
serial prefix `AMR` is the only signal that identifies it. Signals are weighted
for exactly this reason -- see `SIGNAL_WEIGHTS` in `brands.py`.

**That same recorder redirects port 80 to a self-signed HTTPS endpoint**, so
plain HTTP fails at the TLS handshake rather than at the redirect. It needs
`DahuaClient(..., port=443, verify_ssl=False)`.

**Missing endpoints come back two different ways.** The 2019--2022 firmware
here answers `400 Error\r\nBad Request!`; the IPC-B54IR-ASE-S3 (2024) and
IPC-Color4K-T answer `501 Error\r\nNot Implemented!` for the identical
endpoint. Both raise `DahuaNotSupportedError`.

**A Lorex E891AB reboots when asked for `audio.cgi`.** One GET takes the camera
off the network -- HTTP and RTSP both -- for around 105 seconds, and its own
log records `Abort` at the moment of the request followed by `Start up` with
`Reboot Mark: Abort`. Three separate cameras, same result; the Dahua and
Amcrest units on the same network take the request without complaint. This is
what `audio_cgi_reboots` on the Lorex profile guards against.

**`audio.cgi` channels are 1-based**, unlike the `Encode[n]` config sections.
Channel `0` is answered with `401 stale=TRUE`, which invites a retry that is
answered the same way, forever. Confirmed on the IPC-B54IR-ASE-S3, the
IP5M-T1179E and the LTN6416.

**`mediaFileFind` rejects an implausible time window** the same way it rejects
a missing endpoint. Probing it with a window in the year 2000 makes a recorder
that records perfectly well look like it has no such endpoint, which is why the
interview script asks the device for its own clock first.
