import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(15)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

print("1. httpx...", flush=True)
import httpx
print(f"2. httpx OK v{httpx.__version__}", flush=True)
