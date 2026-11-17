#!/usr/bin/env python3
"""
A headless replacement for the EMOTIV Launcher.

Why this exists
---------------
The EMOTIV Launcher is a graphical front end to a local background service,
CortexService, which is what actually owns the headset. The Launcher is not
required to use the headset; it is required only to log in and to display
things. On this machine it fails in two ways that have nothing to do with the
hardware:

1. Account synchronisation. The service repeatedly logs
   "[det_sync] SyncMetadata is not available for user <id> 500" - an HTTP 500
   from Emotiv's own servers for this account. Launcher login waits on that
   sync, so it hangs at "Connecting to server ...". This is the one failure
   with direct evidence in the service log, and no local change can fix a
   fault on their side.

2. A stale connection after the service restarts. CortexService is a server
   and happily serves several clients at once, so two Launcher windows do not
   exclude one another. But when the service is restarted, a Launcher whose
   socket dropped does not always re-establish it: it then sits at the same
   "Connecting to server ..." message with a dead connection while another
   window, which did reconnect, works normally. That is why the failure can
   look random - it depends on which window you happen to be looking at.

This daemon avoids both. It never starts the graphical Launcher, it speaks the
service's local JSON-RPC interface directly, and it never waits on the account
synchronisation that is failing. It keeps exactly one copy of itself running,
restarts the service if it stops answering, connects the headset, and writes a
status file that other tools can read.

Usage
-----
    ./launcherd.py status     one-shot report, exits
    ./launcherd.py connect    connect the headset once, exits
    ./launcherd.py start      run in the background until stopped
    ./launcherd.py stop       stop the background instance
    ./launcherd.py foreground run in this terminal (Ctrl-C to stop)

Reads the same client credentials the rest of this project uses, so the
contact-quality and battery readings the headset reports to Cortex are
available here too.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cortex_ws import Cortex, CortexError  # noqa: E402

HOME = Path.home()
STATE_DIR = HOME / ".emotiv-launcher"
PID_FILE = STATE_DIR / "launcherd.pid"
STATUS_FILE = STATE_DIR / "status.json"
LOG_FILE = STATE_DIR / "launcherd.log"

CORTEX_BIN = Path("/Applications/EmotivApps/EMOTIV Launcher.app/Contents/MacOS/CortexService")
CREDENTIALS = HOME / "Documents" / "Affect Music" / "eeg_credentials.json"

POLL_SECONDS = 10
# Restarting the service is disruptive, so it is rate-limited: the daemon will
# not do it more than this often however unhealthy things look.
RESTART_COOLDOWN = 120


def log(msg):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- processes

def pids_of(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return [int(p) for p in out.stdout.split()]
    except (OSError, ValueError):
        return []


def service_pids():
    return pids_of("MacOS/CortexService")


def launcher_gui_pids():
    """Graphical Launcher processes - not the service, which shares a folder."""
    return [p for p in pids_of("EMOTIV Launcher.app/Contents/MacOS/EMOTIV Launcher")]


def restart_service():
    """
    Stop CortexService and let launchd bring it back.

    The com.emotiv.cortex LaunchDaemon is configured KeepAlive=1, so ending the
    process is the sanctioned way to restart it and needs no privileges. If
    launchd does not return it within a few seconds, start it directly.
    """
    before = service_pids()
    for pid in before:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    log(f"service restart requested (was pid {before or 'none'})")
    for _ in range(20):
        time.sleep(1)
        now = [p for p in service_pids() if p not in before]
        if now:
            log(f"launchd returned the service as pid {now[0]}")
            return True
    if CORTEX_BIN.exists():
        subprocess.Popen([str(CORTEX_BIN)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log("started CortexService directly")
        time.sleep(8)
        return bool(service_pids())
    log("CortexService binary not found; cannot start it")
    return False


# ------------------------------------------------------------------- cortex

def credentials():
    if not CREDENTIALS.exists():
        return None
    try:
        with open(CREDENTIALS) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def probe(connect_headset=True, want_device_stream=True):
    """
    Ask the service what it knows. Returns a status dict; never raises.

    Only local calls are made. Account synchronisation is never waited on.
    """
    st = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "service_running": bool(service_pids()),
        "service_pids": service_pids(),
        "service_answering": False,
        "cortex_version": None,
        "logged_in_user": None,
        "headsets": [],
        "connected_headset": None,
        "battery_percent": None,
        "contact_quality": None,
        "duplicate_launchers": [],
        "problems": [],
    }

    gui = launcher_gui_pids()
    if len(gui) > 1:
        st["duplicate_launchers"] = gui
        st["problems"].append(
            f"{len(gui)} graphical Launcher windows are open (pids {gui}). They do not "
            f"block each other, but after the service restarts a window whose "
            f"connection dropped may not re-establish it and will sit at 'Connecting "
            f"to server' while another window works. This daemon needs none of them."
        )

    if not st["service_running"]:
        st["problems"].append("CortexService is not running.")
        return st

    try:
        cx = Cortex(timeout=15)
    except (CortexError, OSError) as exc:
        st["problems"].append(f"Service is running but not accepting connections: {exc}")
        return st

    try:
        info = cx.call("getCortexInfo")
        st["service_answering"] = True
        st["cortex_version"] = info.get("version")

        try:
            users = cx.call("getUserLogin")
            if users:
                st["logged_in_user"] = users[0].get("username")
        except CortexError as exc:
            st["problems"].append(f"getUserLogin failed: {exc}")

        heads = cx.call("queryHeadsets", timeout=25) or []
        st["headsets"] = [
            {"id": h.get("id"), "status": h.get("status"),
             "connectedBy": h.get("connectedBy"), "firmware": h.get("firmwareDisplay"),
             "settings": h.get("settings")}
            for h in heads
        ]
        if not heads:
            st["problems"].append(
                "No headset found. Check it is switched on, and that either the USB "
                "receiver is plugged in or Bluetooth pairing is in place."
            )
            return st

        head = next((h for h in heads if h.get("status") == "connected"), heads[0])
        hid = head.get("id")

        if connect_headset and head.get("status") != "connected":
            try:
                cx.call("controlDevice",
                        {"command": "connect", "headset": hid}, timeout=30)
                for _ in range(12):
                    time.sleep(2)
                    heads = cx.call("queryHeadsets", {"id": hid}, timeout=20) or []
                    if heads and heads[0].get("status") == "connected":
                        head = heads[0]
                        break
            except CortexError as exc:
                st["problems"].append(f"Could not connect the headset: {exc}")

        st["connected_headset"] = {"id": hid, "status": head.get("status"),
                                   "connectedBy": head.get("connectedBy")}

        if want_device_stream and head.get("status") == "connected":
            _device_stream(cx, hid, st)

    except CortexError as exc:
        st["problems"].append(str(exc))
    finally:
        try:
            cx.close()
        except Exception:
            pass
    return st


def _device_stream(cx, hid, st):
    """
    Subscribe to the 'dev' stream, which carries battery level and per-electrode
    contact quality. This is the one thing Cortex provides that the offline
    reader in this project cannot: those values are not decoded from the raw
    packets. Requires the client credentials and a licence.
    """
    cred = credentials()
    if not cred:
        st["problems"].append(
            f"No credentials at {CREDENTIALS}, so battery and contact quality are "
            f"unavailable. Everything else above still works.")
        return
    try:
        token = cx.call("authorize", {
            "clientId": cred["clientId"], "clientSecret": cred["clientSecret"],
            "license": cred.get("license"), "debit": cred.get("debit", 0),
        }, timeout=30)["cortexToken"]
        session = cx.call("createSession", {
            "cortexToken": token, "headset": hid, "status": "open"}, timeout=30)
        sub = cx.call("subscribe", {
            "cortexToken": token, "session": session["id"], "streams": ["dev"]}, timeout=30)
        cols = (sub.get("success") or [{}])[0].get("cols") or []
        msg = None
        for _ in range(10):
            msg = cx.next_stream(timeout=3)
            if msg and "dev" in msg:
                break
        if msg and "dev" in msg:
            dev = msg["dev"]
            # Documented order: [battery, signal, [per-electrode quality], battery%]
            if len(dev) >= 4 and isinstance(dev[3], (int, float)):
                st["battery_percent"] = dev[3]
            elif dev and isinstance(dev[0], (int, float)):
                st["battery_percent"] = dev[0]
            quality = next((d for d in dev if isinstance(d, list)), None)
            if quality is not None:
                labels = cols[2] if len(cols) > 2 and isinstance(cols[2], list) else None
                st["contact_quality"] = (
                    dict(zip(labels, quality)) if labels and len(labels) == len(quality)
                    else quality)
        else:
            st["problems"].append("Subscribed to the device stream but no reading arrived.")
        try:
            cx.call("unsubscribe", {"cortexToken": token, "session": session["id"],
                                    "streams": ["dev"]}, timeout=10)
            cx.call("updateSession", {"cortexToken": token, "session": session["id"],
                                      "status": "close"}, timeout=10)
        except CortexError:
            pass
    except (CortexError, KeyError) as exc:
        st["problems"].append(f"Battery and contact quality unavailable: {exc}")


# -------------------------------------------------------------------- daemon

def write_status(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, STATUS_FILE)


def read_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def loop():
    last_restart = 0.0
    unhealthy = 0
    log("launcher daemon started")
    while True:
        st = probe()
        write_status(st)
        healthy = st["service_answering"] and bool(st["headsets"])
        if healthy:
            unhealthy = 0
        else:
            unhealthy += 1
            log(f"unhealthy ({unhealthy}): {'; '.join(st['problems']) or 'unknown'}")
            # Two consecutive bad checks, and not too soon after the last one.
            if unhealthy >= 2 and (time.time() - last_restart) > RESTART_COOLDOWN:
                if not st["service_answering"]:
                    restart_service()
                    last_restart = time.time()
                    unhealthy = 0
        time.sleep(POLL_SECONDS)


def start():
    pid = read_pid()
    if pid:
        print(f"Already running as pid {pid}.")
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "foreground"],
        stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT,
        start_new_session=True)
    PID_FILE.write_text(str(proc.pid))
    time.sleep(2)
    print(f"Launcher running in the background as pid {proc.pid}.")
    print(f"  status:  {STATUS_FILE}")
    print(f"  log:     {LOG_FILE}")
    return 0


def stop():
    pid = read_pid()
    if not pid:
        print("Not running.")
        PID_FILE.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        time.sleep(0.5)
        if not read_pid():
            break
    PID_FILE.unlink(missing_ok=True)
    print(f"Stopped pid {pid}.")
    return 0


def print_status(st):
    def line(k, v):
        print(f"  {k:22} {v}")
    print("\nEMOTIV headset status (no Launcher involved)")
    print("-" * 52)
    line("service", f"running, pid {st['service_pids']}" if st["service_running"] else "NOT RUNNING")
    line("service answering", "yes" if st["service_answering"] else "no")
    if st["cortex_version"]:
        line("cortex version", st["cortex_version"])
    if st["logged_in_user"]:
        line("account", st["logged_in_user"])
    if st["headsets"]:
        for h in st["headsets"]:
            line("headset", f"{h['id']}  {h['status']}  via {h['connectedBy']}  fw {h['firmware']}")
    else:
        line("headset", "none found")
    if st["battery_percent"] is not None:
        line("battery", f"{st['battery_percent']}%")
    if st["contact_quality"]:
        cq = st["contact_quality"]
        if isinstance(cq, dict):
            line("contact quality", "  ".join(f"{k}:{v}" for k, v in cq.items()))
        else:
            line("contact quality", cq)
    if st["problems"]:
        print("\n  problems")
        for p in st["problems"]:
            print(f"    - {p}")
    print()


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        st = probe(connect_headset=False)
        write_status(st)
        print_status(st)
    elif cmd == "connect":
        st = probe(connect_headset=True)
        write_status(st)
        print_status(st)
    elif cmd == "start":
        return start()
    elif cmd == "stop":
        return stop()
    elif cmd == "foreground":
        try:
            loop()
        except KeyboardInterrupt:
            log("stopped")
    elif cmd == "restart-service":
        restart_service()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
