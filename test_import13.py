import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(20)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
with open(os.path.join(pkg, "__init__.py")) as f:
    for i, line in enumerate(f.readlines()[:60], 1):
        print(f"{i}: {line.rstrip()}", flush=True)
