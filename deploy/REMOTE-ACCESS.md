# Reaching Sentinel from outside the office

The gateway only ever needs to be on a machine that can **see the NVR on the
LAN**. It does not need a public IP, and it does not need the XMeye cloud.
Everything below is about getting *your phone* to *that machine*.

Pick the one that matches where your desktop is.

---

## A. Desktop is at the office, on the same LAN as the NVR  ← best

Run the gateway there. It talks DVRIP to every camera directly.

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File deploy\windows\install-and-run.ps1
```
```bash
# Linux / macOS
bash deploy/linux/install-and-run.sh
```

It binds to `127.0.0.1`, so nothing is exposed. Now expose just that to your
phone:

**Tailscale (recommended, private):**
```powershell
winget install tailscale.tailscale     # or: brew install tailscale
tailscale up
tailscale ip -4                          # e.g. 100.x.y.z
```
Then on the phone, install Tailscale, sign in with the same account, open
`http://100.x.y.z:8080`. Done — no public IP, no port forwarding, nothing
scan-able.

**Cloudflare Tunnel (a real HTTPS URL):**
```powershell
winget install cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8080
```
Gives `https://something.trycloudflare.com`. Use this only with a strong
admin password — the URL is public to anyone who has it.

---

## B. You can get to the office router (5 minutes, once)

Port forward **TCP 34567** to the NVR, and give the site a DDNS name. Then any
host anywhere works, with no desktop involved.

1. Static DHCP lease for the NVR.
2. Forward external **TCP 34567** → `NVR_IP:34567`.
3. DDNS name on the router (or DuckDNS).
4. Verify from your phone's cellular: `nc -vz your-ddns-name 34567`.

Then deploy the gateway anywhere — see [DEPLOY.md](../DEPLOY.md).

**If step 4 fails, this route is dead.** Most residential ISPs use CGNAT,
where forwarding cannot work at all. Check before spending time.

---

## C. Neither the desktop nor the router is available

Then there is no path from outside, and there is no third-party workaround:

- The XMeye cloud ids in your `DeviceManager.xml` are **not** dialable. XMeye
  P2P is a proprietary UDP rendezvous in which Xiongmai's cloud brokers every
  connection; only XMeye and gDMSS carry the vendor SDK needed to speak it.
- gDMSS Lite on your desktop already solves this — it *has* the SDK. If you
  need remote access today and cannot touch the office, gDMSS Lite remains the
  working answer.

---

## What actually needs the office

| Route | Needs office access? | Time |
|---|---|---|
| A. Tailscale from the desktop | Desktop reachable/controllable remotely | 10 min, once |
| B. Router port forward | Yes, physically or via router admin | 5 min, once |
| C. Nothing | No | n/a |

Route A is the one to pick if the desktop is a machine you can already reach
from wherever you are — the gateway runs there permanently and your phone
becomes the only thing that needs to travel.