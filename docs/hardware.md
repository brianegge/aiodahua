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
| NV4108E-HS | dahua (amcrest hardware) | 4.001.0000005.1 | H.264, H.265, MJPG | 2 | yes | yes | yes | no | no | no | yes | yes | no | yes |
| LTN6416 | dahua | 4.001.0000005.4.R | H.264, H.265, MJPG | 2 | yes | yes | yes | no | no | yes | yes | yes | no | yes |

`skipped` means the library refused to send the request because the brand
profile says it reboots the device. `no` on *Storage* and *Recordings* for a
camera means it has no SD card fitted, not that the firmware lacks the
endpoint.

`IP Camera` is what an IPC-HDW2431TP-AS reports as its model. Several rebadged
units answer `getDeviceType` with something that generic; the `updateSerial`
field in `getSystemInfo` is usually more specific.

## What these devices taught the library

**The NV4108E-HS above is cross-flashed**: Amcrest hardware running generic
Dahua firmware. It answers `getVendor=Dahua`, its version `4.001.0000005.1`
carries no OEM code where stock Amcrest firmware would say `AC`, and it is
configured against Dahua's own easy4ip P2P service, while the Lorex recorder
beside it points at `p2p.lorexservices.com`. Its `AMR` serial and model name
are the only Amcrest left. This is why `identify_brand` separates
`firmware_brand` from `hardware_brand` -- it reports `brand=dahua` for this
device, with `hardware_brand=amcrest` and `is_cross_flashed=True`. The table
lists it under the brand whose firmware it runs.

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

Only the plain GET has been seen to do this. Whether closing the response
sooner, or the speaker POST, behaves any better is **untested and will stay
that way** -- each attempt costs a real camera a reboot, and the RTSP
backchannel is the path this brand wants anyway. Both `async_get_audio_input()`
and `async_post_audio()` refuse on the Lorex profile.

**`audio.cgi` channels are 1-based**, unlike the `Encode[n]` config sections.
Channel `0` is answered with `401 stale=TRUE`, which invites a retry that is
answered the same way, forever. Confirmed on the IPC-B54IR-ASE-S3, the
IP5M-T1179E and the LTN6416.

**`mediaFileFind` rejects an implausible time window** the same way it rejects
a missing endpoint. Probing it with a window in the year 2000 makes a recorder
that records perfectly well look like it has no such endpoint, which is why the
interview script asks the device for its own clock first.

**`loadfile.cgi` on the LTN6416 does not hand back the instant it is asked
for.** The main stream is stored in hourly files, and within one the frame
returned for a time drifts from it as the hour goes on: 5 s early at
16:14, 36 s early from 16:22 to 16:31 after a busy stretch of video, 4 s
early by 16:50, then 11 s *late* in the next hour's file -- as though it
seeks by byte offset at a nominal bitrate. Both channels checked, by
different amounts at the same instant. The camera's own clock in the DHAV
frame headers (`ffprobe -f dhav`, `pts_time` is local wall time counted as
UTC) is right, so read it and ask again shifted by the miss; the frames
are also stamped in their OSD if that is on. The same recorder answers a
range whose start is an exact multiple of five minutes (`16:25:00`, not
`16:24:59`) with a correct `Content-Length` and an empty body, every time,
and a range that crosses from one hourly file into the next with a
truncated one; both come through as `DahuaConnectionError` from
:meth:`async_download_clip`. Start a second earlier, and split at the hour.

**The NV4108E-HS seeks worse, and can be wedged.** After a reboot it
handed back 16:26:27 for a request of 16:20:57 -- five and a half minutes
off -- and 15:54:44 for 15:55:10; the shift-and-ask-again loop lands in
two or three rounds. Before the reboot it had spent an afternoon answering
daytime ranges with empty bodies while a 03:42 range came at once, and
after a few of those aborted downloads answered every `loadfile.cgi` with
`Error\r\nBad Request!` while snapshots and config reads carried on.
`magicBox.cgi?action=reboot` cleared it (about 40 s off the network); what
got it there is not understood.

**`VideoInExposure` and `VideoInOptions` are one store on the IP5M-T1179E.**
Writing `VideoInExposure[0][n].Value2` changes `VideoInOptions[0].ExposureValue2`
(and its `NightOptions`/`NormalOptions` copies) on the next read, and the
reverse. `async_set_shutter_range` writes `VideoInExposure` only. In mode `0`
(auto) the camera still reports a shutter range of 33.33/33.33 ms but does
not honour it; mode `4` makes it hold the range, choosing exposure and gain
automatically inside it. Those are the only two modes tried. Capping this
camera at 1/60 s in a dim indoor scene at night moved mean brightness from
83.5 to 82.2 with no change in noise.
