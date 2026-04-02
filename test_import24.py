import sys, os
sys.stdout.reconfigure(line_buffering=True)
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"
with open(os.path.join(pkg, "_client.py")) as f:
    content = f.read()

# Find _parse_host function
import re
for m in re.finditer(r'def _parse_host', content):
    start = m.start()
    preceding = content[:start].count('\n')
    lines = content.split('\n')
    for i in range(preceding, min(preceding + 40, len(lines))):
        print(f"{i+1}: {lines[i]}", flush=True)
    break
