import os
import json
import redis
import datetime
import time
import urllib.request
from typing import TypedDict, List

from langgraph.graph import StateGraph, END
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from duckduckgo_search import DDGS

# ── Environment ────────────────────────────────────────────────
TASK_ID        = os.environ["TASK_ID"]
REDIS_HOST     = os.environ.get("REDIS_HOST", "redis-service")
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
FASTAPI_HOST   = os.environ.get("FASTAPI_HOST", "fastapi-service")

r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

llm = ChatAnthropic(
    model="claude-sonnet-4-5",
    api_key=ANTHROPIC_KEY,
    max_tokens=2048,
)

# ── State ──────────────────────────────────────────────────────
class AgentState(TypedDict):
    task_id:        str
    task_type:      str
    task_input:     str        # renamed from input (also a Python builtin)
    agent_plan:     str        # renamed from plan
    needs_search:   bool
    search_query:   str        # moved from hidden _search_query
    search_results: List[str]
    reasoning:      str
    final_answer:   str
    steps_taken:    int
    error:          str

# ── Redis helpers ──────────────────────────────────────────────
def update_redis(status: str, **kwargs):
    raw = r.get(f"task:{TASK_ID}")
    if not raw:
        return
    task = json.loads(raw)
    task["status"] = status
    task["updated_at"] = datetime.datetime.utcnow().isoformat()
    for k, v in kwargs.items():
        task[k] = v
    r.set(f"task:{TASK_ID}", json.dumps(task))
    print(f"[{TASK_ID[:8]}] status={status} " +
          " ".join(f"{k}={str(v)[:40]}" for k, v in kwargs.items()))

def notify_fastapi():
    try:
        url = f"http://{FASTAPI_HOST}/tasks/{TASK_ID}"
        urllib.request.urlopen(url, timeout=5)
    except Exception as e:
        print(f"[{TASK_ID[:8]}] WARNING: notify failed: {e}")

# ── Node 1: plan ───────────────────────────────────────────────
def plan_node(state: AgentState) -> dict:
    print(f"[{TASK_ID[:8]}] node=plan")
    update_redis("running", current_node="plan")

    messages = [
        SystemMessage(content="""You are a planning agent. Given a task, you must:
                                1. Analyze what information is needed
                                2. Decide if web search is required (for current events, recent data, specific facts)
                                3. Output a brief plan

                                Respond in this exact JSON format:
                                {
                                "needs_search": true or false,
                                "search_query": "query if needs_search is true, else null",
                                "plan": "2-3 sentence plan describing your approach"
                                }"""),
        HumanMessage(content=f"Task type: {state['task_type']}\nTask: {state['task_input']}"),
    ]

    response = llm.invoke(messages)

    try:
        text = response.content.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
    except Exception:
        parsed = {
            "needs_search": False,
            "search_query": None,
            "plan": response.content[:200],
        }

    return {
        "agent_plan":   parsed.get("plan", ""),
        "needs_search": parsed.get("needs_search", False),
        "search_query": parsed.get("search_query", "") or "",
        "steps_taken":  state["steps_taken"] + 1,
    }

# ── Conditional edge: route after plan ────────────────────────
def route_after_plan(state: AgentState) -> str:
    """
    Decides which node runs after plan.
    Returns the node name as a string.
    """
    if state.get("needs_search"):
        return "search"
    return "write"

# ── Node 2: search ─────────────────────────────────────────────
def search_node(state: AgentState) -> dict:
    print(f"[{TASK_ID[:8]}] node=search")
    update_redis("running", current_node="search")

    query = state.get("search_query") or state["task_input"]
    results = []

    try:
        with DDGS() as ddgs:
            for r_item in ddgs.text(query, max_results=5):
                snippet = f"{r_item['title']}: {r_item['body']}"
                results.append(snippet)
                print(f"[{TASK_ID[:8]}] search result: {snippet[:80]}...")
    except Exception as e:
        print(f"[{TASK_ID[:8]}] search error: {e}")
        results = [f"Search unavailable: {str(e)}"]

    return {
        "search_results": results,
        "steps_taken":    state["steps_taken"] + 1,
    }

# ── Node 3: reason ─────────────────────────────────────────────
def reason_node(state: AgentState) -> dict:
    print(f"[{TASK_ID[:8]}] node=reason")
    update_redis("running", current_node="reason")

    search_context = "\n\n".join(state["search_results"])

    messages = [
        SystemMessage(content="""You are a reasoning agent. Given search results and a task,
analyze the information critically. Identify key facts, note any gaps or conflicts,
and summarize what you have learned. Be concise and factual."""),
        HumanMessage(content=f"""Task: {state['task_input']}

Search results:
{search_context}

Provide your reasoning about what you found and how it answers the task."""),
    ]

    response = llm.invoke(messages)

    return {
        "reasoning":   response.content,
        "steps_taken": state["steps_taken"] + 1,
    }

# ── Node 4: write ──────────────────────────────────────────────
def write_node(state: AgentState) -> dict:
    print(f"[{TASK_ID[:8]}] node=write")
    update_redis("running", current_node="write")

    system_prompts = {
        "research":  "You are a research analyst. Write a thorough, well-structured analysis with clear sections.",
        "summarize": "You are an expert summarizer. Write a concise, accurate summary with the key points clearly listed.",
    }
    system = system_prompts.get(state["task_type"],
             "You are a helpful assistant. Write a clear, accurate, well-structured response.")

    context_parts = [f"Task: {state['task_input']}"]
    if state.get("agent_plan"):
        context_parts.append(f"My plan: {state['agent_plan']}")
    if state.get("search_results"):
        context_parts.append("Search findings:\n" + "\n".join(state["search_results"][:3]))
    if state.get("reasoning"):
        context_parts.append(f"My analysis: {state['reasoning']}")

    messages = [
        SystemMessage(content=system),
        HumanMessage(content="\n\n".join(context_parts) +
                     "\n\nNow write the final, polished response."),
    ]

    response = llm.invoke(messages)

    return {
        "final_answer": response.content,
        "steps_taken":  state["steps_taken"] + 1,
    }

# ── Node 5: done ───────────────────────────────────────────────
def done_node(state: AgentState) -> dict:
    print(f"[{TASK_ID[:8]}] node=done steps={state['steps_taken']}")

    result_payload = {
        "answer": state["final_answer"],
        "trace": {
            "plan":           state.get("agent_plan", ""),
            "needed_search":  state.get("needs_search", False),
            "search_results": state.get("search_results", []),
            "reasoning":      state.get("reasoning", ""),
            "steps_taken":    state["steps_taken"],
        }
    }

    update_redis(
        "complete",
        result=json.dumps(result_payload),
        steps_taken=state["steps_taken"],
        current_node="done",
    )
    notify_fastapi()

    # LangGraph requires at least one state key returned — use steps_taken
    return {"steps_taken": state["steps_taken"]}

# ── Build the graph ────────────────────────────────────────────
def build_graph():
    graph = StateGraph(AgentState)

    # Add all nodes
    graph.add_node("plan",   plan_node)
    graph.add_node("search", search_node)
    graph.add_node("reason", reason_node)
    graph.add_node("write",  write_node)
    graph.add_node("done",   done_node)

    # Entry point
    graph.set_entry_point("plan")

    # Conditional routing after plan
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "search": "search",
            "write":  "write",
        }
    )

    # Fixed edges
    graph.add_edge("search", "reason")
    graph.add_edge("reason", "write")
    graph.add_edge("write",  "done")
    graph.add_edge("done",   END)

    return graph.compile()

# ── Main ───────────────────────────────────────────────────────
def main():
    start_time = time.time()
    print(f"[{TASK_ID[:8]}] worker started")

    # Fetch task from Redis
    raw = r.get(f"task:{TASK_ID}")
    if not raw:
        print(f"[{TASK_ID[:8]}] ERROR: task not found in Redis")
        exit(1)

    task = json.loads(raw)
    print(f"[{TASK_ID[:8]}] task type={task['type']}")

    # Build initial state
    initial_state: AgentState = {
        "task_id":        TASK_ID,
        "task_type":      task["type"],
        "task_input":     task["input"],   # renamed
        "agent_plan":     "",              # renamed
        "needs_search":   False,
        "search_query":   "",              # moved here
        "search_results": [],
        "reasoning":      "",
        "final_answer":   "",
        "steps_taken":    0,
        "error":          "",
    }

    try:
        agent = build_graph()

        # Run the graph — LangGraph handles node execution and routing
        final_state = agent.invoke(initial_state)

        duration = time.time() - start_time
        print(f"[{TASK_ID[:8]}] completed in {duration:.1f}s "
              f"steps={final_state.get('steps_taken', 0)}")

        # Update duration in Redis
        raw = r.get(f"task:{TASK_ID}")
        if raw:
            t = json.loads(raw)
            t["duration_seconds"] = duration
            r.set(f"task:{TASK_ID}", json.dumps(t))

    except Exception as e:
        duration = time.time() - start_time
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[{TASK_ID[:8]}] ERROR: {error_msg}")
        update_redis("failed", error=error_msg, duration_seconds=duration)
        notify_fastapi()
        exit(1)

if __name__ == "__main__":
    main()