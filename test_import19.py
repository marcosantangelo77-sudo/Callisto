import sys, os
sys.stdout.reconfigure(line_buffering=True)
pkg = r"C:\Users\marco\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\ollama"

# Read _client.py - look for module-level code (not in class/def)
with open(os.path.join(pkg, "_client.py")) as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}", flush=True)
# Find lines that are NOT inside class/def (module-level)
indent_level = 0
for i, line in enumerate(lines, 1):
    stripped = line.rstrip()
    if not stripped or stripped.startswith('#'):
        continue
    # Module-level code has 0 indentation
    if not line.startswith(' ') and not line.startswith('\t'):
        if not stripped.startswith(('class ', 'def ', 'async def', '@')):
            print(f"{i}: {stripped}", flush=True)
