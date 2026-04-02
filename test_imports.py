import sys
print(f"Python: {sys.version}")
print("Testing imports...")

try:
    import fastapi
    print(f"  fastapi: OK ({fastapi.__version__})")
except Exception as e:
    print(f"  fastapi: FAILED ({e})")

try:
    import uvicorn
    print(f"  uvicorn: OK")
except Exception as e:
    print(f"  uvicorn: FAILED ({e})")

try:
    import aiosqlite
    print(f"  aiosqlite: OK")
except Exception as e:
    print(f"  aiosqlite: FAILED ({e})")

try:
    import numpy
    print(f"  numpy: OK ({numpy.__version__})")
except Exception as e:
    print(f"  numpy: FAILED ({e})")

try:
    import aiohttp
    print(f"  aiohttp: OK")
except Exception as e:
    print(f"  aiohttp: FAILED ({e})")

try:
    import tracemalloc
    print(f"  tracemalloc: OK")
except Exception as e:
    print(f"  tracemalloc: FAILED ({e})")

print("All core imports tested")
