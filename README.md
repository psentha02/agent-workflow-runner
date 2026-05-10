# Agent Workflow Runner

A Kubernetes-native AI task platform that runs multi-step LangGraph agents as isolated Jobs, with full observability. Submit a task over HTTP, and the system dynamically provisions a Kubernetes Job that runs a Claude-powered agent — with a planning step, optional web search, multi-step reasoning, retry logic, secret injection, persistent queuing, and real-time metrics.

---

## What this is

Most AI agent demos run inside a single process. This project treats AI workloads the way production infrastructure teams do — as isolated, schedulable, observable units of compute. Each agent task gets its own Kubernetes Job with its own resource limits, failure domain, and lifecycle. One stuck agent cannot affect others. A crashed worker retries automatically. Everything is visible on a Grafana dashboard. The entire application is packaged as a Helm chart deployable in one command.

---

## Architecture

```
Client
  │
  │  POST /tasks
  ▼
FastAPI server ──────────────── K8s Service (ClusterIP)
  │                             DNS: fastapi-service
  │  LPUSH task_id
  ▼
Redis queue ─────────────────── K8s StatefulSet + PersistentVolume
  │                             DNS: redis-service
  │  BRPOP (blocking)
  ▼
Job Launcher ─────────────────── K8s Deployment
  │  ServiceAccount + RBAC       watches queue, calls K8s API
  │
  │  create Job (K8s Python client)
  ▼
Worker Pod ───────────────────── K8s Job (one per task)
  │  TASK_ID via env var
  │  API key via K8s Secret
  │
  ├── plan node    → Claude decides approach + whether to search
  ├── search node  → DuckDuckGo web search (conditional)
  ├── reason node  → Claude synthesizes results (conditional)
  ├── write node   → Claude drafts final answer
  └── done node    → writes result + trace to Redis · exit(0)

Prometheus scrapes /metrics every 15s via ServiceMonitor
Grafana queries Prometheus via PromQL
```

---

## LangGraph agent graph

Each worker Pod runs a LangGraph state machine. Claude makes real decisions at each node — whether to search the web, what to search for, how to reason about results, and how to synthesize a final answer.

```
START
  │
  ▼
[plan] ── Claude reads task, decides approach, sets needs_search
  │
  │  route_after_plan()
  ├── YES ──► [search] ── DuckDuckGo · up to 5 results
  │               │
  │               ▼
  │           [reason] ── Claude synthesizes, identifies key facts
  │               │
  └── NO ─────────┤
                  ▼
              [write] ── Claude drafts final polished answer
                  │
                  ▼
              [done] ── writes result + full trace to Redis · exit(0)
                  │
                 END
```

The full reasoning trace is stored in Redis and returned via the API — plan, search results, reasoning, and steps taken are all inspectable.

---

## Kubernetes concepts demonstrated

| Concept | Where used | Why |
|---|---|---|
| **Deployment** | FastAPI, Job Launcher | Stateless services — replaceable Pods, rolling updates |
| **StatefulSet** | Redis | Stable network identity, stable storage across restarts |
| **Job** | Each agent task | Run-to-completion semantics, automatic retry, clean lifecycle |
| **PersistentVolume + PVC** | Redis storage | Queue data survives Pod restarts |
| **ServiceAccount + Role + RoleBinding** | Job Launcher | Scoped RBAC — launcher can only create Jobs |
| **Secret** | ANTHROPIC_API_KEY | Never baked into images or manifests |
| **Service (ClusterIP)** | FastAPI, Redis | Stable DNS for inter-Pod communication |
| **Headless Service** | Redis | Direct Pod DNS for StatefulSet stable identity |
| **ServiceMonitor** | FastAPI metrics | Prometheus operator discovers scrape targets declaratively |
| **Helm Chart** | Application layer | Single-command install, configurable values, repeatable deploys |

---

## Stack

**Orchestration:** k3d (local), Kubernetes 1.29+, Helm 3

**Application:** Python 3.11, FastAPI, Redis (StatefulSet), Kubernetes Python client

**AI:** Anthropic Claude API (claude-sonnet-4-5), LangGraph, LangChain Anthropic, DuckDuckGo Search

**Observability:** Prometheus (kube-prometheus-stack), Grafana, redis_exporter

---

## Project structure

```
agent-workflow-runner/
├── app/
│   ├── main.py                   # FastAPI — task submission + status + metrics
│   ├── Dockerfile
│   └── requirements.txt
├── worker/
│   ├── agent.py                  # LangGraph multi-step agent
│   ├── Dockerfile
│   └── requirements.txt
├── charts/
│   └── agent-runner/
│       ├── Chart.yaml
│       ├── values.yaml           # single source of truth for all config
│       └── templates/
│           ├── _helpers.tpl      # named templates for labels and selectors
│           ├── secret.yaml
│           ├── rbac.yaml
│           ├── redis-statefulset.yaml
│           ├── redis-service.yaml
│           ├── fastapi-deployment.yaml
│           ├── fastapi-service.yaml
│           ├── fastapi-servicemonitor.yaml
│           └── job-launcher-deployment.yaml
├── k8s/                          # raw manifests (pre-Helm reference)
├── launcher.py                   # watches Redis queue, creates K8s Jobs
├── Dockerfile.launcher
├── launcher_requirements.txt
└── README.md
```

---

## Prerequisites

- Docker Desktop running
- `brew install k3d kubectl helm`
- An Anthropic API key

---

## Installation

The system has two layers — cluster-wide observability infrastructure and the application itself. They are installed separately because Prometheus and Grafana are cluster-wide tools that multiple applications can share. If your cluster already has Prometheus installed, skip Step 2.

> **Planned improvement:** a future release will add `kube-prometheus-stack` as a Helm sub-chart dependency so the entire system installs in one command. For now the two-step process below is required.

### Step 1 — Create the cluster and import images

```bash
k3d cluster create agentcluster \
  --agents 1 \
  --port "8080:80@loadbalancer"

# Build and import all three images into k3d
cd app    && docker build -t agent-api:v4 .    && k3d image import agent-api:v4 -c agentcluster    && cd ..
cd worker && docker build -t agent-worker:v5 . && k3d image import agent-worker:v5 -c agentcluster && cd ..
docker build -f Dockerfile.launcher -t job-launcher:v1 .
k3d image import job-launcher:v1 -c agentcluster
```

### Step 2 — Install Prometheus and Grafana (cluster-wide observability)

This installs the `kube-prometheus-stack` Helm chart into a dedicated `monitoring` namespace. It includes Prometheus, Grafana, kube-state-metrics, and the Prometheus operator.

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring

helm install prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.adminPassword=admin \
  --set prometheus.prometheusSpec.scrapeInterval=15s

# Wait for all monitoring Pods to reach Running
kubectl get pods -n monitoring -w
```

Once running, the observability stack is accessible via port-forward:

```bash
# Grafana — http://localhost:3000 (admin / admin)
kubectl port-forward -n monitoring service/prometheus-stack-grafana 3000:80

# Prometheus — http://localhost:9090
kubectl port-forward -n monitoring \
  service/prometheus-stack-kube-prom-prometheus 9090:9090
```

### Step 3 — Install the application chart

```bash
# Validate templates before touching the cluster
helm lint ./charts/agent-runner --set anthropicApiKey=test

# Install into a dedicated namespace
helm install agent-runner ./charts/agent-runner \
  --namespace agent-runner \
  --create-namespace \
  --set anthropicApiKey=$ANTHROPIC_API_KEY

# Watch all Pods come up
kubectl get pods -n agent-runner -w
```

You should see `redis-0`, `fastapi-*`, and `job-launcher-*` all reach `Running`. The job-launcher logs will confirm it is watching the queue:

```bash
kubectl logs deployment/job-launcher -n agent-runner
# Loaded in-cluster config
# [launcher] watching queue 'task_queue' on redis-service
```

---

## Submitting tasks

```bash
# Port-forward FastAPI
kubectl port-forward -n agent-runner service/fastapi-service 9001:80

# Submit a research task (triggers web search path)
TASK_ID=$(curl -s -X POST http://localhost:9001/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "research", "input": "What is the operator pattern in Kubernetes?"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

echo "task_id: $TASK_ID"

# Watch the Job Pod appear and run
kubectl get pods -n agent-runner -w

# Poll for completion
for i in {1..30}; do
  STATUS=$(curl -s http://localhost:9001/tasks/$TASK_ID \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "status: $STATUS"
  if [[ "$STATUS" == "complete" || "$STATUS" == "failed" ]]; then break; fi
  sleep 3
done

# Read the full result and reasoning trace
curl -s http://localhost:9001/tasks/$TASK_ID \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
result = json.loads(d['result'])
print('=== ANSWER ===')
print(result['answer'][:500])
print()
print('=== TRACE ===')
print('plan:', result['trace']['plan'])
print('needed_search:', result['trace']['needed_search'])
print('steps_taken:', result['trace']['steps_taken'])
"
```

Submit a summarization task (skips web search, faster):

```bash
curl -s -X POST http://localhost:9001/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "summarize", "input": "Explain the difference between a Kubernetes Job and a Deployment"}'
```

---

## Grafana dashboards

With Grafana running at `http://localhost:3000`, add these panels to a new dashboard:

| Panel | PromQL | What it shows |
|---|---|---|
| Submission rate | `rate(tasks_submitted_total[5m])` | Tasks submitted per second |
| Queue depth | `task_queue_depth` | Tasks waiting in the Redis queue |
| Success rate | `rate(tasks_completed_total{status="complete"}[5m])` | Completions per second |
| Failure rate | `rate(tasks_completed_total{status="failed"}[5m])` | Failures per second |
| p99 latency | `histogram_quantile(0.99, rate(task_duration_seconds_bucket[5m]))` | 99th percentile agent duration |

Generate load to populate the dashboard:

```bash
for i in {1..10}; do
  curl -s -X POST http://localhost:9001/tasks \
    -H "Content-Type: application/json" \
    -d "{\"type\": \"research\", \"input\": \"kubernetes concept $i\"}" > /dev/null
  echo "submitted task $i"
  sleep 3
done
```

---

## API reference

```
POST /tasks
  Body:    { "type": "research" | "summarize", "input": "<prompt>" }
  Returns: { "task_id": "uuid", "status": "queued", "created_at": "iso8601" }
  Status:  202 Accepted

GET /tasks/{task_id}
  Returns: {
    "task_id", "type", "input", "status",
    "result": {
      "answer": "<final answer>",
      "trace": {
        "plan": "<Claude plan>",
        "needed_search": true | false,
        "search_results": ["..."],
        "reasoning": "<Claude reasoning>",
        "steps_taken": 4
      }
    },
    "duration_seconds", "created_at", "updated_at"
  }
  Status values: queued → running → complete | failed

GET /healthz   → { "status": "ok", "redis": "connected" }
GET /metrics   → Prometheus exposition format (scraped every 15s)
```

---

## Helm reference

```bash
# Render all templates locally without installing
helm template agent-runner ./charts/agent-runner --set anthropicApiKey=test

# Lint for common issues
helm lint ./charts/agent-runner --set anthropicApiKey=test

# Install
helm install agent-runner ./charts/agent-runner \
  --namespace agent-runner --create-namespace \
  --set anthropicApiKey=$ANTHROPIC_API_KEY

# Upgrade after any change to templates or values
helm upgrade agent-runner ./charts/agent-runner \
  --namespace agent-runner \
  --set anthropicApiKey=$ANTHROPIC_API_KEY

# See the active values for a running release
helm get values agent-runner -n agent-runner

# See the rendered manifests of a running release
helm get manifest agent-runner -n agent-runner

# Uninstall the application cleanly
helm uninstall agent-runner -n agent-runner

# Uninstall the observability stack separately
helm uninstall prometheus-stack -n monitoring

# List all releases across all namespaces
helm list -A
```

---

## Key design decisions

**Jobs not threads** — each agent task runs in an isolated Pod with its own resource limits and failure domain. One agent stuck in a loop consuming memory does not affect others. A thread-per-task model inside a single server lets one bad task take down everything.

**LangGraph state machine** — the agent accumulates context as it moves through nodes. The plan node decides what to do, search gathers facts, reason synthesizes, write produces the answer. Each step builds on the last. The full trace is stored and inspectable via the API.

**Redis not a database** — BRPOP blocks atomically until a task arrives, waking exactly one worker with zero CPU overhead. A Postgres queue needs polling, locking, a status column, and a reaper job — 200 lines of infrastructure for two Redis commands.

**StatefulSet for Redis, Deployment for everything else** — Redis needs stable identity and storage across restarts. StatefulSets guarantee `redis-0` always reattaches to the same PersistentVolume.

**Scoped RBAC** — the launcher ServiceAccount can only create Jobs in its namespace. A compromised Pod has a minimal blast radius — it cannot read Secrets, modify Deployments, or touch other namespaces.

**Helm chart for the application layer only** — Prometheus and Grafana are cluster-wide infrastructure, not application-specific. They are installed separately so multiple applications can share the same observability stack. A future release will add `kube-prometheus-stack` as a chart dependency for fully one-command installation.

---

## Debugging reference

```bash
# Pod not starting
kubectl describe pod <pod-name> -n agent-runner
kubectl logs <pod-name> -n agent-runner -p        # previous crashed container

# Job failed
kubectl get jobs -n agent-runner
kubectl logs job/<job-name> -n agent-runner
kubectl delete jobs -n agent-runner --field-selector status.successful=0

# Redis inspection
kubectl exec -it redis-0 -n agent-runner -- redis-cli
  LLEN task_queue           # queue depth
  LRANGE task_queue 0 -1    # all queued task_ids
  GET task:<id>             # full task JSON

# Prometheus not scraping
kubectl logs -n monitoring deployment/prometheus-stack-kube-prom-operator --tail=50

# Helm troubleshooting
helm template agent-runner ./charts/agent-runner --set anthropicApiKey=test
helm get manifest agent-runner -n agent-runner
```

---

## Git commit history

| Phase | Commit | What was built |
|---|---|---|
| 1 | `feat: k3d cluster and FastAPI skeleton` | Local K8s cluster, FastAPI with in-memory store |
| 2 | `feat: Redis StatefulSet with persistent storage` | StatefulSet, PVC, AOF persistence, Redis-backed API |
| 3 | `feat: RBAC, secrets, Job Launcher, worker agent` | ServiceAccount, scoped Role, job launcher, Claude agent |
| 4 | `feat: Prometheus and Grafana observability` | Counter/Histogram/Gauge, ServiceMonitor, Grafana dashboards |
| 5 | `feat: LangGraph multi-step agent with web search` | plan/search/reason/write/done graph, DuckDuckGo, trace |
| 6 | `feat: Helm chart packaging full application` | Templated chart, values.yaml, _helpers.tpl, one-command install |

---

## Planned improvements

- **Helm sub-chart dependency** — add `kube-prometheus-stack` as a chart dependency so the entire system including Prometheus and Grafana installs in one command
- **Conversation memory** — add user_id to the task payload and use LangGraph's Redis checkpointer to persist agent state across tasks
- **Cloud deployment** — push images to GitHub Container Registry and deploy to a real cluster on Hetzner or AWS EKS
- **Alerting** — add PrometheusRule for job failure rate and queue depth thresholds
