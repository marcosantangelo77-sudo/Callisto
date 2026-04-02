import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

print("1. importing ollama._client...", flush=True)
from ollama._client import AsyncClient, Client
print("2. _client imported OK", flush=True)

print("3. creating Client()...", flush=True)
c = Client()
print("4. Client created OK", flush=True)
