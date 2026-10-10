# Deploying Sentinel Live to a public host

Read [the P2P section of the README](README.md#read-this-before-anything-else-your-xml-cannot-be-streamed-as-is)
first. This document assumes you understand why the cloud IDs in your
`DeviceManager.xml` are useless on their own.

## How this fits together

```
your NVR site            public host                your phone
┌──────────────┐  :34567 ┌────────────────┐   HTTPS   ┌────────┐
│  NVR / DVR   │ ──────► │ Sentinel       │ ◄──────── │  PWA   │
│  (behind     │  open   │ gateway        │   token   │ or APK │
│   router)    │          │ + WebCodecs UI │           └────────┘
└──────────────┘          └────────────────┘
        ▲
   port forward, or a VPN/tunnel back to this site
```

The gateway is the only piece that speaks DVRIP. The browser UI is static and
is served by the same process.

## Step 1 — the NVR site must be reachable on TCP 34567

DVRIP defaults to **TCP 34567** (the reference implementation also lists
UDP 34568). For each device site:

1. Give the NVR a **static DHCP lease** on the local router.
2. Forward **external TCP 34567 → NVR_IP:34567**.
3. Give the site a **static public IP or a DDNS name** (the router usually
   offers DDNS; Cloudflare or DuckDNS are fine otherwise).
4. Verify from outside the site:
   ```bash
   nc -vz your-ddns-name 34567
   ```

### Stop and read this if step 4 fails

A failing port test is almost never a Sentinel problem. In order of likelihood:

- **CGNAT.** Most residential ISPs put you behind carrier-grade NAT, where
  port forwarding cannot work no matter what the router says. Ask the ISP for
  a public IP, or use a tunnel/VPN from the NVR site instead. This is the
  single most common reason this whole plan fails.
- **Double NAT.** Modem + router in series; forward on both, or bridge the
  modem.
- **Firewall / ISP blocks** of uncommon inbound ports.
- **The NVR is not listening** — some models ship with DVRIP disabled. Check
  its network settings page.

If you cannot get a public route to the NVR, no public host will help. Run the
gateway on your desktop behind Tailscale instead; that path needs none of this.

## Step 2 — deploy the gateway

Any Docker-capable host works. Fly.io is the example:

```bash
fly launch --no-deploy --copy-config
fly volumes create sentinel_data --size 1     # persists devices + recordings
fly secrets set SENTINEL_TOKEN=$(openssl rand -hex 32)
fly deploy
```

`fly.toml` sets `SENTINEL_REQUIRE_TOKEN=1`, which makes the process **refuse
to boot** if `SENTINEL_TOKEN` is empty — an exposed instance can never come up
open by accident. `force_https = true` and the `/healthz` probe are already
configured.

Other hosts: `docker build -t sentinel .` then run it with
`-e SENTINEL_REQUIRE_TOKEN=1 -e SENTINEL_TOKEN=... -e SENTINEL_DATA=/data -p 8080:8080`.

### Record that token somewhere

The PWA prompts for it on first load and stores it in the browser. If you lose
it, `fly secrets list` shows the name but not the value — regenerate it.

## Step 3 — first run

1. Open `https://<your-host>/`, paste the token.
2. **Import** → paste your `DeviceManager.xml`.
3. Edit each device and set **Host** to `your-ddns-name` (or the site's public
   IP) and **Port** `34567`. Or do it in one shot:
   ```bash
   curl -X POST https://<your-host>/api/devices/import \
     -H "Authorization: Bearer $SENTINEL_TOKEN" -H 'Content-Type: application/json' \
     -d '{"xml":"<DeviceManager .../>",
          "hosts":{"AA0000000AAAAA1":"your-ddns-name:34567"}}'
   ```
4. Start a tile. Watch `Health` for `state: live` and a non-zero fps.

The token travels as `?token=` on the WebSocket, so it can appear in proxy
logs — keep TLS on and don't paste the URL into a public chat.

## Security checklist

- [ ] The bootstrap admin password changed, and `SENTINEL_ADMIN_PASSWORD` unset
- [ ] A separate account per person, least-privilege role
- [ ] `SENTINEL_REQUIRE_TOKEN=1` in effect
- [ ] HTTPS enforced (`force_https`, or terminate TLS at your proxy)
- [ ] NVR port forward is **34567 only** — never 80, never 37777
- [ ] Cameras still use their own strong passwords, not the defaults
- [ ] `data/` is gitignored and never committed
- [ ] Someone is watching for unknown devices on each site

### What you have accepted by deploying this way

Anyone who obtains the token can watch your cameras, view them from the device
list, move the PTZ cameras and pull recordings. DVRIP itself sends the
password as an MD5-derived digest and is **not** end-to-end encrypted, so a
hostile network between the gateway and your NVR can observe it.

If any of that is unacceptable, stop and use Tailscale. It is free, takes ten
minutes, and removes the entire attack surface this document creates.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `nc` says connection refused | port forward missing, wrong port, or CGNAT |
| `nc` times out | ISP blocks the port, or the NVR is offline |
| `login refused: username or password is incorrect` | credentials wrong, or that account lacks stream rights |
| Tile says `no host configured` | imported but never given an address |
| `state: reconnecting` repeatedly | gateway can reach the IP but the device rejects the session; check the port and the DVRIP service is enabled |
| Health shows live but fps drops to 0 | host→NVR bandwidth; lower the NVR's main-stream bitrate |