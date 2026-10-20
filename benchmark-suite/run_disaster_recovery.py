import csv
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ==============================================================================
# KONFIGURATION FÜR UF3
# ==============================================================================
RUNS = 1

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

CSV_FILE = PROJECT_ROOT / "results" / "disaster_recovery_measurements.csv"
LOG_FILE = PROJECT_ROOT / "results" / "disaster_recovery_audit.log"
INFRA_DIR = PROJECT_ROOT / "infrastructure"
MANIFEST_PATH = PROJECT_ROOT / "workloads" / "base" / "manifest.yaml"
ARGOCD_APP_PATH = PROJECT_ROOT / "workloads" / "argocd-app.yaml"
ADMIN_KUBECONFIG = PROJECT_ROOT / "admin-kubeconfig.yaml"

os.makedirs(CSV_FILE.parent, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def run_cmd(cmd, log_cmd=True):
    if log_cmd:
        logging.info(f"EXEC: {cmd}")
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if log_cmd and res.stdout.strip():
        logging.info(f"STDOUT: {res.stdout.strip()}")
    if log_cmd and res.stderr.strip():
        logging.warning(f"STDERR: {res.stderr.strip()}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def wait_for_all_pods_ready(timeout=180.0):
    """Wartet, bis alle 5 Pods der 3-Tier-App (1x DB, 2x API, 2x Frontend) Ready sind."""
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        _, db_ready, _ = run_cmd(
            'kubectl get deploy database -o jsonpath="{.status.readyReplicas}"',
            log_cmd=False,
        )
        _, api_ready, _ = run_cmd(
            'kubectl get deploy backend-api -o jsonpath="{.status.readyReplicas}"',
            log_cmd=False,
        )
        _, fe_ready, _ = run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.status.readyReplicas}"',
            log_cmd=False,
        )
        if (
            db_ready.replace('"', "") == "1"
            and api_ready.replace('"', "") == "2"
            and fe_ready.replace('"', "") == "2"
        ):
            return True
        time.sleep(0.5)
    return False


def run_gitops_dr(results):
    logging.info("=======================================================")
    logging.info("STARTE UF3-MESSUNG: GITOPS DISASTER RECOVERY (Terraform + ArgoCD)")
    logging.info("=======================================================")

    for run in range(1, RUNS + 1):
        logging.info(f"\n[GitOps DR] Simuliere Totalausfall (Lauf {run}/{RUNS})...")
        run_cmd(f"terraform -chdir={INFRA_DIR} destroy -auto-approve", log_cmd=False)
        run_cmd("kind delete cluster --name bachelor-demo", log_cmd=False)

        logging.info("  -> Cluster zerstört. Starte 3-Phasen-Messung (GitOps)...")
        t_total_start = time.perf_counter()

        # PHASE 1: 100% Tooling-Fairness - Reines Cluster-Provisioning via Terraform
        t_p1 = time.perf_counter()
        run_cmd(
            f"terraform -chdir={INFRA_DIR} apply -target=kind_cluster.bachelor_cluster -auto-approve"
        )
        phase1_s = round(time.perf_counter() - t_p1, 2)
        logging.info(f"  -> Phase 1 (Cluster-Provisioning via Terraform): {phase1_s}s")

        # PHASE 2: Bootstrapping des In-Cluster-Operators (ArgoCD Helm Release via Terraform)
        t_p2 = time.perf_counter()
        run_cmd(f"terraform -chdir={INFRA_DIR} apply -auto-approve")
        phase2_s = round(time.perf_counter() - t_p2, 2)
        logging.info(f"  -> Phase 2 (Operator-Bootstrapping ArgoCD): {phase2_s}s")

        # PHASE 3: Workload-Synchronisation der 3-Tier-Applikation
        t_p3 = time.perf_counter()
        run_cmd(f"kubectl apply -f {ARGOCD_APP_PATH}")
        wait_for_all_pods_ready()
        phase3_s = round(time.perf_counter() - t_p3, 2)
        logging.info(f"  -> Phase 3 (Workload-Sync 3-Tier-App): {phase3_s}s")

        total_s = round(time.perf_counter() - t_total_start, 2)
        logging.info(f"  => GitOps Gesamtdauer: {total_s}s")

        results.append(
            {
                "model": "GitOps_ArgoCD",
                "run": run,
                "phase1_cluster_s": phase1_s,
                "phase2_operator_s": phase2_s,
                "phase3_workload_s": phase3_s,
                "recovery_duration_s": total_s,
                "manual_steps": 1,  # 1 deklarativer Schritt (Bootstrap stößt alles an)
            }
        )


def run_push_dr(results):
    logging.info("\n=======================================================")
    logging.info("STARTE UF3-MESSUNG: PUSH-MODELL DISASTER RECOVERY (Sequenziell)")
    logging.info("=======================================================")

    for run in range(1, RUNS + 1):
        logging.info(f"\n[Push DR] Simuliere Totalausfall (Lauf {run}/{RUNS})...")
        run_cmd(f"terraform -chdir={INFRA_DIR} destroy -auto-approve", log_cmd=False)
        run_cmd("kind delete cluster --name bachelor-demo", log_cmd=False)

        logging.info("  -> Cluster zerstört. Starte 3-Phasen-Messung (Push-Modell)...")
        t_total_start = time.perf_counter()

        # PHASE 1: 100% Tooling-Fairness - Auch Push nutzt Terraform für den nackten Cluster
        t_p1 = time.perf_counter()
        run_cmd(
            f"terraform -chdir={INFRA_DIR} apply -target=kind_cluster.bachelor_cluster -auto-approve"
        )
        phase1_s = round(time.perf_counter() - t_p1, 2)
        logging.info(f"  -> Phase 1 (Cluster-Provisioning via Terraform): {phase1_s}s")

        # PHASE 2: Kein In-Cluster-Operator im Push-Modell (0.0s)
        phase2_s = 0.0

        # PHASE 3: Admin-Secret-Export & Imperativer Push der Workloads
        t_p3 = time.perf_counter()
        run_cmd(f"kind get kubeconfig --name bachelor-demo > {ADMIN_KUBECONFIG}")
        run_cmd(f"kubectl --kubeconfig={ADMIN_KUBECONFIG} apply -f {MANIFEST_PATH}")
        run_cmd(
            f"kubectl --kubeconfig={ADMIN_KUBECONFIG} rollout status deploy/database"
        )
        run_cmd(
            f"kubectl --kubeconfig={ADMIN_KUBECONFIG} rollout status deploy/backend-api"
        )
        run_cmd(
            f"kubectl --kubeconfig={ADMIN_KUBECONFIG} rollout status deploy/frontend"
        )
        wait_for_all_pods_ready()
        phase3_s = round(time.perf_counter() - t_p3, 2)
        logging.info(f"  -> Phase 3 (Secret-Export & Push-Rollout): {phase3_s}s")

        total_s = round(time.perf_counter() - t_total_start, 2)
        logging.info(f"  => Push-Pipeline Gesamtdauer: {total_s}s")

        results.append(
            {
                "model": "Push_Pipeline",
                "run": run,
                "phase1_cluster_s": phase1_s,
                "phase2_operator_s": phase2_s,
                "phase3_workload_s": phase3_s,
                "recovery_duration_s": total_s,
                "manual_steps": 4,  # 1: Cluster-Bau, 2: Secret-Export, 3: Workload-Push, 4: Rollout-Check
            }
        )


if __name__ == "__main__":
    results = []
    run_gitops_dr(results)
    run_push_dr(results)

    with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "model",
                "run",
                "phase1_cluster_s",
                "phase2_operator_s",
                "phase3_workload_s",
                "recovery_duration_s",
                "manual_steps",
            ],
        )
        writer.writeheader()
        writer.writerows(results)

    logging.info(f"FERTIG! 3-Phasen-DR-Daten gespeichert in '{CSV_FILE}'.")
