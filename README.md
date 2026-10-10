# Sentinel Live

Live DVRIP view, PTZ, snapshots, recording and motion detection for
Xiongmai / XMeye DVRs, NVRs and IP cameras.

```
./run.sh                                  # http://localhost:8080
SENTINEL_ADMIN_PASSWORD='...' ./run.sh    # create the first admin on boot
```

## Signing in

The first visit asks you to create an account, which becomes the **admin**.
After that, add per-person accounts in **Setup → Users**:

| Role | Can |
|---|---|
| `viewer` | watch live, snapshots |
| `operator` | + PTZ, recording, motion detection |
| `admin` | + add/edit/delete devices, manage users |

Passwords are stored as PBKDF2-HMAC-SHA256 (240k rounds, per-user salt) and
never leave the server. Sessions are HMAC-signed and carried in an HttpOnly
cookie, so the secret is not in URLs where proxies log it.

`SENTINEL_TOKEN` still works as a machine credential for scripts
(`Authorization: Bearer …`) and as the only way in if you never create users.

---

## Read this before anything else: your XML cannot be streamed as-is

The `DeviceManager.xml` you exported contains **XMeye P2P cloud ids**, not
addresses:

```xml
<Device name="Loading Bay" domain="AA0000000AAAAA1" port="37777" ... />
             ^^^^^^^^^^^^^^^^^^^^ this is a cloud id
```

XMeye P2P is a proprietary **UDP** protocol in which every client-to-device
connection is brokered through Xiongmai's "hop" servers. Per SEC Consult's
advisory on the platform, *"all connections between clients and the devices
are established via Xiongmai cloud servers."* Those ids are therefore **not
dialable by any third-party application** — only by XMeye or gDMSS, both of
which ship the vendor's closed SDK and perform the rendezvous for you.

No amount of work on this app changes that. What *is* open is the protocol
underneath: **DVRIP** (also called NETsurveillance or "Sofia"), the TCP
protocol on port **34567** that gDMSS Lite speaks *after* the cloud handshake.
That is what Sentinel implements.

### Getting an address for each device

Any one of these works; use the first that applies.

| Option | What to do |
|---|---|
| **LAN** | Put the phone on the same network as the NVR. Add the device with its IPv4 address (find it in the NVR's network settings, or let a router admin tell you). |
| **Port forward** | On the NVR's router, forward external **TCP 34567** to the NVR's LAN IP. Then use your public IP. |
| **VPN** | WireGuard / Tailscale onto your own network, then use the NVR's private IP. Best option — it does not expose 34567 to the internet. |

Set the host once per device in **Setup → Edit** and it streams. The import
screen accepts an optional host map so all six can be bound at once:

```bash
curl -X POST localhost:8080/api/devices/import \
  -H 'Content-Type: application/json' \
  -d '{"xml":"<DeviceManager .../>",
       "hosts":{"AA0000000AAAAA1":"192.168.1.64:34567"}}'
```

Credentials are imported from the XML, so you do not retype them.

**Until a host is set, those devices import fine and show up in the list —
they just have nothing to dial.** The UI marks them `p2p id` and the stream
tile says so explicitly rather than spinning forever.

---

## What it does

- **Multi-view grid** — 1/2/4/6/9/16 tiles, drag-free ordering, fullscreen, PiP-free.
- **Real video, not screenshots** — H.264/H.265 is demuxed server-side and
  decoded in the browser by **WebCodecs**. Falls back to a 2 s JPEG still
  poll automatically if a device sends a codec the browser will not take.
- **PTZ** — 8-way pan/tilt, zoom and focus, with press-and-hold (Start/Stop).
- **Snapshots** — full-resolution JPEG straight off the device.
- **Recording** — segmented to `data/recordings/<device>/`, in time-based
  files that play in VLC or ffmpeg.
- **Motion detection** — per-device, frame-difference on device JPEG stills.
- **Health** — live fps, bitrate, resolution, staleness, reconnect counts.
- **PWA** — installs to the home screen, works full screen on a phone.

### Keys

`1`-`6` grid layout · `H` health · `R` recordings · `N` new device ·
double-click a tile for fullscreen

---

## How the video path works

The hard part is that a browser cannot play a raw DVRIP byte stream, so:

```
NVR ──DVRIP/TCP:34567──▶ gateway ──WebSocket──▶ browser
                          │                      │
                    Annex-B re-frame        WebCodecs
                    + SPS/PPS extract        VideoDecoder → canvas
```

Three things had to be right, and each was a real bug found by the tests:

1. **Parameter sets.** `avc1.PPCCLL` and the picture size are read out of the
   SPS (`backend/codec.py`) so the browser can configure a decoder that
   matches the actual stream.
2. **Access-unit framing.** `EncodedVideoChunk` boundaries must land on
   pictures. Encoders emit ~10 slices per frame, so splitting on IDR alone
   yields ten bogus "frames" per picture. Boundaries come from
   `first_mb_in_slice == 0` in the slice header instead.
3. **No control traffic on the video socket.** The session keepalive and the
   binary frame reader cannot share a socket — a JSON reply arrives starting
   `0x7B`, which is not a valid frame tag, and the stream dies. During
   preview the heartbeat moves to its own connection.

A viewer joining mid-GOP is held on delta frames until the next IDR instead
of wedging the decoder.

---

## Security

- Passwords are sealed with **AES-256-GCM** (`data/device.key`, mode 0600).
  The key file is the whole secret; back it up with `devices.json`.
- Passwords are never returned by the API — only `has_password: true`.
- The API is closed until someone signs in or a token is presented; only
  `/healthz` and the sign-in endpoints are open.
- Set `SENTINEL_REQUIRE_TOKEN=1` when hosting publicly: the process then
  refuses to boot unless there is a user or a token.
- DVRIP itself sends the password as an MD5-derived digest and is not
  end-to-end encrypted. Keep it on a VPN or LAN where possible.

---

## Development

```bash
./tools/devserver.sh up        # mock NVR :34570 + app :8099
./tools/devserver.sh down
./tools/devserver.sh status
```

`tools/mock_nvr.py` is a real DVRIP **server** — it speaks the wire protocol,
serves a synthetic H.264 clip and answers JPEG snapshots — so the whole
client can be exercised without hardware. `tools/make_testclip.py` renders the
clip with PyAV (a dev dependency only; the app never imports it).

```bash
python3 -m pytest tests/ -q                       # 28 passed
node tools/test_frontend.mjs /tmp/opencode/testclip.h264   # 16 assertions
```

The frontend test is not decoration: it is what caught the streaming-parser
bug where the final unterminated NAL was both emitted and held back, which
silently corrupted every stream whose WebSocket frames did not happen to align
with NAL boundaries.

### Layout

```
backend/dvrip_client.py   protocol: login, keepalive, preview, snapshot, PTZ
backend/codec.py          SPS parse, avcC/hvcC, NAL + access-unit splitting
backend/store.py          XML import, encrypted credential store
backend/hub.py            per-device sessions, reconnect, fan-out, GOP cache
backend/recorder.py       segmented recording + motion detection
backend/app.py            REST + WebSocket
frontend/stream.js        Annex-B -> access units (unit-tested in Node)
frontend/player.js        WebCodecs tile, snapshot fallback
frontend/app.js           UI
android/                  native WebView shell (aapt2/javac/d8, no gradle)
```

---

## Deploy

Three routes, depending on what you can reach — see
**[deploy/REMOTE-ACCESS.md](deploy/REMOTE-ACCESS.md)** for the decision table.

| You can... | Do this |
|---|---|
| Run a script on the office desktop | `deploy/windows/install-and-run.ps1` (or the Linux one), then Tailscale |
| Get into the office router for 5 minutes | Forward TCP 34567, then [DEPLOY.md](DEPLOY.md) |
| Deploy anywhere public | [DEPLOY.md](DEPLOY.md) — Fly.io, fail-closed auth, security checklist |

The gateway only ever needs to run where it can **see the NVR on the LAN**.
It does not need the XMeye cloud, and it does not need a public IP.

---

## Install

### As a PWA
Serve it and open it on the phone: **⋮ → Add to Home screen**. It launches
full screen with no browser chrome.

### As a native APK
```bash
cd android && bash build.sh
# -> android/build/sentinel-live.apk   (~46 KB)
adb install -r android/build/sentinel-live.apk
# or copy the .apk to the phone and open it (allow "install unknown apps")
```
`net.sentinel.live`, minSdk 24, targetSdk 34, signed v2+v3, no permissions
beyond `INTERNET` and `ACCESS_NETWORK_STATE`.

The shell is a full-screen WebView that asks for the gateway address on first
launch and keeps it afterwards (long-press anywhere to change it). It holds
`FLAG_KEEP_SCREEN_ON` — a CCTV wall that blanks after 30 seconds is useless —
and falls back to a native "can't reach the gateway" screen with Retry and
Change gateway instead of a blank WebView.

Rebuild with `VERSION_CODE=2 VERSION_NAME=1.0.1 bash build.sh`. The keystore
(`android/sentinel-release.jks`) is gitignored; an uploaded APK can only be
replaced by one signed with the same key, so back it up.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `unknown frame tag 0x7B224E61` | control reply on the video socket — fixed in current code; update or check `open_preview` is not called twice |
| Tile shows `snapshot` mode | browser rejected the codec; stills keep working meanwhile |
| `login refused: username or password is incorrect` | credentials wrong, or the account lacks stream permission |
| Tile says `no host configured` | imported from P2P export and not yet bound to an address |
| `Cannot connect` | not on the LAN, or TCP 34567 is not forwarded |