import sys, os
sys.stdout.reconfigure(line_buffering=True)
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"

# Read _utils.py 
with open(os.path.join(pkg, "_utils.py")) as f:
    for i, line in enumerate(f.readlines()[:30], 1):
        print(f"{i}: {line.rstrip()}", flush=True)
