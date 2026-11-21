#!/usr/bin/env python3
"""Seed the DEMO instance on port 8001 with demo admin, 3 bots, live cycles,
a labelled test position, a published bot and a Pro test subscriber."""
import json, urllib.request, http.cookiejar

BASE = "http://127.0.0.1:8001"

class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def call(self, method, path, body=None, timeout=900):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

admin = Client()
st, d = admin.call("POST", "/api/signup", {"email": "demo@alphaswarm.local", "password": "DemoPass123!", "name": "Demo Admin"})
if st != 200:
    st, d = admin.call("POST", "/api/login", {"email": "demo@alphaswarm.local", "password": "DemoPass123!"})
print("admin signup:", st, d.get("role") or d)

st, bots = admin.call("GET", "/api/bots")
existing = bots.get("bots", []) if isinstance(bots, dict) else []
ids = {b.get("kind") or b.get("name"): b["id"] for b in existing}
print("existing bots:", ids)

def ensure_bot(kind, name):
    for b in existing:
        if b.get("name") == name:
            return b["id"]
    st, d = admin.call("POST", "/api/bots", {"kind": kind, "name": name, "starting_balance": 10000})
    print("create bot", name, st, d)
    return (d.get("bot") or {}).get("id") or d.get("id")

v1 = ensure_bot("v1", "Technical v1")
v2 = ensure_bot("v2", "Technical v2")
ev = ensure_bot("event", "Event Watcher")
print("bot ids:", v1, v2, ev)

for bid, nm in [(v1, "v1"), (v2, "v2")]:
    st, d = admin.call("POST", f"/api/bots/{bid}/cycle")
    print("cycle", nm, st, str(d)[:200])

st, d = admin.call("POST", f"/api/bots/{v1}/demo-trade")
print("demo-trade v1:", st, str(d)[:200])

st, d = admin.call("POST", f"/api/bots/{ev}/cycle")
print("cycle event watcher:", st, str(d)[:300])

st, d = admin.call("POST", f"/api/bots/{v1}/publish")
print("publish v1:", st, str(d)[:150])

user = Client()
st, d = user.call("POST", "/api/signup", {"email": "fan@alphaswarm.local", "password": "FanPass123!", "name": "Demo Fan"})
if st != 200:
    st, d = user.call("POST", "/api/login", {"email": "fan@alphaswarm.local", "password": "FanPass123!"})
print("user signup:", st, d.get("role") or d)
st, d = user.call("POST", "/api/checkout", {"plan": "pro", "card_number": "4242 4242 4242 4242"})
print("user checkout pro:", st, str(d)[:200])
print("DONE")
