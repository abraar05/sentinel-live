#!/usr/bin/env python3
"""Wait out the recorder's login lockout, then make exactly one attempt.

Written because the device counts failed attempts and hard-locks, so a retry
loop is the one thing that must never happen here.
"""
import hashlib, json, sys, time, urllib.request, urllib.error

BASE = "http://192.168.31.120"
LOGIN = BASE + "/RPC2_Login"
USER = "admin"
PASSWORD = sys.argv[1] if len(sys.argv) > 1 else "amaya123"
OUT = "/tmp/opencode/login_result.txt"
md5 = lambda s: hashlib.md5(s.encode()).hexdigest()


def post(body, cookie=None):
    req = urllib.request.Request(LOGIN, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return "", {"err": repr(e)}


def save(state, detail):
    with open(OUT, "w") as fh:
        json.dump({"state": state, "detail": detail, "password": PASSWORD}, fh)


# --- wait for the lockout countdown to reach zero ---------------------------
waited = 0
while waited < 1800:
    body, _ = post({"method": "global.login",
                     "params": {"userName": USER, "password": "",
                                "clientType": "Web3.0"}, "id": 1})
    ch = json.loads(body) if body else {}
    lock = (ch.get("params") or {}).get("remainLockSecond")
    if lock is None:
        break
    print(f"  locked, {lock}s remaining (waited {waited}s)", flush=True)
    time.sleep(min(int(lock) + 15, 300))
    waited += min(int(lock) + 15, 300)

print(f"  lockout window passed after {waited}s; making ONE attempt", flush=True)

# --- exactly one attempt ---------------------------------------------------
body, hdrs = post({"method": "global.login",
                   "params": {"userName": USER, "password": "",
                              "clientType": "Web3.0"}, "id": 1})
ch = json.loads(body)
p = ch.get("params", {})
if "challenge" not in body:
    save("locked", str(p.get("remainLockSecond")))
    raise SystemExit(0)

o = md5(f"{USER}:{p.get('realm','')}:{PASSWORD}")
resp = md5(f"{USER}:{p.get('random','')}:{o}")
req = {"method": "global.login",
       "params": {"userName": USER, "password": resp, "clientType": "Web3.0",
                  "authorityType": p.get("encryption", "Default"),
                  "passwordType": p.get("encryption", "Default")},
       "id": 2}
if ch.get("session") is not None:
    req["session"] = ch["session"]
cookie = hdrs.get("Set-Cookie", "").split(";")[0]

body2, h2 = post(req, cookie)
r = json.loads(body2) if body2 else {}
pr = r.get("params", {})
if pr.get("remainLockSecond"):
    save("locked", f"{pr.get('remainLockSecond')}s / {pr.get('remainLoginTimes')} tries")
elif r.get("result") is True:
    save("ok", json.dumps({"session": r.get("session"),
                           "cookie": h2.get("Set-Cookie", "").split(";")[0]}))
else:
    save("bad", body2[:200])
print(f"  result: {open(OUT).read()}", flush=True)