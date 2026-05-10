import os
import json
import redis
import anthropic
import datetime
import time
import urllib.request

TASK_ID = os.environ["TASK_ID"]
REDIS_HOST = os.environ.get("REDIS_HOST", "redis-service")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FASTAPI_HOST = os.environ.get("FASTAPI_HOST", "fastapi-service")

r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def update_status(status: str, result: str = None, error: str = None, duration_seconds: float = None):
    raw = r.get(f"task:{TASK_ID}")
    if not raw:
        print(f"ERROR: task:{TASK_ID} not found in Redis")
        return
    task = json.loads(raw)
    task["status"] = status
    task["updated_at"] = datetime.datetime.utcnow().isoformat()
    if result:
        task["result"] = result
    if error:
        task["error"] = error
    if duration_seconds is not None:
        task["duration_seconds"] = duration_seconds   # worker records actual duration
    r.set(f"task:{TASK_ID}", json.dumps(task))
    print(f"[{TASK_ID}] status → {status}")

def notify_completion(task_id: str):
    """
    Call the FastAPI endpoint so it records completion metrics.
    Simple HTTP call — no extra dependencies needed.
    """
    try:
        url = f"http://{FASTAPI_HOST}/tasks/{task_id}"
        req = urllib.request.urlopen(url, timeout=5)
        print(f"[{TASK_ID}] notified FastAPI of completion")
    except Exception as e:
        # Non-fatal — metrics miss is better than task failure
        print(f"[{TASK_ID}] WARNING: could not notify FastAPI: {e}")

def run_agent(task_input: str, task_type: str) -> str:
    system_prompts = {
        "research": "You are a research assistant. Provide a thorough, well-structured analysis.",
        "summarize": "You are a summarization expert. Be concise and capture key points.",
        "default": "You are a helpful AI assistant.",
    }
    system = system_prompts.get(task_type, system_prompts["default"])
    print(f"[{TASK_ID}] running agent — type={task_type}")
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": task_input}],
    )
    return message.content[0].text

def main():
    print(f"[{TASK_ID}] worker started")
    start_time = time.time()                          # start the clock here

    raw = r.get(f"task:{TASK_ID}")
    if not raw:
        print(f"[{TASK_ID}] ERROR: task not found in Redis, exiting")
        exit(1)

    task = json.loads(raw)
    print(f"[{TASK_ID}] fetched task: type={task['type']}")
    update_status("running")

    try:
        result = run_agent(task["input"], task["type"])
        duration = time.time() - start_time           # measure actual duration
        update_status("complete", result=result, duration_seconds=duration)
        notify_completion(TASK_ID)
        print(f"[{TASK_ID}] completed in {duration:.1f}s")

    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[{TASK_ID}] ERROR: {error_msg}")
        update_status("failed", error=error_msg, duration_seconds=duration)
        notify_completion(TASK_ID)
        exit(1)

if __name__ == "__main__":
    main()