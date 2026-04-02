import sys, os, threading, time
sys.stdout.reconfigure(line_buffering=True)

def watchdog():
    time.sleep(15)
    print("TIMEOUT", flush=True)
    os._exit(1)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

print("1. pydantic...", flush=True)
import pydantic
print(f"   v{pydantic.__version__}", flush=True)

print("2. pydantic.json_schema...", flush=True)
from pydantic.json_schema import JsonSchemaValue
print("   OK", flush=True)

print("3. typing_extensions...", flush=True)
from typing_extensions import Annotated, Literal
print("   OK", flush=True)

print("4. pydantic ByteSize...", flush=True)
from pydantic import ByteSize, ConfigDict, Field, model_serializer
print("   OK", flush=True)

print("All OK!", flush=True)
