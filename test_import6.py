import sys
sys.stdout.reconfigure(line_buffering=True)
print("1. testing ollama...", flush=True)
import ollama
print("2. ollama OK", flush=True)
from inference import OLLAMA_HOST, AGENT_CONFIGS
print("3. inference OK", flush=True)
import monitor
print("4. monitor OK", flush=True)
