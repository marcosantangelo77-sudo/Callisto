import sys, os, threading, time, platform
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(30)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

import httpx

# Reproduce BaseClient.__init__ exactly
host = "http://localhost:11434"
follow_redirects = True
timeout = None
_headers = {
    'content-type': 'application/json',
    'accept': 'application/json',
    'user-agent': f'ollama-python/0.6.1 (AMD64 windows) Python/{platform.python_version()}',
}

print("1. Creating httpx.Client with exact BaseClient params...", flush=True)
c = httpx.Client(
    base_url=host,
    follow_redirects=follow_redirects,
    timeout=timeout,
    headers=_headers,
)
print("2. OK", flush=True)
c.close()
print("3. Closed", flush=True)

# Now try with host=None (default)
host2 = os.getenv('OLLAMA_HOST')
print(f"4. OLLAMA_HOST={host2}", flush=True)
print("5. Creating with OLLAMA_HOST...", flush=True)
c2 = httpx.Client(
    base_url=host2,
    follow_redirects=True,
    timeout=None,
    headers=_headers,
)
print("6. OK", flush=True)
c2.close()
print("DONE", flush=True)
