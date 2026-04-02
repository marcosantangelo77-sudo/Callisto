import sys, os, threading, time, platform
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(30)
    print("\nTIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

import httpx

# Exactly what BaseClient does:
host = os.getenv('OLLAMA_HOST', 'http://127.0.0.1:11434')
print(f"host={host}", flush=True)

__version__ = '0.6.1'
headers = {
    'content-type': 'application/json',
    'accept': 'application/json',
    'user-agent': f'ollama-python/{__version__} ({platform.machine()} {platform.system().lower()}) Python/{platform.python_version()}',
}

print("Creating Client step by step...", flush=True)

# Step 1: parse host - no network
print("  1. parse host...", flush=True)
# Inline _parse_host for testing
parsed = host.rstrip('/')
if not parsed.startswith(('http://', 'https://')):
    parsed = f'http://{parsed}'
print(f"     parsed={parsed}", flush=True)

# Step 2: create httpx.Client
print("  2. httpx.Client()...", flush=True)
c = httpx.Client(
    base_url=parsed,
    follow_redirects=True,
    timeout=None,
    headers=headers,
)
print("  3. created OK", flush=True)

# Step 3: test a simple request
print("  4. testing health...", flush=True)
try:
    r = c.get("/api/version")
    print(f"  5. response: {r.status_code} {r.text[:100]}", flush=True)
except Exception as e:
    print(f"  5. error: {e}", flush=True)

c.close()
print("ALL OK", flush=True)
