#!/usr/bin/env python3
"""
ANTI OWNER RESCUE CONSOLE (Trạm Cứu Hộ / Break-Glass CLI)
Commands:
  python3 anti_rescue.py status      - Full health and system diagnostics
  python3 anti_rescue.py killswitch  - Emergency halt of all workers
  python3 anti_rescue.py recover     - Resume and restart core supervisor/sentinel
  python3 anti_rescue.py pause       - Engage global pause
  python3 anti_rescue.py resume      - Remove global pause
  python3 anti_rescue.py logs <name> - View tail of logs (supervisor, aider, browser, gateway)
"""

import sys
import os
import json
from pathlib import Path
import urllib.request
import urllib.error

TOKEN_FILE = Path(__file__).parent / "owner_break_glass.token"
if TOKEN_FILE.exists():
    BREAK_GLASS_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BREAK_GLASS_TOKEN = os.environ.get("ANTI_BREAK_GLASS_TOKEN", "")

DEFAULT_HOST = os.environ.get("ANTI_GATEWAY_HOST", "10.8.0.1:20145")

def call_api(endpoint: str, method: str = "GET", data: dict = None, host: str = DEFAULT_HOST):
    url = f"http://{host}{endpoint}"
    headers = {
        "Authorization": f"Bearer {BREAK_GLASS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, headers=headers, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        # Fallback helper notice
        print(f"[-] Connection to {url} failed: {e}")
        print(f"[!] Tip: If WireGuard is not yet up on this Mac, you can test via SSH port forward:")
        print(f"    ssh -L 20145:127.0.0.1:20145 root@96.9.225.171 -N &")
        print(f"    ANTI_GATEWAY_HOST=127.0.0.1:20145 python3 anti_rescue.py <command>")
        sys.exit(1)

def print_header(title):
    print("\n" + "=" * 60)
    print(f"  ANTI RESCUE CONSOLE :: {title}")
    print("=" * 60)

def cmd_status():
    print_header("SYSTEM DIAGNOSTICS")
    res = call_api("/api/v1/owner/diagnostics")
    print(f"Role:         {res.get('role')}")
    print(f"Uptime:       {res.get('uptime')}")
    print("\n[Service States]")
    for svc, state in res.get("services", {}).items():
        symbol = "✅" if state == "active" else "❌"
        print(f"  {symbol} {svc:<30}: {state}")
    print("\n[Memory]")
    print(res.get("memory_mb", "").strip())
    print("\n[Disk]")
    print(res.get("disk", "").strip())
    print("\n[WireGuard Mesh Peers]")
    print(res.get("wireguard", "").strip())

def cmd_killswitch():
    print_header("EMERGENCY KILLSWITCH")
    confirm = input("Are you sure you want to halt all agents? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return
    res = call_api("/api/v1/owner/killswitch", method="POST")
    print(json.dumps(res, indent=2))

def cmd_recover():
    print_header("EMERGENCY RECOVERY")
    res = call_api("/api/v1/owner/recover", method="POST")
    print(json.dumps(res, indent=2))

def cmd_pause():
    res = call_api("/api/v1/system/pause", method="POST")
    print(json.dumps(res, indent=2))

def cmd_resume():
    res = call_api("/api/v1/system/resume", method="POST")
    print(json.dumps(res, indent=2))

def cmd_logs(target="supervisor"):
    res = call_api(f"/api/v1/logs?target={target}&lines=30")
    print_header(f"LOGS: {target}")
    print(res.get("content", ""))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "status":
        cmd_status()
    elif cmd == "killswitch":
        cmd_killswitch()
    elif cmd == "recover":
        cmd_recover()
    elif cmd == "pause":
        cmd_pause()
    elif cmd == "resume":
        cmd_resume()
    elif cmd == "logs":
        target = sys.argv[2] if len(sys.argv) > 3 else "supervisor"
        cmd_logs(target)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
