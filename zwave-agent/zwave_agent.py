#!/usr/bin/env python3
"""zwave_agent — runs on each Z-Wave Pi (systemd). One HTTP service that lets Home Assistant
keep the zwave-js container healthy and current, and (on the home Pi) feeds the Kospel ESP.

Endpoints (HTTP on PORT, default 8901):
  GET  /trv.json           -> Fibaro TRV snapshot (home Pi; used by the KC868 ESP). No token.
  GET  /health             -> {running,image,driver_version,schema,restarts,exit_code,error,disk_pct}. No token.
  POST /restart            -> restart the container (start if stopped); returns fail reason if it was down. TOKEN.
  POST /update             -> rollback-safe image update (tag rollback, pull, verify arm/v7, recreate
                              w/ log rotation, health-check, auto-rollback on failure). TOKEN.
Token via header  X-Token: <ZW_AGENT_TOKEN env>  (LAN-only; prevents accidental triggers).

Deploy: /opt/zwave-agent/zwave_agent.py + venv(websockets) + systemd zwave-agent.service.
Both Pis are identical: device /dev/ttyACM0, store /root/zwavejs2mqtt/store, ports 3000+8091.
"""
import asyncio, json, os, shutil, socket, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websockets

PORT = int(os.environ.get("ZW_AGENT_PORT", "8901"))
TOKEN = os.environ.get("ZW_AGENT_TOKEN", "")
CONTAINER = os.environ.get("ZW_CONTAINER", "zwavejs2mqtt")
IMAGE = os.environ.get("ZW_IMAGE", "zwavejs/zwavejs2mqtt:latest")
COMPOSE_DIR = os.environ.get("ZW_COMPOSE_DIR", "")   # if set -> manage via docker compose (e.g. .81 host-mode)
WS = os.environ.get("ZW_WS", "ws://127.0.0.1:3000")
# UDP push target for the Kospel ESP (the ESP's HTTP pull was killing its HA API connection).
# Empty/unset -> no push (garage Pi has no TRVs anyway).
ESP_UDP = os.environ.get("ZW_ESP_UDP", "192.168.1.221:8902")
# fixed run args (identical on both Pis)
RUN_ARGS = ["--name", CONTAINER, "--restart", "always",
            "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            "--device", "/dev/ttyACM0:/dev/ttyACM0",
            "-v", "/root/zwavejs2mqtt/store:/usr/src/app/store",
            "-p", "3000:3000", "-p", "8091:8091",
            "-e", "SESSION_SECRET=" + os.environ.get("ZW_SESSION_SECRET", "changeme")]
# EDIT ME: your Fibaro FGT-001 node ids -> room names (from Z-Wave JS UI)
TRV = {6: "kuchnia", 7: "velux", 8: "balkon", 9: "homeoffice", 33: "sypialnia", 34: "wykusz"}
STATE = {"ts": 0, "driver_version": None, "schema": None}

def dk(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)

def dcompose(*args, timeout=600):
    return subprocess.run(["docker", "compose", "-f", os.path.join(COMPOSE_DIR, "docker-compose.yml"), *args],
                          capture_output=True, text=True, timeout=timeout)

# ---------- zwave-js WS poll (TRV data + driver version for /health) ----------
async def poll_once():
    # NB: websockets 8.1 (Debian 11) has no open_timeout kwarg -> wrap connect in wait_for
    ws = await asyncio.wait_for(websockets.connect(WS, max_size=64 * 1024 * 1024), timeout=15)
    try:
        hello = json.loads(await ws.recv())
        STATE["driver_version"] = hello.get("driverVersion")
        STATE["schema"] = hello.get("maxSchemaVersion")
        await ws.send(json.dumps({"messageId": "s", "command": "set_api_schema",
                                  "schemaVersion": min(35, hello.get("maxSchemaVersion", 35))}))
        await ws.recv()
        await ws.send(json.dumps({"messageId": "l", "command": "start_listening"}))
        res = json.loads(await ws.recv())
        rooms = {}
        for n in res["result"]["state"]["nodes"]:
            nm = TRV.get(n["nodeId"])
            if not nm:
                continue
            r = {"temp": None, "cel": None, "okno": 0, "grzeje": 0, "bateria": None}
            for v in n.get("values", []):
                cc, p, k, ep, val = (v.get("commandClass"), v.get("property"),
                                     v.get("propertyKey"), v.get("endpoint"), v.get("value"))
                if cc == 49 and p == "Air temperature": r["temp"] = val
                elif cc == 67 and p == "setpoint": r["cel"] = val
                elif cc == 128 and ep == 0: r["bateria"] = val
                elif cc == 112 and p == 3 and k == 1: r["okno"] = int(val or 0)
                elif cc == 112 and p == 3 and k == 2: r["grzeje"] = int(val or 0)
            rooms[nm] = r
        temps = [r["temp"] for r in rooms.values() if isinstance(r["temp"], (int, float))]
        STATE.update({"ts": int(time.time()), "rooms": rooms,
                      "okna": sum(r["okno"] for r in rooms.values()),
                      "grzeja": sum(r["grzeje"] for r in rooms.values()),
                      "min_temp": round(min(temps), 1) if temps else None,
                      "srednia": round(sum(temps) / len(temps), 1) if temps else None})
    finally:
        await ws.close()

def push_udp():
    if not ESP_UDP or not STATE.get("rooms"): return
    try:
        host, port = ESP_UDP.rsplit(":", 1)
        pkt = json.dumps({"okna": STATE.get("okna", 0), "min_temp": STATE.get("min_temp")}).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, (host, int(port)))       # unicast to the ESP
        s.sendto(pkt, ("192.168.1.255", int(port)))  # + LAN broadcast (udp component listens on broadcast)
        s.close()
    except Exception as e:
        print("udp push err:", type(e).__name__, str(e)[:80], flush=True)

def poll_loop():
    while True:
        try:
            asyncio.run(poll_once())
            push_udp()
        except Exception as e:
            print("poll err:", type(e).__name__, str(e)[:120], flush=True)
        time.sleep(30)

# ---------- container ops ----------
def container_health():
    r = dk("inspect", CONTAINER, "--format",
           "{{.State.Running}}|{{.Config.Image}}|{{.RestartCount}}|{{.State.ExitCode}}|{{.State.Error}}", timeout=20)
    running = image = restarts = exit_code = err = None
    if r.returncode == 0:
        parts = (r.stdout.strip() + "||||").split("|")
        running = parts[0] == "true"; image = parts[1]; restarts = parts[2]
        exit_code = parts[3]; err = parts[4]
    try:
        du = shutil.disk_usage("/"); disk_pct = round(du.used / du.total * 100)
    except Exception:
        disk_pct = None
    return {"container": CONTAINER, "running": running, "image": image, "restarts": restarts,
            "exit_code": exit_code, "error": err or None, "disk_pct": disk_pct,
            "driver_version": STATE.get("driver_version"), "schema": STATE.get("schema"),
            "data_age_s": (int(time.time()) - STATE["ts"]) if STATE["ts"] else None}

def fail_reason():
    logs = dk("logs", "--tail", "20", CONTAINER, timeout=20)
    tail = (logs.stdout or "") + (logs.stderr or "")
    import re
    tail = re.sub(r"\x1b\[[0-9;]*m", "", tail).strip().splitlines()
    return " | ".join(tail[-6:])[:600]

def do_restart():
    h0 = container_health()
    reason = "" if h0.get("running") else fail_reason()
    r = dk("restart", CONTAINER, timeout=90)
    if r.returncode != 0:
        r = dk("start", CONTAINER, timeout=90)
    time.sleep(6)
    h1 = container_health()
    return {"ok": bool(h1.get("running")), "was_running": h0.get("running"),
            "fail_reason": reason, "health": h1}

def do_update():
    old = dk("inspect", CONTAINER, "--format", "{{.Image}}", timeout=20).stdout.strip()
    if old:
        dk("tag", old, CONTAINER + ":rollback", timeout=30)
    # pull (compose or plain)
    pull = dcompose("pull") if COMPOSE_DIR else dk("pull", IMAGE, timeout=600)
    if pull.returncode != 0:
        return {"ok": False, "step": "pull", "err": (pull.stderr or "")[-300:]}
    arch = dk("image", "inspect", IMAGE, "--format", "{{.Architecture}}/{{.Variant}}", timeout=20).stdout.strip()
    if not arch.startswith("arm/v7"):
        return {"ok": False, "step": "arch", "arch": arch, "note": "not arm/v7 — aborted, container untouched"}
    new = dk("image", "inspect", IMAGE, "--format", "{{.Id}}", timeout=20).stdout.strip()
    if old == new:
        return {"ok": True, "changed": False, "note": "already latest", "version": STATE.get("driver_version")}
    if COMPOSE_DIR:
        up = dcompose("up", "-d")
        time.sleep(20)
        if dk("inspect", CONTAINER, "--format", "{{.State.Running}}", timeout=20).stdout.strip() != "true":
            if old:
                dk("tag", old, IMAGE, timeout=30)   # revert :latest to the old image, recreate
            dcompose("up", "-d")
            return {"ok": False, "step": "compose-up", "note": "new image did not run — ROLLED BACK",
                    "err": (up.stderr or "")[-300:]}
        return {"ok": True, "changed": True, "old": old[:19], "new": new[:19], "arch": arch, "mode": "compose"}
    dk("rm", "-f", CONTAINER, timeout=60)
    run = dk("run", "-d", *RUN_ARGS, IMAGE, timeout=120)
    time.sleep(20)
    if dk("inspect", CONTAINER, "--format", "{{.State.Running}}", timeout=20).stdout.strip() != "true":
        dk("rm", "-f", CONTAINER, timeout=60)
        dk("run", "-d", *RUN_ARGS, CONTAINER + ":rollback", timeout=120)
        return {"ok": False, "step": "recreate", "note": "new image did not run — ROLLED BACK",
                "run_err": (run.stderr or "")[-300:]}
    return {"ok": True, "changed": True, "old": old[:19], "new": new[:19], "arch": arch, "mode": "run"}

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _auth(self):
        return TOKEN and self.headers.get("X-Token") == TOKEN
    def do_GET(self):
        if self.path.startswith("/trv"): self._send(200, STATE)
        elif self.path.startswith("/health"): self._send(200, container_health())
        else: self._send(404, {"error": "not found"})
    def do_POST(self):
        if not self._auth(): self._send(403, {"error": "bad token"}); return
        if self.path.startswith("/restart"): self._send(200, do_restart())
        elif self.path.startswith("/update"): self._send(200, do_update())
        else: self._send(404, {"error": "not found"})
    def log_message(self, *a): pass

def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"zwave_agent on :{PORT} container={CONTAINER} image={IMAGE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

if __name__ == "__main__":
    main()
