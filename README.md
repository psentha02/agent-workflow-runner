# Agent Workflow Runner

A Kubernetes-native AI task platform that runs multi-step LangGraph agents as isolated Jobs, with full observability. Submit a task over HTTP, and the system dynamically provisions a Kubernetes Job that runs a Claude-powered agent — with a planning step, optional web search, multi-step reasoning, retry logic, secret injection, persistent queuing, and real-time metrics.

---

## What this is

Most AI agent demos run inside a single process. This project treats AI workloads the way production infrastructure teams do — as isolated, schedulable, observable units of compute. Each agent task gets its own Kubernetes Job with its own resource limits, failure domain, and lifecycle. One stuck agent cannot affect others. A crashed worker retries automatically. Everything is visible on a Grafana dashboard. The entire system is packaged as a Helm chart and deployable to any Kubernetes cluster in one command.

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
| **Helm Chart** | Full application | Single-command install, configurable values, repeatable deploys |

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

## Quick start — Helm

### Prerequisites

- Docker Desktop running
- `brew install k3d kubectl helm`
- An Anthropic API key

### 1 — Create cluster and import images

```bash
k3d cluster create agentcluster \
  --agents 1 \
  --port "8080:80@loadbalancer"

cd app    && docker build -t agent-api:v4 .    && k3d image import agent-api:v4 -c agentcluster    && cd ..
cd worker && docker build -t agent-worker:v5 . && k3d image import agent-worker:v5 -c agentcluster && cd ..
docker build -f Dockerfile.launcher -t job-launcher:v1 .
k3d image import job-launcher:v1 -c agentcluster
```

### 2 — Install observability stack

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
```

### 3 — Install the application chart

```bash
helm lint ./charts/agent-runner --set anthropicApiKey=test

helm install agent-runner ./charts/agent-runner \
  --namespace agent-runner \
  --create-namespace \
  --set anthropicApiKey=$ANTHROPIC_API_KEY

kubectl get pods -n agent-runner -w
```

### 4 — Submit a task and watch it run

```bash
kubectl port-forward -n agent-runner service/fastapi-service 9001:80

TASK_ID=$(curl -s -X POST http://localhost:9001/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "research", "input": "What is the operator pattern in Kubernetes?"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

echo "task_id: $TASK_ID"

for i in {1..30}; do
  STATUS=$(curl -s http://localhost:9001/tasks/$TASK_ID \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "status: $STATUS"
  if [[ "$STATUS" == "complete" || "$STATUS" == "failed" ]]; then break; fi
  sleep 3
done

curl -s http://localhost:9001/tasks/$TASK_ID \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
result = json.loads(d['result'])
print('ANSWER:', result['answer'][:400])
print('STEPS:', result['trace']['steps_taken'])
print('SEARCHED:', result['trace']['needed_search'])
"
```

---

## Helm reference

```bash
helm template agent-runner ./charts/agent-runner --set anthropicApiKey=test
helm lint ./charts/agent-runner --set anthropicApiKey=test
helm install agent-runner ./charts/agent-runner --namespace agent-runner --create-namespace --set anthropicApiKey=$ANTHROPIC_API_KEY
helm upgrade agent-runner ./charts/agent-runner --namespace agent-runner --set anthropicApiKey=$ANTHROPIC_API_KEY
helm get values agent-runner -n agent-runner
helm get manifest agent-runner -n agent-runner
helm uninstall agent-runner -n agent-runner
helm list -A
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
        "needed_search": true|false,
        "search_results": ["..."],
        "reasoning": "<Claude reasoning>",
        "steps_taken": 4
      }
    },
    "duration_seconds", "created_at", "updated_at"
  }

GET /healthz   → { "status": "ok", "redis": "connected" }
GET /metrics   → Prometheus exposition format
```

---

## Grafana dashboard queries

| Panel | PromQL | What it shows |
|---|---|---|
| Submission rate | `rate(tasks_submitted_total[5m])` | Tasks per second |
| Queue depth | `task_queue_depth` | Tasks waiting to be picked up |
| Success rate | `rate(tasks_completed_total{status="complete"}[5m])` | Completions per second |
| Failure rate | `rate(tasks_completed_total{status="failed"}[5m])` | Failures per second |
| p99 latency | `histogram_quantile(0.99, rate(task_duration_seconds_bucket[5m]))` | 99th percentile agent duration |

---

## Key design decisions

**Jobs not threads** — each agent task runs in an isolated Pod. One agent stuck in a loop consuming memory doesn't affect others.

**LangGraph state machine** — the agent accumulates context as it moves through nodes. The plan node decides what to do, search gathers facts, reason synthesizes, write produces the answer. The full trace is stored and inspectable.

**Redis not a database** — BRPOP blocks atomically until a task arrives, waking exactly one worker with zero CPU overhead.

**StatefulSet for Redis, Deployment for everything else** — Redis needs stable identity and storage. StatefulSets guarantee redis-0 always reattaches to the same PersistentVolume.

**Scoped RBAC** — the launcher ServiceAccount can only create Jobs in its namespace. Compromised Pod has minimal blast radius.

**Helm chart** — the entire application deploys in one command to any Kubernetes cluster.

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

## Debugging reference

```bash
kubectl describe pod <pod-name> -n agent-runner
kubectl logs <pod-name> -n agent-runner -p
kubectl get jobs -n agent-runner
kubectl exec -it redis-0 -n agent-runner -- redis-cli
kubectl logs -n monitoring deployment/prometheus-stack-kube-prom-operator --tail=50
helm template agent-runner ./charts/agent-runner --set anthropicApiKey=test | grep -A5 "kind:"
kubectl delete jobs -n agent-runner --field-selector status.successful=0
```
