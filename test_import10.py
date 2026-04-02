import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Read the __init__.py of ollama._types to see what it imports
pkg_path = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
init_file = os.path.join(pkg_path, "_types.py")
if os.path.exists(init_file):
    with open(init_file) as f:
        content = f.read()
    # Show first 50 lines
    for i, line in enumerate(content.split('\n')[:50]):
        print(f"{i+1}: {line}", flush=True)
else:
    # Check directory structure
    for f in os.listdir(pkg_path):
        print(f"  {f}", flush=True)
