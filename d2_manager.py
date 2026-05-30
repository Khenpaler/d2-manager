#!/usr/bin/env python3
"""
Smart Bro GreenPacket D2-220G Manager
======================================
Local web tool for LTE band locking, signal monitoring, and WiFi settings.

Requirements:
    pip install flask

Usage:
    python3 d2_manager.py
    Then open: http://localhost:5000

SETUP (one-time):
    1. Log into http://192.168.1.1 or http://smartbrosettings.net
    2. Go to Advanced Settings → System → Diagnostics (Ping Test)
    3. In the ping host field, paste:
           127.0.0.1 & busybox telnetd -p 2323 -l /bin/sh
    4. Click Ping/Start — telnet will be active until next reboot
    5. Come back to this tool and click "Check Connection"
"""

import socket
import time
import threading
from flask import Flask, render_template_string, jsonify, request, Response

app = Flask(__name__)

ROUTER_IP = "192.168.1.1"
TELNET_PORT = 2323
CMD_TIMEOUT = 8

# LTE Band bitmask values (these are the hex values as integers)
# Format: AT+ZNLOCKBAND=1,0,<hex_value>,0
# Value 0 = auto (all bands unlocked)
BAND_INFO = {
    "B1":  {"label": "B1 — 2100 MHz",  "hex": 0x1,          "int": 1},
    "B3":  {"label": "B3 — 1800 MHz",  "hex": 0x4,          "int": 4},
    "B5":  {"label": "B5 — 850 MHz",   "hex": 0x10,         "int": 16},
    "B7":  {"label": "B7 — 2600 MHz",  "hex": 0x40,         "int": 64},
    "B8":  {"label": "B8 — 900 MHz",   "hex": 0x80000,      "int": 524288},
    "B28": {"label": "B28 — 700 MHz",  "hex": 0x8000000,    "int": 134217728},
    "B40": {"label": "B40 — TDD 2300", "hex": 0x8000000000, "int": 549755813888},
    "B41": {"label": "B41 — TDD 2500", "hex": 0x10000000000,"int": 1099511627776},
}


# ─── Telnet Helper ────────────────────────────────────────────────────────────

class TelnetSession:
    def __init__(self, host, port, timeout=CMD_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        time.sleep(0.3)
        self._read_until_quiet(idle_timeout=0.5)

    def _read_until_quiet(self, idle_timeout=2.5):
        """Read until no new data arrives for idle_timeout seconds."""
        self.sock.settimeout(idle_timeout)
        data = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        self.sock.settimeout(self.timeout)
        return data.decode("utf-8", errors="ignore")

    def run(self, cmd, wait=3.0):
        # Send a sentinel echo after the command so we know output ended
        sentinel = "DONE_SENTINEL_XYZ"
        full = f"{cmd}\necho {sentinel}\n"
        self.sock.sendall(full.encode())
        # Read with idle timeout
        buf = b""
        self.sock.settimeout(wait)
        deadline = time.time() + wait + 5
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if sentinel.encode() in buf:
                    break
            except socket.timeout:
                break
        self.sock.settimeout(self.timeout)
        result = buf.decode("utf-8", errors="ignore")
        # Strip the sentinel and trailing shell prompt noise
        if sentinel in result:
            result = result[:result.index(sentinel)]
        return result.strip()

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


def telnet_run(cmd, wait=2.0):
    """Open a telnet session, run one command, return output."""
    t = TelnetSession(ROUTER_IP, TELNET_PORT)
    try:
        t.connect()
        return t.run(cmd, wait=wait)
    except Exception as e:
        return f"[Error] {e}"
    finally:
        t.close()


def at_cmd(atcmd, wait=3.0):
    """Send an AT command, trying ubus first then at.sh fallback."""
    t = TelnetSession(ROUTER_IP, TELNET_PORT)
    try:
        t.connect()

        # Method 1: ubus call modemd atcmd
        escaped = atcmd.replace('"', '\\"')
        ubus_cmd = f'ubus call modemd atcmd \'{{"atcmd":"{escaped}"}}\''
        result = t.run(ubus_cmd, wait=wait)

        # If ubus returned nothing useful, try /etc/modemd/at.sh
        if not result or "ubus: not found" in result or result.strip() == "":
            atsh_cmd = f"/etc/modemd/at.sh {atcmd}"
            result = t.run(atsh_cmd, wait=wait)

        # If still empty, try direct atcmd via echo to modem device
        if not result or result.strip() == "":
            result = t.run(f'echo -e "{atcmd}\\r" > /dev/ttyUSB2; sleep 1; cat /dev/ttyUSB2', wait=wait)

        return result.strip() if result else "(no response)"
    except Exception as e:
        return f"[Error] {e}"
    finally:
        t.close()


def check_telnet():
    """Return True if telnet port is reachable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        result = s.connect_ex((ROUTER_IP, TELNET_PORT))
        s.close()
        return result == 0
    except Exception:
        return False


# ─── Signal Parsing ───────────────────────────────────────────────────────────

def parse_signal():
    """Collect signal info via telnet AT commands."""
    try:
        t = TelnetSession(ROUTER_IP, TELNET_PORT)
        t.connect()

        # Run all AT commands in one shell pipeline so we get output cleanly
        batch = (
            "echo '---CESQ---' && ubus call modemd atcmd '{\"atcmd\":\"AT+CESQ\"}' 2>/dev/null || /etc/modemd/at.sh AT+CESQ 2>/dev/null; "
            "echo '---ZNLOCK---' && ubus call modemd atcmd '{\"atcmd\":\"AT+ZNLOCKBAND?\"}' 2>/dev/null || /etc/modemd/at.sh 'AT+ZNLOCKBAND?' 2>/dev/null; "
            "echo '---CEREG---' && ubus call modemd atcmd '{\"atcmd\":\"AT+CEREG?\"}' 2>/dev/null || /etc/modemd/at.sh 'AT+CEREG?' 2>/dev/null; "
            "echo '---ECSQ---' && ubus call modemd atcmd '{\"atcmd\":\"AT+ECSQ\"}' 2>/dev/null; "
        )
        all_out = t.run(batch, wait=6.0)
        t.close()

        # Parse sections
        cesq   = _section(all_out, "---CESQ---",   "---ZNLOCK---")
        znlock = _section(all_out, "---ZNLOCK---",  "---CEREG---")
        creg   = _section(all_out, "---CEREG---",   "---ECSQ---")
        ecsq   = _section(all_out, "---ECSQ---",    None)
        qeng   = ""

        raw = (
            f"=== AT+CESQ ===\n{cesq or '(empty)'}\n\n"
            f"=== AT+ZNLOCKBAND? ===\n{znlock or '(empty)'}\n\n"
            f"=== AT+CEREG? ===\n{creg or '(empty)'}\n\n"
            f"=== AT+ECSQ ===\n{ecsq or '(empty)'}\n\n"
            f"--- Full output ---\n{all_out}"
        )

        import re
        rsrp, rsrq = None, None

        # Parse +CESQ: rxlev,ber,rscp,ecno,rsrq,rsrp
        m = re.search(r'\+CESQ:\s*\d+,\d+,\d+,\d+,(\d+),(\d+)', all_out)
        if m:
            rsrq_raw = int(m.group(1))
            rsrp_raw = int(m.group(2))
            if rsrp_raw != 255:
                rsrp = rsrp_raw - 141
            if rsrq_raw != 255:
                rsrq = round((rsrq_raw / 2) - 19.5, 1)

        # Also try JSON response from ubus: {"result": "...+CESQ:..."}
        m2 = re.search(r'"result":\s*"[^"]*\+CESQ:\s*\d+,\d+,\d+,\d+,(\d+),(\d+)', all_out)
        if m2 and rsrp is None:
            rsrq_raw = int(m2.group(1))
            rsrp_raw = int(m2.group(2))
            if rsrp_raw != 255:
                rsrp = rsrp_raw - 141
            if rsrq_raw != 255:
                rsrq = round((rsrq_raw / 2) - 19.5, 1)

        return {"raw": raw, "rsrp": rsrp, "rsrq": rsrq}

    except Exception as e:
        return {"raw": f"[Error] {e}", "rsrp": None, "rsrq": None}


def _section(text, start_marker, end_marker):
    """Extract text between two markers."""
    if start_marker not in text:
        return ""
    start = text.index(start_marker) + len(start_marker)
    if end_marker and end_marker in text:
        end = text.index(end_marker)
        return text[start:end].strip()
    return text[start:].strip()


# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    return jsonify({"telnet": check_telnet(), "router": ROUTER_IP})


@app.route("/api/signal")
def api_signal():
    if not check_telnet():
        return jsonify({"error": "Telnet not available. Run setup first."})
    data = parse_signal()
    return jsonify(data)


@app.route("/api/debug")
def api_debug():
    """Raw telnet debug — run a simple command and show everything."""
    if not check_telnet():
        return jsonify({"result": "Telnet not available"})
    # Run a very simple command to verify shell is working
    result1 = telnet_run("echo hello_shell", wait=3.0)
    result2 = telnet_run("ubus list | head -5", wait=3.0)
    result3 = telnet_run("ls /etc/modemd/ 2>/dev/null || echo 'no /etc/modemd'", wait=3.0)
    return jsonify({
        "echo_test": result1,
        "ubus_list": result2,
        "modemd_dir": result3,
    })


@app.route("/api/band-lock", methods=["POST"])
def api_band_lock():
    if not check_telnet():
        return jsonify({"result": "[Error] Telnet not available. Run setup first."})

    body = request.get_json()
    value_str = body.get("value", "0")

    try:
        value = int(value_str)
        hex_value = hex(value) if value != 0 else "0"
    except ValueError:
        return jsonify({"result": "[Error] Invalid band value"})

    if value == 0:
        cmd = "AT+ZNLOCKBAND=0"
        label = "Unlocking all bands (Auto mode)"
    else:
        # Format: AT+ZNLOCKBAND=1,0,<hex>,0
        cmd = f"AT+ZNLOCKBAND=1,0,{hex_value},0"
        label = f"Locking to bands (mask={hex_value})"

    result = at_cmd(cmd, wait=3)

    # Reboot modem module to apply
    reboot_result = at_cmd("AT+CFUN=1,1", wait=4)

    output = (
        f"{label}\n"
        f"Command: {cmd}\n\n"
        f"Response:\n{result}\n\n"
        f"Modem restarting to apply changes...\n{reboot_result}\n\n"
        f"Wait ~15 seconds for the LTE connection to re-establish."
    )
    return jsonify({"result": output})


@app.route("/api/wifi", methods=["POST"])
def api_wifi():
    if not check_telnet():
        return jsonify({"result": "[Error] Telnet not available. Run setup first."})

    action = request.get_json().get("action", "status")

    if action == "status":
        cmd = "uci show wireless"
        result = telnet_run(cmd, wait=2)
    elif action == "enable_5g":
        result = telnet_run(
            "uci set wireless.radio1.disabled=0 && uci commit wireless && wifi restart",
            wait=5
        )
        result = "5GHz enabled.\n\n" + result
    elif action == "disable_5g":
        result = telnet_run(
            "uci set wireless.radio1.disabled=1 && uci commit wireless && wifi restart",
            wait=5
        )
        result = "5GHz disabled.\n\n" + result
    elif action == "enable_24g":
        result = telnet_run(
            "uci set wireless.radio0.disabled=0 && uci commit wireless && wifi restart",
            wait=5
        )
        result = "2.4GHz enabled.\n\n" + result
    elif action == "disable_24g":
        result = telnet_run(
            "uci set wireless.radio0.disabled=1 && uci commit wireless && wifi restart",
            wait=5
        )
        result = "2.4GHz disabled.\n\n" + result
    else:
        result = "Unknown action"

    return jsonify({"result": result})


@app.route("/api/change-imei", methods=["POST"])
def api_change_imei():
    if not check_telnet():
        return jsonify({"result": "[Error] Telnet not available. Run setup first."})

    import re
    body = request.get_json()
    imei = body.get("imei", "").strip()

    if not re.match(r'^\d{15}$', imei):
        return jsonify({"result": "[Error] Invalid IMEI — must be exactly 15 digits."})

    try:
        t = TelnetSession(ROUTER_IP, TELNET_PORT)
        t.connect()

        r1 = t.run("/etc/modemd/at.sh AT*PROD=2", wait=3)
        r2 = t.run("/etc/modemd/at.sh AT*MRD_IMEI=D", wait=3)
        r3 = t.run(f'echo -e \'at+egmr=1,7,"{imei}"\\r\' > /dev/ttyUSB2; sleep 1', wait=4)
        r4 = t.run("/etc/modemd/at.sh AT*PROD=0", wait=3)

        t.close()

        output = (
            f"Setting IMEI: {imei}\n\n"
            f"Step 1 — Enable production mode (AT*PROD=2):\n{r1 or '(ok)'}\n\n"
            f"Step 2 — Clear IMEI (AT*MRD_IMEI=D):\n{r2 or '(ok)'}\n\n"
            f"Step 3 — Write new IMEI (at+egmr=1,7):\n{r3 or '(ok)'}\n\n"
            f"Step 4 — Disable production mode (AT*PROD=0):\n{r4 or '(ok)'}\n\n"
            f"Done. Reboot the modem to apply the new IMEI."
        )
        return jsonify({"result": output})
    except Exception as e:
        return jsonify({"result": f"[Error] {e}"})


@app.route("/api/at", methods=["POST"])
def api_at():
    if not check_telnet():
        return jsonify({"result": "[Error] Telnet not available. Run setup first."})

    cmd = request.get_json().get("cmd", "").strip()
    if not cmd:
        return jsonify({"result": "[Error] No command provided"})

    # Determine if it's a shell command or AT command
    if cmd.upper().startswith("AT"):
        result = at_cmd(cmd, wait=3)
    else:
        result = telnet_run(cmd, wait=3)

    return jsonify({"result": result})


# ─── PWA Routes ───────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "D2-220G Manager",
        "short_name": "D2 Manager",
        "description": "Smart Bro GreenPacket D2-220G LTE Band & WiFi Manager",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#006400",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    })


@app.route("/sw.js")
def service_worker():
    sw = """
const CACHE = 'D2-220G-v1';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['/'])));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return; // API calls: network only
  e.respondWith(
    fetch(e.request)
      .then(r => {
        const clone = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return r;
      })
      .catch(() => caches.match(e.request))
  );
});
""".strip()
    return Response(sw, mimetype="application/javascript")


@app.route("/icon.svg")
def icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="80" fill="#0d1117"/>
  <rect x="56"  y="312" width="80" height="144" rx="10" fill="#006400"/>
  <rect x="172" y="232" width="80" height="224" rx="10" fill="#15803d"/>
  <rect x="288" y="152" width="80" height="304" rx="10" fill="#16a34a"/>
  <rect x="404" y="72"  width="80" height="384" rx="10" fill="#4ade80"/>
  <text x="256" y="500" font-family="'Segoe UI',system-ui,sans-serif" font-size="48"
        font-weight="700" fill="#4ade80" text-anchor="middle">D2</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


# ─── HTML Template ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>D2-220G Manager</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#006400">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="D2 Manager">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #e0e0e0; min-height: 100vh; }
  a { color: #58a6ff; }

  header {
    background: linear-gradient(135deg, #006400 0%, #004d00 100%);
    padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }
  header h1 { font-size: 1.25rem; color: #fff; font-weight: 600; }
  header .subtitle { font-size: 0.8rem; color: #a7f3a7; margin-top: 2px; }

  .badge {
    padding: 5px 14px; border-radius: 20px; font-size: 0.8rem; font-weight: 600;
    transition: all 0.3s;
  }
  .badge-ok  { background: #0a3d20; color: #4ade80; border: 1px solid #16a34a; }
  .badge-err { background: #3d0a0a; color: #f87171; border: 1px solid #dc2626; }
  .badge-loading { background: #1a2a3a; color: #60a5fa; border: 1px solid #3b82f6; }

  .container { max-width: 960px; margin: 24px auto; padding: 0 16px; }

  .card {
    background: #161b22; border: 1px solid #21262d; border-radius: 12px;
    padding: 20px; margin-bottom: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
  }
  .card-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #21262d;
  }
  .card-header h2 { font-size: 1rem; color: #58d68d; font-weight: 600; }

  .alert {
    padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 0.875rem;
    line-height: 1.5;
  }
  .alert-warn { background: #2d1b00; border: 1px solid #d97706; color: #fbbf24; }
  .alert-info { background: #0d1f3c; border: 1px solid #1d4ed8; color: #93c5fd; }
  .alert-success { background: #0a2d1a; border: 1px solid #16a34a; color: #86efac; }

  ol, ul { padding-left: 20px; }
  li { margin-bottom: 6px; font-size: 0.875rem; line-height: 1.6; }
  code {
    background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
    padding: 2px 8px; font-family: 'Courier New', monospace; font-size: 0.85rem;
    color: #f0883e; word-break: break-all;
  }

  .signal-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 12px;
  }
  .sig-box {
    background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 14px 10px; text-align: center;
  }
  .sig-box .sig-label { font-size: 0.7rem; color: #8b949e; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
  .sig-box .sig-value { font-size: 1.3rem; font-weight: 700; color: #58a6ff; }

  .band-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px;
    margin-bottom: 16px;
  }
  .band-btn {
    padding: 12px 8px; border: 2px solid #21262d; border-radius: 8px;
    background: #0d1117; color: #c9d1d9; cursor: pointer;
    transition: all 0.15s; text-align: center; font-size: 0.9rem; font-weight: 600;
    user-select: none;
  }
  .band-btn small { display: block; font-size: 0.72rem; color: #8b949e; font-weight: 400; margin-top: 3px; }
  .band-btn:hover { border-color: #58a6ff; background: #0d2040; }
  .band-btn.selected { border-color: #4ade80; background: #0a2d1a; color: #4ade80; }
  .band-btn.selected small { color: #86efac; }

  .flex { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .mt { margin-top: 14px; }

  .btn {
    padding: 8px 18px; border: none; border-radius: 6px; cursor: pointer;
    font-size: 0.875rem; font-weight: 600; transition: all 0.15s;
    white-space: nowrap;
  }
  .btn:active { transform: scale(0.97); }
  .btn-green  { background: #16a34a; color: #fff; }
  .btn-green:hover { background: #15803d; }
  .btn-blue   { background: #1d4ed8; color: #fff; }
  .btn-blue:hover { background: #1e40af; }
  .btn-red    { background: #dc2626; color: #fff; }
  .btn-red:hover { background: #b91c1c; }
  .btn-gray   { background: #21262d; color: #c9d1d9; }
  .btn-gray:hover { background: #30363d; }
  .btn-sm { padding: 5px 12px; font-size: 0.8rem; }

  .output-box {
    background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
    padding: 12px; font-family: 'Courier New', monospace; font-size: 0.78rem;
    color: #a5d6a7; min-height: 50px; max-height: 220px;
    overflow-y: auto; white-space: pre-wrap; word-break: break-word;
    line-height: 1.5;
  }
  .output-box.error { color: #f87171; }

  .at-row { display: flex; gap: 8px; align-items: center; }
  .at-input {
    flex: 1; background: #0d1117; border: 1px solid #30363d;
    color: #c9d1d9; padding: 8px 12px; border-radius: 6px;
    font-family: 'Courier New', monospace; font-size: 0.875rem;
  }
  .at-input:focus { outline: none; border-color: #58a6ff; }

  .preset-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px;
  }
  .preset-btn {
    padding: 10px; border: 1px solid #21262d; border-radius: 8px;
    background: #0d1117; cursor: pointer; text-align: center;
    transition: all 0.15s; color: #c9d1d9; font-size: 0.85rem;
  }
  .preset-btn small { display: block; color: #8b949e; font-size: 0.72rem; margin-top: 2px; }
  .preset-btn:hover { border-color: #4ade80; background: #0a2d1a; color: #4ade80; }

  .section-note { font-size: 0.8rem; color: #8b949e; margin-bottom: 14px; }

  .wifi-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .wifi-card {
    background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 14px; text-align: center;
  }
  .wifi-card h3 { font-size: 0.9rem; margin-bottom: 12px; color: #c9d1d9; }
  .wifi-card .flex { justify-content: center; }
</style>
</head>
<body>

<header>
  <div>
    <h1>Smart Bro D2-220G Manager</h1>
    <div class="subtitle">GreenPacket D2-220G &mdash; LTE Band Manager &amp; WiFi Control</div>
  </div>
  <span class="badge badge-loading" id="telnet-badge">Checking...</span>
</header>

<div class="container">

  <!-- Setup Card -->
  <div class="card" id="setup-card" style="display:none">
    <div class="card-header"><h2>One-Time Setup — Enable Telnet Access</h2></div>
    <div class="alert alert-warn">
      Telnet is not active yet. You need to enable it once via the router's Diagnostics page.
      It will reset after a router reboot.
    </div>
    <ol>
      <li>Open
        <a href="http://192.168.1.1" target="_blank">http://192.168.1.1</a> or
        <a href="http://smartbrosettings.net" target="_blank">smartbrosettings.net</a>
      </li>
      <li>Go to <b>Advanced Settings &rarr; System &rarr; Diagnostics</b> (or Ping Test)</li>
      <li>In the <b>ping host</b> field, paste exactly:
        <br><br>
        <code>127.0.0.1 &amp; busybox telnetd -p 2323 -l /bin/sh</code>
      </li>
      <li>Click <b>Ping</b> or <b>Start</b> — the page may show an error, that's normal</li>
      <li>Click <button onclick="checkStatus()" class="btn btn-green btn-sm">Check Connection</button> below</li>
    </ol>
    <div class="alert alert-info mt">
      This runs entirely on your local network (192.168.1.1). No data leaves your home.
    </div>
    <button onclick="checkStatus()" class="btn btn-blue mt">Check Connection</button>
  </div>

  <!-- Signal Stats -->
  <div class="card">
    <div class="card-header">
      <h2>Signal Statistics</h2>
      <button onclick="refreshSignal()" class="btn btn-gray btn-sm">Refresh</button>
    </div>
    <div class="signal-grid">
      <div class="sig-box">
        <div class="sig-label">Band</div>
        <div class="sig-value" id="sig-band">--</div>
      </div>
      <div class="sig-box">
        <div class="sig-label">RSRP</div>
        <div class="sig-value" id="sig-rsrp">--</div>
      </div>
      <div class="sig-box">
        <div class="sig-label">RSRQ</div>
        <div class="sig-value" id="sig-rsrq">--</div>
      </div>
      <div class="sig-box">
        <div class="sig-label">RSSI</div>
        <div class="sig-value" id="sig-rssi">--</div>
      </div>
      <div class="sig-box">
        <div class="sig-label">SINR</div>
        <div class="sig-value" id="sig-sinr">--</div>
      </div>
      <div class="sig-box">
        <div class="sig-label">Cell ID</div>
        <div class="sig-value" id="sig-cell" style="font-size:0.85rem">--</div>
      </div>
    </div>
    <div class="output-box mt" id="signal-raw">Run "Refresh" to load signal data...</div>
  </div>

  <!-- Band Locking -->
  <div class="card">
    <div class="card-header"><h2>LTE Band Locking</h2></div>
    <p class="section-note">
      Select one or more bands. Multiple selections enable Carrier Aggregation (CA) for faster speeds.
      Your current band is B1 (2100 MHz) with poor signal — try B28 (700 MHz) for better range.
    </p>
    <div class="band-grid" id="band-grid">
      <div class="band-btn" data-value="1" data-name="B1" onclick="toggleBand(this)">
        B1 <small>2100 MHz</small>
      </div>
      <div class="band-btn" data-value="4" data-name="B3" onclick="toggleBand(this)">
        B3 <small>1800 MHz</small>
      </div>
      <div class="band-btn" data-value="16" data-name="B5" onclick="toggleBand(this)">
        B5 <small>850 MHz</small>
      </div>
      <div class="band-btn" data-value="64" data-name="B7" onclick="toggleBand(this)">
        B7 <small>2600 MHz</small>
      </div>
      <div class="band-btn" data-value="524288" data-name="B8" onclick="toggleBand(this)">
        B8 <small>900 MHz</small>
      </div>
      <div class="band-btn" data-value="134217728" data-name="B28" onclick="toggleBand(this)">
        B28 <small>700 MHz APT</small>
      </div>
      <div class="band-btn" data-value="549755813888" data-name="B40" onclick="toggleBand(this)">
        B40 <small>TDD 2300 MHz</small>
      </div>
      <div class="band-btn" data-value="1099511627776" data-name="B41" onclick="toggleBand(this)">
        B41 <small>TDD 2500 MHz</small>
      </div>
    </div>

    <div class="flex">
      <button onclick="lockBands()" class="btn btn-green">Apply Band Lock</button>
      <button onclick="unlockAll()" class="btn btn-blue">Auto (All Bands)</button>
      <span id="selected-label" style="font-size:0.85rem; color:#8b949e">Nothing selected</span>
    </div>
    <div class="output-box mt" id="band-output"></div>
  </div>

  <!-- Quick Presets -->
  <div class="card">
    <div class="card-header"><h2>Quick Presets</h2></div>
    <p class="section-note">
      Smart Bro PH uses mainly B1, B3, B28, B40, B41.
      B28 (700 MHz) penetrates walls best. B1+B3 CA gives fastest speeds in urban areas.
    </p>
    <div class="preset-grid">
      <div class="preset-btn" onclick="applyPreset('1', 'B1 Only')">
        B1 Only <small>2100 MHz</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('4', 'B3 Only')">
        B3 Only <small>1800 MHz</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('134217728', 'B28 Only')">
        B28 Only <small>700 MHz — best range</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('524288', 'B8 Only')">
        B8 Only <small>900 MHz</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('5', 'B1 + B3 CA')">
        B1 + B3 CA <small>2100+1800 — fast</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('134217729', 'B1 + B28 CA')">
        B1 + B28 CA <small>2100+700</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('134217732', 'B3 + B28 CA')">
        B3 + B28 CA <small>1800+700</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('134217733', 'B1+B3+B28 CA')">
        B1+B3+B28 CA <small>triple band</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('549755813888', 'B40 Only')">
        B40 Only <small>TDD 2300</small>
      </div>
      <div class="preset-btn" onclick="applyPreset('1099511627776', 'B41 Only')">
        B41 Only <small>TDD 2500</small>
      </div>
    </div>
    <div class="output-box mt" id="preset-output"></div>
  </div>

  <!-- WiFi Settings -->
  <div class="card">
    <div class="card-header"><h2>WiFi Band Control</h2></div>
    <p class="section-note">
      D2-220G supports both 2.4 GHz and 5 GHz. Enable both for dual band.
    </p>
    <div class="wifi-grid">
      <div class="wifi-card">
        <h3>2.4 GHz Radio</h3>
        <div class="flex">
          <button onclick="wifiAction('enable_24g')" class="btn btn-green">Enable</button>
          <button onclick="wifiAction('disable_24g')" class="btn btn-red">Disable</button>
        </div>
      </div>
      <div class="wifi-card">
        <h3>5 GHz Radio</h3>
        <div class="flex">
          <button onclick="wifiAction('enable_5g')" class="btn btn-green">Enable</button>
          <button onclick="wifiAction('disable_5g')" class="btn btn-red">Disable</button>
        </div>
      </div>
    </div>
    <div class="flex mt">
      <button onclick="wifiAction('status')" class="btn btn-gray">Check WiFi Status</button>
    </div>
    <div class="output-box mt" id="wifi-output"></div>
  </div>

  <!-- IMEI Changer -->
  <div class="card">
    <div class="card-header"><h2>IMEI Changer</h2></div>
    <p class="section-note">
      Changes the modem IMEI so carrier-specific promos (e.g. Unli Data 599, Fam Surf) are recognised.
      The modem will reboot after applying.
    </p>
    <div class="alert alert-warn">
      Only use IMEIs that correspond to a compatible device for your SIM's carrier.
      Rebooting is required for the change to take effect.
    </div>

    <div class="at-row" style="margin-bottom:10px;">
      <input class="at-input" id="imei-input" type="text" maxlength="15"
        placeholder="Enter 15-digit IMEI"
        oninput="this.value=this.value.replace(/\D/g,'').slice(0,15)">
      <button onclick="changeImei()" class="btn btn-green">Apply IMEI</button>
    </div>

    <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:14px;">
      <div>
        <div style="font-size:0.8rem; color:#8b949e; margin-bottom:8px; font-weight:600;">
          Globe At Home Prepaid WiFi
        </div>
        <div class="preset-grid" style="grid-template-columns:1fr 1fr;">
          <div class="preset-btn" onclick="setImei('864434175846779')">864434175846779</div>
          <div class="preset-btn" onclick="setImei('864434428099424')">864434428099424</div>
          <div class="preset-btn" onclick="setImei('864434987045693')">864434987045693</div>
          <div class="preset-btn" onclick="setImei('864434327295537')">864434327295537</div>
          <div class="preset-btn" onclick="setImei('864434858159417')">864434858159417</div>
          <div class="preset-btn" onclick="setImei('864434993383377')">864434993383377</div>
          <div class="preset-btn" onclick="setImei('864434497881595')">864434497881595</div>
          <div class="preset-btn" onclick="setImei('864434894575725')">864434894575725</div>
          <div class="preset-btn" onclick="setImei('864434071600858')">864434071600858</div>
          <div class="preset-btn" onclick="setImei('864434866210616')">864434866210616</div>
          <div class="preset-btn" onclick="setImei('864434450688011')">864434450688011</div>
          <div class="preset-btn" onclick="setImei('864434379628080')">864434379628080</div>
          <div class="preset-btn" onclick="setImei('864434249429404')">864434249429404</div>
          <div class="preset-btn" onclick="setImei('864434207135522')">864434207135522</div>
          <div class="preset-btn" onclick="setImei('864434609887092')">864434609887092</div>
          <div class="preset-btn" onclick="setImei('864434323079133')">864434323079133</div>
          <div class="preset-btn" onclick="setImei('864434617642034')">864434617642034</div>
          <div class="preset-btn" onclick="setImei('864434844729380')">864434844729380</div>
          <div class="preset-btn" onclick="setImei('864434943570776')">864434943570776</div>
          <div class="preset-btn" onclick="setImei('864434361259100')">864434361259100</div>
        </div>
      </div>
      <div>
        <div style="font-size:0.8rem; color:#8b949e; margin-bottom:8px; font-weight:600;">
          SmartBro / PLDT Prepaid WiFi / Rocket SIM
        </div>
        <div class="preset-grid" style="grid-template-columns:1fr 1fr;">
          <div class="preset-btn" onclick="setImei('354386673492950')">354386673492950</div>
          <div class="preset-btn" onclick="setImei('354386594702891')">354386594702891</div>
          <div class="preset-btn" onclick="setImei('354386181662524')">354386181662524</div>
          <div class="preset-btn" onclick="setImei('354386341639446')">354386341639446</div>
          <div class="preset-btn" onclick="setImei('354386737044904')">354386737044904</div>
          <div class="preset-btn" onclick="setImei('354386803026397')">354386803026397</div>
          <div class="preset-btn" onclick="setImei('354386621803811')">354386621803811</div>
          <div class="preset-btn" onclick="setImei('354386863549510')">354386863549510</div>
          <div class="preset-btn" onclick="setImei('354386880326264')">354386880326264</div>
          <div class="preset-btn" onclick="setImei('354386566859158')">354386566859158</div>
          <div class="preset-btn" onclick="setImei('354386533124637')">354386533124637</div>
          <div class="preset-btn" onclick="setImei('354386603286977')">354386603286977</div>
          <div class="preset-btn" onclick="setImei('354386094999112')">354386094999112</div>
          <div class="preset-btn" onclick="setImei('354386074241634')">354386074241634</div>
          <div class="preset-btn" onclick="setImei('354386206553732')">354386206553732</div>
          <div class="preset-btn" onclick="setImei('354386592894955')">354386592894955</div>
          <div class="preset-btn" onclick="setImei('354386343872565')">354386343872565</div>
          <div class="preset-btn" onclick="setImei('354386557857856')">354386557857856</div>
          <div class="preset-btn" onclick="setImei('354386141890447')">354386141890447</div>
          <div class="preset-btn" onclick="setImei('354386938846156')">354386938846156</div>
        </div>
      </div>
    </div>

    <div class="output-box" id="imei-output"></div>
  </div>

  <!-- AT Terminal -->
  <div class="card">
    <div class="card-header"><h2>AT / Shell Terminal</h2></div>
    <p class="section-note">
      Commands starting with "AT" are sent via the modem daemon. Other commands run as shell.
    </p>
    <div class="at-row">
      <input class="at-input" id="at-cmd" type="text"
        placeholder="e.g. AT+ZNLOCKBAND?  or  cat /proc/cpuinfo"
        onkeydown="if(event.key==='Enter') sendAT()">
      <button onclick="sendAT()" class="btn btn-green">Send</button>
    </div>
    <div class="flex mt" style="gap:6px; flex-wrap:wrap;">
      <button onclick="quickAT('AT+ZNLOCKBAND?')" class="btn btn-gray btn-sm">Current Band Lock</button>
      <button onclick="quickAT('AT+CESQ')" class="btn btn-gray btn-sm">Signal Quality</button>
      <button onclick="quickAT('AT+CEREG?')" class="btn btn-gray btn-sm">Registration</button>
      <button onclick="quickAT('cat /proc/version')" class="btn btn-gray btn-sm">Kernel Version</button>
      <button onclick="quickAT('ubus list')" class="btn btn-gray btn-sm">ubus Services</button>
    </div>
    <div class="output-box mt" id="at-output"></div>
  </div>

</div>

<script>
// ─── State ──────────────────────────────────────────────────────────────────
let selectedValue = BigInt(0);
let isConnected = false;

// ─── Status ─────────────────────────────────────────────────────────────────
async function checkStatus() {
  const badge = document.getElementById('telnet-badge');
  badge.className = 'badge badge-loading';
  badge.textContent = 'Checking...';

  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    isConnected = d.telnet;

    if (isConnected) {
      badge.className = 'badge badge-ok';
      badge.textContent = 'Connected';
      document.getElementById('setup-card').style.display = 'none';
      refreshSignal();
    } else {
      badge.className = 'badge badge-err';
      badge.textContent = 'Not Connected';
      document.getElementById('setup-card').style.display = 'block';
    }
  } catch(e) {
    badge.className = 'badge badge-err';
    badge.textContent = 'Error';
  }
}

// ─── Signal ─────────────────────────────────────────────────────────────────
async function refreshSignal() {
  document.getElementById('signal-raw').textContent = 'Fetching signal data...';
  try {
    const r = await fetch('/api/signal');
    const d = await r.json();

    if (d.error) {
      document.getElementById('signal-raw').textContent = d.error;
      return;
    }
    if (d.rsrp !== null && d.rsrp !== undefined)
      document.getElementById('sig-rsrp').textContent = d.rsrp + ' dBm';
    if (d.rsrq !== null && d.rsrq !== undefined)
      document.getElementById('sig-rsrq').textContent = d.rsrq + ' dB';

    document.getElementById('signal-raw').textContent = d.raw || '(no data)';
  } catch(e) {
    document.getElementById('signal-raw').textContent = 'Error: ' + e.message;
  }
}

// ─── Band Locking ───────────────────────────────────────────────────────────
function toggleBand(el) {
  el.classList.toggle('selected');
  recalcBands();
}

function recalcBands() {
  selectedValue = BigInt(0);
  const names = [];
  document.querySelectorAll('.band-btn.selected').forEach(el => {
    selectedValue += BigInt(el.dataset.value);
    names.push(el.dataset.name);
  });
  document.getElementById('selected-label').textContent =
    names.length ? 'Selected: ' + names.join(' + ') : 'Nothing selected';
}

async function lockBands() {
  if (selectedValue === BigInt(0)) {
    document.getElementById('band-output').textContent = 'Please select at least one band first.';
    return;
  }
  await applyBandValue(selectedValue.toString(), document.getElementById('band-output'));
}

async function unlockAll() {
  await applyBandValue('0', document.getElementById('band-output'));
}

async function applyPreset(value, label) {
  const el = document.getElementById('preset-output');
  el.textContent = 'Applying: ' + label + '...';
  await applyBandValue(value, el);
}

async function applyBandValue(value, outEl) {
  outEl.textContent = 'Sending command...';
  try {
    const r = await fetch('/api/band-lock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value })
    });
    const d = await r.json();
    outEl.textContent = d.result;
  } catch(e) {
    outEl.textContent = 'Error: ' + e.message;
  }
}

// ─── WiFi ───────────────────────────────────────────────────────────────────
async function wifiAction(action) {
  const out = document.getElementById('wifi-output');
  out.textContent = 'Running...';
  try {
    const r = await fetch('/api/wifi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    const d = await r.json();
    out.textContent = d.result;
  } catch(e) {
    out.textContent = 'Error: ' + e.message;
  }
}

// ─── AT Terminal ────────────────────────────────────────────────────────────
function quickAT(cmd) {
  document.getElementById('at-cmd').value = cmd;
  sendAT();
}

async function sendAT() {
  const cmd = document.getElementById('at-cmd').value.trim();
  const out = document.getElementById('at-output');
  if (!cmd) return;
  out.textContent = '> ' + cmd + '\\n\\nSending...';
  try {
    const r = await fetch('/api/at', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd })
    });
    const d = await r.json();
    out.textContent = '> ' + cmd + '\\n\\n' + d.result;
  } catch(e) {
    out.textContent = 'Error: ' + e.message;
  }
}

// ─── IMEI Changer ────────────────────────────────────────────────────────────
function setImei(imei) {
  document.getElementById('imei-input').value = imei;
}

async function changeImei() {
  const imei = document.getElementById('imei-input').value.trim();
  const out = document.getElementById('imei-output');
  if (imei.length !== 15 || !/^\d{15}$/.test(imei)) {
    out.textContent = '[Error] IMEI must be exactly 15 digits.';
    out.className = 'output-box error';
    return;
  }
  out.className = 'output-box';
  out.textContent = 'Sending IMEI change sequence...';
  try {
    const r = await fetch('/api/change-imei', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ imei })
    });
    const d = await r.json();
    out.textContent = d.result;
    if (d.result.startsWith('[Error]')) out.className = 'output-box error';
  } catch(e) {
    out.textContent = 'Error: ' + e.message;
    out.className = 'output-box error';
  }
}

// ─── Init ────────────────────────────────────────────────────────────────────
checkStatus();
setInterval(checkStatus, 60000); // re-check every 60s

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>
</body>
</html>"""

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("=" * 55)
    print("  Smart Bro D2-220G Manager")
    print("=" * 55)
    print(f"  Router IP    : {ROUTER_IP}")
    print(f"  Telnet Port  : {TELNET_PORT}")
    print()
    print("  Open in browser: http://localhost:5000")
    print()
    print("  SETUP (one-time per router reboot):")
    print("  1. Go to Advanced Settings > System > Diagnostics")
    print("  2. Ping host field: 127.0.0.1 & busybox telnetd -p 2323 -l /bin/sh")
    print("  3. Click Ping/Start")
    print("=" * 55)
    print()

    app.run(host="0.0.0.0", port=5000, debug=False)
