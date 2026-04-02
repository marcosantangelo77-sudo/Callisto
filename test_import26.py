import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

import httpx
print("1. httpx.Client(base_url='http://localhost:11434', timeout=None)...", flush=True)
c = httpx.Client(base_url="http://localhost:11434", timeout=None)
print("2. OK", flush=True)
c.close()

# Now the real test — use _parse_host with the env var
print("3. Testing _parse_host('http://localhost:11434')...", flush=True)
# Simulate what ollama does
import importlib.util
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
spec = importlib.util.spec_from_file_location("ollama._types", os.path.join(pkg, "_types.py"))
types_mod = importlib.util.module_from_spec(spec)
sys.modules["ollama._types"] = types_mod
spec.loader.exec_module(types_mod)

spec2 = importlib.util.spec_from_file_location("ollama._utils", os.path.join(pkg, "_utils.py"))
utils_mod = importlib.util.module_from_spec(spec2)
sys.modules["ollama._utils"] = utils_mod
spec2.loader.exec_module(utils_mod)

spec3 = importlib.util.spec_from_file_location("ollama._client", os.path.join(pkg, "_client.py"))
client_mod = importlib.util.module_from_spec(spec3)
sys.modules["ollama._client"] = client_mod
spec3.loader.exec_module(client_mod)
print("4. Modules loaded", flush=True)

parsed = client_mod._parse_host("http://localhost:11434")
print(f"5. Parsed host: {parsed}", flush=True)

print("6. Creating Client(host='http://localhost:11434')...", flush=True)
c2 = client_mod.Client(host="http://localhost:11434")
print("7. Client created OK", flush=True)
