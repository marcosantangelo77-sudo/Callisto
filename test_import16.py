import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(15)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

print("1. ipaddress...", flush=True)
import ipaddress
print("2. anyio...", flush=True)
import anyio
print("3. anyio OK", flush=True)
