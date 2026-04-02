import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(30)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Import everything that _client.py imports, in order
print("1. standard libs...", flush=True)
import ipaddress, json, platform, urllib.parse
from hashlib import sha256
from pathlib import Path

print("2. anyio...", flush=True)
import anyio

print("3. pydantic.json_schema...", flush=True)
from pydantic.json_schema import JsonSchemaValue

print("4. httpx...", flush=True)
import httpx

print("5. importlib.metadata...", flush=True)
from importlib import metadata
v = metadata.version('ollama')
print(f"   ollama={v}", flush=True)

# Now try importing the actual _types directly (bypassing __init__)
print("6. loading _types directly...", flush=True)
import importlib.util
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
spec = importlib.util.spec_from_file_location("ollama._types", os.path.join(pkg, "_types.py"))
types_mod = importlib.util.module_from_spec(spec)
sys.modules["ollama._types"] = types_mod
spec.loader.exec_module(types_mod)
print("   OK", flush=True)

print("7. loading _utils directly...", flush=True)
spec2 = importlib.util.spec_from_file_location("ollama._utils", os.path.join(pkg, "_utils.py"))
utils_mod = importlib.util.module_from_spec(spec2)
sys.modules["ollama._utils"] = utils_mod
spec2.loader.exec_module(utils_mod)
print("   OK", flush=True)

print("8. loading _client directly...", flush=True)
spec3 = importlib.util.spec_from_file_location("ollama._client", os.path.join(pkg, "_client.py"))
client_mod = importlib.util.module_from_spec(spec3)
sys.modules["ollama._client"] = client_mod
spec3.loader.exec_module(client_mod)
print("   OK", flush=True)

print("9. creating Client()...", flush=True)
c = client_mod.Client()
print("   OK", flush=True)

print("ALL OK", flush=True)
