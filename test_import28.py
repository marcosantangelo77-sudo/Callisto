import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(30)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Enable verbose imports
sys.flags  # just to check
print("Starting verbose import...", flush=True)

# Monkey-patch httpx.Client.__init__ to add tracing
import httpx
_orig_init = httpx.Client.__init__

def traced_init(self, *args, **kwargs):
    print(f"  httpx.Client.__init__ called with base_url={kwargs.get('base_url', args[0] if args else 'N/A')}", flush=True)
    _orig_init(self, *args, **kwargs)
    print("  httpx.Client.__init__ done", flush=True)

httpx.Client.__init__ = traced_init

print("Importing ollama...", flush=True)
import ollama
print("DONE!", flush=True)
