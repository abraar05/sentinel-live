#!/usr/bin/env python3
"""Exactly one login attempt. Never loop - the recorder hard-locks."""
import hashlib, json, sys, urllib.request, urllib.error

BASE, USER = "http://192.168.31.120", "admin"
PASSWORD = sys.argv[1]
md5 = lambda s: hashlib.md5(s.encode()).hexdigest()

def post(body, cookie=None):
    req = urllib.request.Request(BASE + "/RPC2_Login", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return "", {"err": repr(e)}

body, hdrs = post({"method": "global.login",
                   "params": {"userName": USER, "password": "", "clientType": "Web3.0"}, "id": 1})
if not body:
    print("unreachable"); sys.exit(1)
ch = json.loads(body); p = ch.get("params", {})
if "challenge" not in body:
    print(f"still locked: {p.get('remainLockSecond')}s"); sys.exit(1)
o = md5(f"{USER}:{p.get('realm','')}:{PASSWORD}")
resp = md5(f"{USER}:{p.get('random','')}:{o}")
req = {"method": "global.login",
       "params": {"userName": USER, "password": resp, "clientType": "Web3.0",
                  "authorityType": p.get("encryption", "Default"),
                  "passwordType": p.get("encryption", "Default")}, "id": 2}
if ch.get("session") is not None:
    req["session"] = ch["session"]
body2, h2 = post(req, hdrs.get("Set-Cookie", "").split(";")[0])
r = json.loads(body2) if body2 else {}
pr = r.get("params", {})
if pr.get("remainLockSecond"):
    print(f"LOCKED {pr.get('remainLockSecond')}s / {pr.get('remainLoginTimes')} tries")
elif r.get("result") is True:
    out = {"state": "ok", "session": r.get("session"),
           "cookie": h2.get("Set-Cookie", "").split(";")[0]}
    open("/tmp/opencode/login_result.txt", "w").write(json.dumps(out))
    print("LOGIN OK " + json.dumps(out))
else:
    open("/tmp/opencode/login_result.txt", "w").write(json.dumps({"state": "bad", "detail": body2[:200]}))
    print("REJECTED " + body2[:160])
