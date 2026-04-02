import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Find ollama package location
import importlib.metadata
loc = importlib.metadata.distribution("ollama").locate_file("")
print(f"ollama location: {loc}", flush=True)

# Try importing the submodules
try:
    print("importing ollama._types...", flush=True)
    import ollama._types
    print("  OK", flush=True)
except Exception as e:
    print(f"  FAIL: {e}", flush=True)

try:
    print("importing ollama._client...", flush=True)
    import ollama._client
    print("  OK", flush=True)
except Exception as e:
    print(f"  FAIL: {e}", flush=True)

print("done", flush=True)
