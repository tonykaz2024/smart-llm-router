"""Start Smart Router as a persistent background process"""
import subprocess
import sys
import os
import time

ROUTER_SCRIPT = os.path.join(os.path.dirname(__file__), "smart-router.py")
PORT = 4001

def is_running():
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=2)
        return True
    except Exception:
        return False

if is_running():
    print(f"Router already running on port {PORT}")
    sys.exit(0)

proc = subprocess.Popen(
    [sys.executable, ROUTER_SCRIPT, "--port", str(PORT)],
    stdout=open(os.path.join(os.path.dirname(__file__), "router.log"), "a"),
    stderr=subprocess.STDOUT,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        if os.name == "nt" else 0,
)

time.sleep(2)
if is_running():
    print(f"Router started on port {PORT} (PID: {proc.pid})")
    pid_file = os.path.join(os.path.dirname(__file__), "router.pid")
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
else:
    print(f"Router failed to start. Check F:\\llm-router\\router.log")
    sys.exit(1)
