import uuid
import datetime
import json
import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
import redis
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)

app = FastAPI(title="Agent Workflow Runner", version="0.3.0")

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis-service"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True,
)

QUEUE_NAME = "task_queue"

# ── Prometheus metrics ──────────────────────────────────────────
# Counter: only ever goes up — total tasks submitted since startup
TASKS_SUBMITTED = Counter(
    "tasks_submitted_total",
    "Total number of tasks submitted",
    ["task_type"],       # label: lets you split by research/summarize/etc
)

# Counter: completed tasks, split by status
TASKS_COMPLETED = Counter(
    "tasks_completed_total",
    "Total number of tasks completed",
    ["task_type", "status"],   # status = complete | failed
)

# Histogram: measures distribution of values — great for latency
# Buckets define the boundary points for the histogram bars
TASK_DURATION = Histogram(
    "task_duration_seconds",
    "Time from submission to completion",
    ["task_type"],
    buckets=[5, 10, 30, 60, 120, 300],   # 5s, 10s, 30s... up to 5min
)

# Gauge: can go up or down — current queue depth
QUEUE_DEPTH = Gauge(
    "task_queue_depth",
    "Number of tasks currently in the Redis queue",
)
# ───────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    type: str
    input: str

class TaskResponse(BaseModel):
    task_id: str
    status: str
    created_at: str

@app.post("/tasks", response_model=TaskResponse, status_code=202)
def submit_task(req: TaskRequest):
    task_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    task = {
        "task_id": task_id,
        "type": req.type,
        "input": req.input,
        "status": "queued",
        "result": None,
        "created_at": now,
        "submitted_at_ts": time.time(),   # unix timestamp for duration calc
    }

    r.set(f"task:{task_id}", json.dumps(task))
    r.lpush(QUEUE_NAME, task_id)

    # Increment counter with the task_type label
    TASKS_SUBMITTED.labels(task_type=req.type).inc()

    # Update queue depth gauge
    QUEUE_DEPTH.set(r.llen(QUEUE_NAME))

    return TaskResponse(task_id=task_id, status="queued", created_at=now)

@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    raw = r.get(f"task:{task_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Task not found")

    task = json.loads(raw)

    # Record metrics when we first see a terminal state
    if task["status"] in ("complete", "failed") and "duration_recorded" not in task:
        
        # Use worker-recorded duration if available, otherwise fall back to wall clock
        if "duration_seconds" in task:
            duration = task["duration_seconds"]
        else:
            duration = time.time() - task.get("submitted_at_ts", time.time())

        TASK_DURATION.labels(task_type=task["type"]).observe(duration)
        TASKS_COMPLETED.labels(
            task_type=task["type"],
            status=task["status"],
        ).inc()

        task["duration_recorded"] = True
        r.set(f"task:{task_id}", json.dumps(task))
        print(f"[{task_id}] recorded duration={duration:.1f}s status={task['status']}")

    return task

@app.get("/metrics")
def metrics():
    # Prometheus scrapes this endpoint every 15 seconds
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/healthz")
def health():
    try:
        r.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="Redis unavailable")