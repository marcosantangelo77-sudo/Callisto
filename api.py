"""
FastAPI REST layer for Callisto.

Endpoints for task submission, session retrieval, world queries, and health checks.
Runs on port 8420.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agp import Domain
from logging_config import setup_logging
from memory import MemoryStore
from monitor import HealthMonitor
from orchestrator import Orchestrator
from task_queue import TaskQueue

load_dotenv()

setup_logging()
logger = logging.getLogger("callisto.api")

CALLISTO_PORT = int(os.getenv("CALLISTO_PORT", "8420"))

# Shared state
memory: Optional[MemoryStore] = None
queue: Optional[TaskQueue] = None
orchestrator_instance: Optional[Orchestrator] = None
monitor: Optional[HealthMonitor] = None
worker_task: Optional[asyncio.Task] = None


async def task_worker():
    """Background worker: polls task queue and runs AGP sessions."""
    while True:
        try:
            task = await queue.get_next()
            if task is None:
                await asyncio.sleep(2)
                continue

            task_id = task["task_id"]
            logger.info(f"Worker picked up task {task_id}: {task['query']}")

            try:
                result = await orchestrator_instance.run_session(task["query"])
                session_id = result.get("session_id")
                await queue.complete_task(task_id, result, session_id=session_id)
                logger.info(f"Task {task_id} completed, session {session_id}")
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}")
                await queue.fail_task(task_id, str(e))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle manager."""
    global memory, queue, orchestrator_instance, monitor, worker_task

    # Startup
    memory = MemoryStore()
    await memory.initialize()

    queue = TaskQueue()
    await queue.initialize()

    orchestrator_instance = Orchestrator(memory)
    monitor = HealthMonitor()
    await monitor.start()

    worker_task = asyncio.create_task(task_worker())
    logger.info(f"Callisto API started on port {CALLISTO_PORT}")

    yield

    # Shutdown
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    await monitor.stop()
    await queue.close()
    await memory.close()
    logger.info("Callisto API shut down")


app = FastAPI(
    title="Callisto",
    description="Autonomous multi-agent reasoning system governed by the Aluft Gianne Protocol",
    version="0.1.0",
    lifespan=lifespan,
)


class TaskSubmission(BaseModel):
    query: str
    priority: int = 0


class TaskResponse(BaseModel):
    task_id: int


@app.post("/task", response_model=TaskResponse)
async def submit_task(submission: TaskSubmission):
    """Submit a query for AGP session processing."""
    task_id = await queue.submit_task(submission.query, submission.priority)
    return TaskResponse(task_id=task_id)


@app.get("/task/{task_id}")
async def get_task(task_id: int):
    """Get task status and result."""
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get a sealed AGP session with full provenance."""
    session = await memory.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/world/{domain}")
async def query_world(
    domain: str,
    keyword: Optional[str] = None,
    min_confidence: Optional[float] = None,
    limit: int = 50,
):
    """Query a domain world."""
    try:
        domain_enum = Domain(domain.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain. Must be one of: {[d.value for d in Domain]}",
        )
    results = await memory.query_world(
        domain_enum, keyword=keyword, min_confidence=min_confidence, limit=limit
    )
    return {"domain": domain_enum.value, "count": len(results), "entries": results}


@app.get("/health")
async def health():
    """Health check for all three agents."""
    return monitor.get_status()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=CALLISTO_PORT, reload=False)
