import csv
import logging
import os
import subprocess
import sys
import time

# ==============================================================================
# KONFIGURATION FÜR UF3 / HYPOTHESE 3 (DISASTER RECOVERY & RE-INITIALISIERUNG)
# ==============================================================================
RUNS = 1  # Hinweis: Da ein kompletter Cluster-Neubau ~60-90s dauert, reichen hier 5-10 Läufe
CSV_FILE = "results/disaster_recovery_measurements.csv"
LOG_FILE = "results/disaster_recovery_audit.log"

os.makedirs("results", exist_ok=True)
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
        time.sleep(1.0)
    return False


def run_gitops_dr(results):
    logging.info("=======================================================")
    logging.info("STARTE UF3-MESSUNG: GITOPS DISASTER RECOVERY (Terraform + ArgoCD)")
    logging.info("=======================================================")

    for run in range(1, RUNS + 1):
        logging.info(f"\n[GitOps DR] Simuliere Totalausfall (Lauf {run}/{RUNS})...")
        run_cmd("terraform -chdir=infrastructure destroy -auto-approve", log_cmd=False)

        logging.info(
            "  -> Cluster komplett zerstört. Starte Wiederaufbau (Stoppuhr läuft)..."
        )
        t_start = time.perf_counter()

        # Schritt 1: Terraform zieht Cluster + ArgoCD hoch
        run_cmd("terraform -chdir=infrastructure apply -auto-approve")
        # Schritt 2: Deklaratives App-Manifest übergeben (kann auch direkt in Terraform integriert sein)
        run_cmd("kubectl apply -f workloads/argocd-app.yaml")

        wait_for_all_pods_ready()
        duration_s = round(time.perf_counter() - t_start, 2)
        logging.info(
            f"  -> GitOps Umgebung vollständig wiederhergestellt in {duration_s}s!"
        )

        results.append(
            {
                "model": "GitOps_ArgoCD",
                "run": run,
                "recovery_duration_s": duration_s,
                "manual_steps": 1,  # 1 deklarativer Bootstrap-Lauf (Terraform inkl. App-Bootstrap)
            }
        )


def run_push_dr(results):
    logging.info("\n=======================================================")
    logging.info("STARTE UF3-MESSUNG: PUSH-MODELL DISASTER RECOVERY (Sequenziell)")
    logging.info("=======================================================")

    for run in range(1, RUNS + 1):
        logging.info(f"\n[Push DR] Simuliere Totalausfall (Lauf {run}/{RUNS})...")
        run_cmd("kind delete cluster --name bachelor-demo")

        logging.info(
            "  -> Cluster komplett zerstört. Starte sequenziellen Wiederaufbau (Stoppuhr)..."
        )
        t_start = time.perf_counter()

        # Manuelle/Sequenzielle Prozesskette im Push-Modell:
        # 1. Basis-Cluster erstellen
        run_cmd("kind create cluster --name bachelor-demo --wait 60s")
        # 2. Neue Admin-Zugangsdaten ("God-Mode") für externes CI/CD exportieren
        run_cmd("kind get kubeconfig --name bachelor-demo > admin-kubeconfig.yaml")
        # 3. Workloads über externe Kubeconfig pushen
        run_cmd(
            "kubectl --kubeconfig=admin-kubeconfig.yaml apply -f workloads/base/manifest.yaml"
        )
        # 4. Rollout-Status der Schichten einzeln abwarten
        run_cmd(
            "kubectl --kubeconfig=admin-kubeconfig.yaml rollout status deploy/database"
        )
        run_cmd(
            "kubectl --kubeconfig=admin-kubeconfig.yaml rollout status deploy/backend-api"
        )
        run_cmd(
            "kubectl --kubeconfig=admin-kubeconfig.yaml rollout status deploy/frontend"
        )

        wait_for_all_pods_ready()
        duration_s = round(time.perf_counter() - t_start, 2)
        logging.info(f"  -> Push-Umgebung wiederhergestellt in {duration_s}s!")

        results.append(
            {
                "model": "Push_Pipeline",
                "run": run,
                "recovery_duration_s": duration_s,
                "manual_steps": 4,  # 1: Cluster-Bau, 2: Secret-Export, 3: Pipeline-Trigger, 4: Rollout-Check
            }
        )


if __name__ == "__main__":
    results = []
    run_gitops_dr(results)
    run_push_dr(results)

    with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["model", "run", "recovery_duration_s", "manual_steps"]
        )
        writer.writeheader()
        writer.writerows(results)

    logging.info(f"FERTIG! Disaster-Recovery-Daten gespeichert in '{CSV_FILE}'.")
