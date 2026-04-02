import sys, os
sys.stdout.reconfigure(line_buffering=True)
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
with open(os.path.join(pkg, "_client.py")) as f:
    for i, line in enumerate(f.readlines()[:40], 1):
        print(f"{i}: {line.rstrip()}", flush=True)
