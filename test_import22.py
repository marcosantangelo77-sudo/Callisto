import sys, os
sys.stdout.reconfigure(line_buffering=True)
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
with open(os.path.join(pkg, "_client.py")) as f:
    content = f.read()

# Find Client class and its __init__
import re
# Find "class Client" and print surrounding code
for m in re.finditer(r'class Client\b', content):
    start = m.start()
    # Get ~60 lines from start
    preceding = content[:start].count('\n')
    lines = content.split('\n')
    for i in range(preceding, min(preceding + 80, len(lines))):
        print(f"{i+1}: {lines[i]}", flush=True)
    break
