import sys
sys.stdout.reconfigure(line_buffering=True)

modules = [
    "asyncio", "gc", "logging", "os", "tracemalloc",
    "aiosqlite", "dotenv", "fastapi", "pydantic",
    "agp", "logging_config", "memory", "monitor",
    "orchestrator", "task_queue",
]

for m in modules:
    try:
        __import__(m)
        print(f"OK: {m}", flush=True)
    except Exception as e:
        print(f"FAIL: {m} -> {e}", flush=True)
