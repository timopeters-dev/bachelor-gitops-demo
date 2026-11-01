"""
UF3: Disaster Recovery / Cluster-Re-Initialisierung (GitOps vs. Push)

Aufruf:
    python run_disaster_recovery.py --runs 2                 # Pilot
    python run_disaster_recovery.py --runs 10                # Vollmessung
"""

import argparse
import csv
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ==============================================================================
# PFADE
# ==============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
os.chdir(PROJECT_ROOT)

INFRA_DIR = PROJECT_ROOT / "infrastructure"
MANIFEST = PROJECT_ROOT / "workloads" / "base" / "manifest.yaml"
APP_YAML = PROJECT_ROOT / "workloads" / "argocd-app.yaml"
ADMIN_KUBECONFIG = SCRIPT_DIR / "admin-kubeconfig.yaml"

Q_INFRA = shlex.quote(str(INFRA_DIR))
Q_MANIFEST = shlex.quote(str(MANIFEST))
Q_APP_YAML = shlex.quote(str(APP_YAML))
Q_KUBECONFIG = shlex.quote(str(ADMIN_KUBECONFIG))

CSV_FILE = SCRIPT_DIR / "results" / "disaster_recovery_measurements_v2.csv"
LOG_FILE = SCRIPT_DIR / "results" / "disaster_recovery_audit_v2.log"
os.makedirs(CSV_FILE.parent, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

CLUSTER_NAME = "bachelor-demo"
PODS_READY_TIMEOUT_S = 240.0
FIELDS = [
    "ts_utc",
    "model",
    "run",
    "order_pos",
    "valid",
    "phase1_cluster_s",
    "phase2_operator_s",
    "phase3_workload_s",
    "total_s",
    "n_commands",
    "n_credential_transfers",
    "note",
]

STEPS = {"n": 0}


def run_cmd(cmd, log_cmd=True, log_output=True):
    """log_output=False: nur Befehl und Rückgabecode loggen (z. B. Terraform)."""
    if log_cmd:
        logging.info(f"EXEC: {cmd}")
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if log_cmd and log_output:
        if res.stdout.strip():
            logging.info(f"STDOUT: {res.stdout.strip()}")
        if res.stderr.strip():
            logging.warning(f"STDERR: {res.stderr.strip()}")
    elif log_cmd:
        logging.info(f"  rc={res.returncode}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def step(cmd, log_output=True):
    """Zustandsändernder Befehl: wird gezählt (n_commands)."""
    STEPS["n"] += 1
    return run_cmd(cmd, log_output=log_output)


def kc(kubeconfig_quoted):
    return f"--kubeconfig={kubeconfig_quoted} " if kubeconfig_quoted else ""


def wait_for_all_pods_ready(kubeconfig_quoted=None, timeout=PODS_READY_TIMEOUT_S):
    """Wartet auf 1x database, 2x backend-api, 2x frontend. False bei Timeout."""
    prefix = f"kubectl {kc(kubeconfig_quoted)}"
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        vals = []
        for d in ("database", "backend-api", "frontend"):
            _, v, _ = run_cmd(
                f"{prefix}get deploy {d} -o jsonpath='{{.status.readyReplicas}}'",
                log_cmd=False,
            )
            vals.append(v)
        if vals == ["1", "2", "2"]:
            return True
        time.sleep(0.5)
    return False


def destroy_all():
    """Simuliert den Totalausfall und prüft, dass der Cluster wirklich weg ist."""
    run_cmd(f"terraform -chdir={Q_INFRA} destroy -auto-approve", log_output=False)
    run_cmd(f"kind delete cluster --name {CLUSTER_NAME}", log_output=False)
    if ADMIN_KUBECONFIG.exists():
        ADMIN_KUBECONFIG.unlink()
    _, out, _ = run_cmd("kind get clusters", log_cmd=False)
    gone = CLUSTER_NAME not in out.split()
    if not gone:
        logging.error("Cluster existiert nach destroy noch.")
    return gone


class Recorder:
    def __init__(self, path):
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=FIELDS)
        self.w.writeheader()
        self.f.flush()

    def add(self, **row):
        row["ts_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.w.writerow({k: row.get(k, "") for k in FIELDS})
        self.f.flush()

    def close(self):
        self.f.close()


def run_cycle(model, run, order_pos, rec):
    """rec=None -> Warm-up (nicht gespeichert)."""
    is_gitops = model == "GitOps_ArgoCD"
    logging.info(
        f"[{model}] Lauf {run} (Position {order_pos}): simuliere Totalausfall ..."
    )

    def emit(**kw):
        if rec is not None:
            rec.add(model=model, run=run, order_pos=order_pos, **kw)

    if not destroy_all():
        emit(valid=0, note="Cluster nach destroy noch vorhanden")
        return

    STEPS["n"] = 0
    t_total = time.perf_counter()

    # PHASE 1: Cluster-Provisioning (identischer Terraform-Befehl in beiden Modellen)
    t = time.perf_counter()
    rc, _, _ = step(
        f"terraform -chdir={Q_INFRA} apply -target=kind_cluster.bachelor_cluster -auto-approve",
        log_output=False,
    )
    phase1 = round(time.perf_counter() - t, 2)
    if rc != 0:
        emit(valid=0, phase1_cluster_s=phase1, note="Phase 1 fehlgeschlagen")
        return
    logging.info(f"  -> Phase 1: {phase1} s")

    phase2 = ""
    if is_gitops:
        # PHASE 2: Operator-Bootstrap (ArgoCD per Helm via Terraform)
        t = time.perf_counter()
        rc, _, _ = step(
            f"terraform -chdir={Q_INFRA} apply -auto-approve", log_output=False
        )
        phase2 = round(time.perf_counter() - t, 2)
        if rc != 0:
            emit(
                valid=0,
                phase1_cluster_s=phase1,
                phase2_operator_s=phase2,
                note="Phase 2 fehlgeschlagen",
            )
            return
        logging.info(f"  -> Phase 2: {phase2} s")

        # PHASE 3: Application einspielen, ArgoCD synchronisiert die Workloads
        t = time.perf_counter()
        rc, _, err = step(f"kubectl apply -f {Q_APP_YAML}")
        if rc != 0:
            emit(
                valid=0,
                phase1_cluster_s=phase1,
                phase2_operator_s=phase2,
                note=f"Application-Apply fehlgeschlagen: {err}",
            )
            return
        ready = wait_for_all_pods_ready()
        credentials = 0
    else:
        # PHASE 3: Admin-Kubeconfig exportieren, Workloads imperativ ausrollen
        t = time.perf_counter()
        rc, _, err = step(f"kind get kubeconfig --name {CLUSTER_NAME} > {Q_KUBECONFIG}")
        if rc == 0:
            rc, _, err = step(f"kubectl {kc(Q_KUBECONFIG)}apply -f {Q_MANIFEST}")
        if rc != 0:
            emit(
                valid=0,
                phase1_cluster_s=phase1,
                note=f"Push-Rollout fehlgeschlagen: {err}",
            )
            return
        for d in ("database", "backend-api", "frontend"):
            run_cmd(
                f"kubectl {kc(Q_KUBECONFIG)}rollout status deploy/{d} --timeout=120s",
                log_cmd=False,
            )
        ready = wait_for_all_pods_ready(Q_KUBECONFIG)
        credentials = (
            1  # Admin-Zugangsdaten verlassen den Cluster (Export in externes System)
        )

    phase3 = round(time.perf_counter() - t, 2)
    total = round(time.perf_counter() - t_total, 2)
    n_commands = STEPS["n"]

    if not ready:
        emit(
            valid=0,
            phase1_cluster_s=phase1,
            phase2_operator_s=phase2,
            phase3_workload_s=phase3,
            n_commands=n_commands,
            n_credential_transfers=credentials,
            note="Pods nicht innerhalb des Timeouts ready",
        )
        return

    logging.info(
        f"  -> Phase 3: {phase3} s | Gesamt: {total} s | Befehle: {n_commands}"
    )
    emit(
        valid=1,
        phase1_cluster_s=phase1,
        phase2_operator_s=phase2,
        phase3_workload_s=phase3,
        total_s=total,
        n_commands=n_commands,
        n_credential_transfers=credentials,
    )


def preconditions():
    for p in (INFRA_DIR, MANIFEST, APP_YAML):
        if not p.exists():
            logging.critical(f"FATAL: Pfad nicht gefunden: {p}")
            sys.exit(1)
    for label, cmd in [
        ("terraform", "terraform version"),
        ("kind", "kind version"),
        ("kubectl", "kubectl version --client"),
        ("git HEAD", "git rev-parse HEAD"),
    ]:
        _, out, _ = run_cmd(cmd, log_cmd=False)
        logging.info(f"{label}: {out.splitlines()[0] if out else 'n/a'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2, help="Läufe je Modell")
    ap.add_argument(
        "--no-warmup", action="store_true", help="Warm-up-Zyklus je Modell überspringen"
    )
    args = ap.parse_args()

    preconditions()
    rec = Recorder(CSV_FILE)
    try:
        if not args.no_warmup:
            logging.info("=== Warm-up (nicht gespeichert) ===")
            run_cycle("GitOps_ArgoCD", 0, 0, None)
            run_cycle("Push_Pipeline", 0, 0, None)

        logging.info("=== Hauptmessung (Reihenfolge wechselt je Lauf) ===")
        for run in range(1, args.runs + 1):
            order = ["GitOps_ArgoCD", "Push_Pipeline"]
            if run % 2 == 0:
                order.reverse()
            for pos, model in enumerate(order, start=1):
                run_cycle(model, run, pos, rec)
    finally:
        rec.close()
    logging.info(f"FERTIG. Ergebnisse in '{CSV_FILE}', Log in '{LOG_FILE}'.")
