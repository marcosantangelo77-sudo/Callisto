import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Check what OLLAMA_HOST is set to
print(f"OLLAMA_HOST={os.getenv('OLLAMA_HOST', 'NOT SET')}", flush=True)

# Try creating httpx.Client directly
import httpx
print("1. Creating httpx.Client with default timeout...", flush=True)
c = httpx.Client(base_url="http://127.0.0.1:11434", timeout=None)
print("2. httpx.Client created OK", flush=True)
c.close()
print("3. Closed", flush=True)
