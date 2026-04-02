import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Directly exec the _types.py content
print("1. importing ollama._types directly...", flush=True)
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
sys.path.insert(0, pkg)

# Read and exec line by line to find the hang
with open(os.path.join(pkg, "_types.py")) as f:
    lines = f.readlines()

print(f"   {len(lines)} lines total", flush=True)

# Try importing the module directly
import importlib.util
spec = importlib.util.spec_from_file_location("ollama._types", os.path.join(pkg, "_types.py"))
print("2. spec created", flush=True)
mod = importlib.util.module_from_spec(spec)
print("3. module created, executing...", flush=True)
spec.loader.exec_module(mod)
print("4. module executed OK", flush=True)
