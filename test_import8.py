import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(15)
    print("TIMEOUT: still hanging after 15s", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

# Try importing ollama with explicit OLLAMA_HOST
os.environ["OLLAMA_HOST"] = "http://127.0.0.1:11434"
print("importing ollama (OLLAMA_HOST=127.0.0.1:11434)...", flush=True)
import ollama
print(f"ollama imported OK", flush=True)
