import sys
sys.stdout.reconfigure(line_buffering=True)
print("testing pip show ollama...", flush=True)
import importlib.metadata
v = importlib.metadata.version("ollama")
print(f"ollama package version: {v}", flush=True)

# Try importing with timeout indicator
import signal, threading

def watchdog():
    import time
    time.sleep(10)
    print("TIMEOUT: import ollama hung for 10+ seconds", flush=True)
    import os
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

print("importing ollama...", flush=True)
import ollama
print("ollama imported OK", flush=True)
