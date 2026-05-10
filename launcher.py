import os
import json
import redis
import time
from kubernetes import client, config

REDIS_HOST = os.environ.get("REDIS_HOST", "redis-service")
QUEUE_NAME = "task_queue"
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "agent-worker:v1")
NAMESPACE = os.environ.get("NAMESPACE", "default")

r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

def load_k8s_config():
    """
    Inside a cluster: use the ServiceAccount token mounted automatically.
    Outside a cluster (local dev): use your kubeconfig file.
    """
    try:
        config.load_incluster_config()    # uses /var/run/secrets/kubernetes.io/serviceaccount/
        print("Loaded in-cluster config")
    except config.ConfigException:
        config.load_kube_config()         # falls back to ~/.kube/config
        print("Loaded kubeconfig")

def build_job_manifest(task_id: str, task_type: str) -> client.V1Job:
    """
    Programmatically build a K8s Job object.
    This is the Python equivalent of writing a Job YAML manifest.
    """
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=f"agent-task-{task_id[:8]}",   # Job names must be unique
            namespace=NAMESPACE,
            labels={"app": "agent-worker", "task-id": task_id[:8]},
        ),
        spec=client.V1JobSpec(
            backoff_limit=2,                  # retry up to 2 times on failure
            active_deadline_seconds=300,      # kill if running longer than 5 min
            ttl_seconds_after_finished=600,   # auto-delete Job 10 min after completion
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="Never",   # Job semantics — don't restart in place
                    service_account_name="job-launcher-sa",
                    containers=[
                        client.V1Container(
                            name="worker",
                            image=WORKER_IMAGE,
                            image_pull_policy="Never",    # use locally imported image
                            env=[
                                client.V1EnvVar(name="TASK_ID", value=task_id),
                                client.V1EnvVar(name="REDIS_HOST", value=REDIS_HOST),
                                # Pull API key from the Secret we created — never hardcode
                                client.V1EnvVar(
                                    name="ANTHROPIC_API_KEY",
                                    value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            name="agent-secrets",
                                            key="ANTHROPIC_API_KEY",
                                        )
                                    ),
                                ),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"memory": "128Mi", "cpu": "100m"},
                                limits={"memory": "256Mi", "cpu": "500m"},
                            ),
                        )
                    ],
                )
            ),
        ),
    )

def launch_job(task_id: str, task_type: str):
    batch_v1 = client.BatchV1Api()
    job = build_job_manifest(task_id, task_type)
    batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job)
    print(f"[launcher] created Job for task {task_id[:8]}")

def main():
    load_k8s_config()
    print(f"[launcher] watching queue '{QUEUE_NAME}' on {REDIS_HOST}")

    while True:
        try:
            # BRPOP blocks here — zero CPU until a task arrives
            # Timeout=30 means wake up every 30s even with no tasks (for health checks)
            result = r.brpop(QUEUE_NAME, timeout=30)

            if result is None:
                # Timeout — just loop back and block again
                continue

            _, task_id = result   # BRPOP returns (queue_name, value)
            print(f"[launcher] dequeued task {task_id[:8]}")

            # Fetch task metadata to get the type
            raw = r.get(f"task:{task_id}")
            if not raw:
                print(f"[launcher] WARNING: task {task_id} not found in Redis, skipping")
                continue

            task = json.loads(raw)
            launch_job(task_id, task["type"])

        except Exception as e:
            print(f"[launcher] ERROR: {e}")
            time.sleep(5)   # brief backoff before retrying

if __name__ == "__main__":
    main()