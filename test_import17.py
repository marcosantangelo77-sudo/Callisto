import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

print("1. ollama._utils...", flush=True)
from ollama._utils import convert_function_to_tool
print("2. OK", flush=True)

# Read more of _client.py to see what else it imports
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
with open(os.path.join(pkg, "_client.py")) as f:
    lines = f.readlines()
    for i, line in enumerate(lines[40:80], 41):
        print(f"{i}: {line.rstrip()}", flush=True)
