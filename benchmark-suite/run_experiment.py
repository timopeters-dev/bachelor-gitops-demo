import csv
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path

# ==============================================================================
# KONFIGURATION FÜR UF1 (DRIFT DETECTION & RECOVERY)
# ==============================================================================
RUNS_PER_PATTERN = 10
TIMEOUT_SECONDS = 30.0
PUSH_CRON_INTERVAL_S = 60.0  # Festes Ausführungsintervall T für Hypothese H1a
RANDOM_SEED = 42  # Fester Seed für 100% reproduzierbare Zufallszeitpunkte

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

CSV_FILE = PROJECT_ROOT / "results" / "drift_measurements.csv"
LOG_FILE = PROJECT_ROOT / "results" / "experiment_audit.log"
MANIFEST_PATH = PROJECT_ROOT / "workloads" / "base" / "manifest.yaml"
ARGOCD_APP_PATH = PROJECT_ROOT / "workloads" / "argocd-app.yaml"

random.seed(RANDOM_SEED)

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
    """Loggt echte Aktionen (log_cmd=True), hält Stoppuhr-Polls schnell & sauber."""
    if log_cmd:
        logging.info(f"EXEC: {cmd}")
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if log_cmd and res.stdout.strip():
        logging.info(f"STDOUT: {res.stdout.strip()}")
    if log_cmd and res.stderr.strip():
        logging.warning(f"STDERR: {res.stderr.strip()}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


# ==============================================================================
# DEFINITION DER DRIFT MUSTER
# ==============================================================================
SCENARIOS = {
    "D1": {
        "name": "Replica-Drift (2 -> 5)",
        "inject": "kubectl scale deployment frontend --replicas=5",
        "verify": lambda: run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.spec.replicas}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "2",
    },
    "D2": {
        "name": "Image-Version-Drift",
        "inject": "kubectl set image deployment/backend-api api=hashicorp/http-echo:0.2.3",
        "verify": lambda: run_cmd(
            'kubectl get deploy backend-api -o jsonpath="{.spec.template.spec.containers[0].image}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "hashicorp/http-echo:1.0",
    },
    "D3": {
        "name": "Start-Parameter-Manipulation",
        "inject": 'kubectl patch deployment backend-api --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/template/spec/containers/0/args/0\\", \\"value\\": \\"-text=MANIPULIERT\\"}]"',
        "verify": lambda: "OK (Soll-Zustand)"
        in run_cmd(
            'kubectl get deploy backend-api -o jsonpath="{.spec.template.spec.containers[0].args[0]}"',
            log_cmd=False,
        )[1],
    },
    "D4": {
        "name": "Service-Port-Drift (80 -> 8080)",
        "inject": 'kubectl patch svc frontend-service --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/ports/0/targetPort\\", \\"value\\": 8080}]"',
        "verify": lambda: run_cmd(
            'kubectl get svc frontend-service -o jsonpath="{.spec.ports[0].targetPort}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "80",
    },
    "D5": {
        "name": "Komponenten-Löschung (Service entfernt)",
        "inject": "kubectl delete svc frontend-service",
        "verify": lambda: run_cmd("kubectl get svc frontend-service", log_cmd=False)[0]
        == 0,
    },
    "D6": {
        "name": "Datenbank-Env-Manipulation (REDIS_AOF_ENABLED -> yes)",
        "inject": "kubectl set env deployment/database REDIS_AOF_ENABLED=yes",
        "verify": lambda: run_cmd(
            'kubectl get deploy database -o jsonpath="{.spec.template.spec.containers[0].env[0].value}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "no",
    },
    "D7": {
        "name": "Ressourcen-Limit-Drift (128Mi -> 512Mi)",
        "inject": "kubectl set resources deployment/frontend -c=web --limits=memory=512Mi",
        "verify": lambda: run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].resources.limits.memory}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "128Mi",
    },
    "D8": {
        "name": "Schatten-Ressource (Rogue ConfigMap)",
        "inject": (
            "kubectl create configmap rogue-config --from-literal=hack=true && "
            "kubectl label configmap rogue-config argocd.argoproj.io/instance=bachelor-3tier-app app.kubernetes.io/instance=bachelor-3tier-app --overwrite && "
            'kubectl annotate configmap rogue-config argocd.argoproj.io/tracking-id="bachelor-3tier-app:/ConfigMap:default/rogue-config" --overwrite'
        ),
        "verify": lambda: run_cmd("kubectl get configmap rogue-config", log_cmd=False)[
            0
        ]
        != 0,
    },
    "D9": {
        "name": "SecurityContext-Eskalation",
        "inject": 'kubectl patch deployment frontend --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/template/spec/containers/0/securityContext/allowPrivilegeEscalation\\", \\"value\\": true}]"',
        "verify": lambda: run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "false",
    },
    "D10": {
        "name": "Service-Selector-Hijacking (tier=frontend -> tier=backend)",
        "inject": 'kubectl patch svc frontend-service --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/selector/tier\\", \\"value\\": \\"backend\\"}]"',
        "verify": lambda: run_cmd(
            'kubectl get svc frontend-service -o jsonpath="{.spec.selector.tier}"',
            log_cmd=False,
        )[1].replace('"', "")
        == "frontend",
    },
}


def set_argocd_selfheal(enabled: bool):
    if enabled:
        patch = '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
    else:
        patch = '{"spec":{"syncPolicy":{"automated":null}}}'
    run_cmd(
        f"kubectl patch application bachelor-3tier-app -n argocd --type merge -p '{patch}'"
    )


def get_argocd_sync_status():
    _, out, _ = run_cmd(
        'kubectl get application bachelor-3tier-app -n argocd -o jsonpath="{.status.sync.status}"',
        log_cmd=False,
    )
    return out.replace('"', "")


def wait_until_no_operation_running():
    for _ in range(40):
        _, op_phase, _ = run_cmd(
            'kubectl get application bachelor-3tier-app -n argocd -o jsonpath="{.status.operationState.phase}"',
            log_cmd=False,
        )
        if op_phase.replace('"', "") != "Running":
            break
        time.sleep(0.2)


def trigger_argocd_reconciliation():
    wait_until_no_operation_running()
    set_argocd_selfheal(True)
    patch = '{"operation":{"initiatedBy":{"username":"admin"},"sync":{"prune":true}}}'
    run_cmd(
        f"kubectl patch application bachelor-3tier-app -n argocd --type merge -p '{patch}'"
    )


def wait_for_clean_argocd():
    run_cmd("kubectl delete configmap rogue-config --ignore-not-found", log_cmd=False)
    wait_until_no_operation_running()
    set_argocd_selfheal(True)
    run_cmd(
        "kubectl annotate application bachelor-3tier-app -n argocd argocd.argoproj.io/refresh=normal --overwrite",
        log_cmd=False,
    )
    for _ in range(50):
        status = get_argocd_sync_status()
        _, op_phase, _ = run_cmd(
            'kubectl get application bachelor-3tier-app -n argocd -o jsonpath="{.status.operationState.phase}"',
            log_cmd=False,
        )
        if status == "Synced" and op_phase.replace('"', "") != "Running":
            break
        time.sleep(0.3)


def run_gitops_benchmark(results):
    logging.info("=======================================================")
    logging.info("STARTE MESSUNG: GITOPS-MODELL (ArgoCD Pull & Self-Heal)")
    logging.info("=======================================================")
    run_cmd(f"kubectl apply -f {ARGOCD_APP_PATH}")
    wait_for_clean_argocd()

    for pattern_id, data in SCENARIOS.items():
        for run in range(1, RUNS_PER_PATTERN + 1):
            logging.info(
                f"[GitOps] Muster {pattern_id} ({data['name']}) - Lauf {run}/{RUNS_PER_PATTERN}"
            )

            wait_for_clean_argocd()
            set_argocd_selfheal(False)
            time.sleep(0.5)

            # 1. Drift injizieren & echte OutOfSync-Erkennung für ALLE 10 Muster messen
            run_cmd(data["inject"])
            t_start_detect = time.perf_counter()
            detected = False
            detection_time = None

            run_cmd(
                "kubectl annotate application bachelor-3tier-app -n argocd argocd.argoproj.io/refresh=normal --overwrite"
            )
            while (time.perf_counter() - t_start_detect) < TIMEOUT_SECONDS:
                if get_argocd_sync_status() == "OutOfSync":
                    detection_time = round(time.perf_counter() - t_start_detect, 3)
                    detected = True
                    break
                time.sleep(0.1)

            logging.info(f"  -> Drift erkannt: {detected} (Dauer: {detection_time}s)")

            # 2. Reconciliation starten & Recovery messen
            t_start_recover = time.perf_counter()
            trigger_argocd_reconciliation()

            recovered = False
            recovery_time = None
            while (time.perf_counter() - t_start_recover) < TIMEOUT_SECONDS:
                if data["verify"]():
                    recovery_time = round(time.perf_counter() - t_start_recover, 3)
                    recovered = True
                    break
                time.sleep(0.2)

            logging.info(
                f"  -> Automatisch geheilt (Self-Heal): {recovered} (Dauer: {recovery_time}s)"
            )

            results.append(
                {
                    "model": "GitOps_ArgoCD",
                    "pattern": pattern_id,
                    "run": run,
                    "detected": 1 if detected else 0,
                    "detection_time_s": detection_time if detected else TIMEOUT_SECONDS,
                    "cron_sim_detection_s": (
                        detection_time if detected else TIMEOUT_SECONDS
                    ),
                    "recovered": 1 if recovered else 0,
                    "recovery_time_s": recovery_time if recovered else TIMEOUT_SECONDS,
                }
            )


def run_push_benchmark(results):
    logging.info("=======================================================")
    logging.info("STARTE MESSUNG: REFERENZSYSTEM (Klassische Push-Pipeline)")
    logging.info("=======================================================")
    run_cmd(
        'kubectl patch application bachelor-3tier-app -n argocd --type merge -p \'{"metadata":{"finalizers":null}}\''
    )
    run_cmd(f"kubectl delete -f {ARGOCD_APP_PATH} --ignore-not-found")
    run_cmd(f"kubectl apply -f {MANIFEST_PATH}")
    time.sleep(3)

    for pattern_id, data in SCENARIOS.items():
        for run in range(1, RUNS_PER_PATTERN + 1):
            logging.info(
                f"[Push-Pipeline] Muster {pattern_id} ({data['name']}) - Lauf {run}/{RUNS_PER_PATTERN}"
            )

            run_cmd(data["inject"])

            # Ereignisgesteuerte Push-Pipeline hat KEINE aktive Laufzeit-Erkennung (0%, NaN)
            detected = False
            detection_time = ""  # NaN in CSV für rein ereignisgesteuerte Push-Pipeline

            # Für H1a (Push-Pipeline mit festem Ausführungsintervall T=60s):
            # Zufälliger Eintrittszeitpunkt U(0.5, T) im Intervall
            cron_wait_s = round(random.uniform(0.5, PUSH_CRON_INTERVAL_S), 3)

            t_start_recover = time.perf_counter()
            run_cmd(f"kubectl apply -f {MANIFEST_PATH}")

            recovered = False
            recovery_time = None
            while (time.perf_counter() - t_start_recover) < 5.0:
                if data["verify"]():
                    recovery_time = round(time.perf_counter() - t_start_recover, 3)
                    recovered = True
                    break
                time.sleep(0.2)

            logging.info(
                f"  -> Durch Push-Lauf ('kubectl apply') behoben: {recovered} (Dauer: {recovery_time}s)"
            )

            results.append(
                {
                    "model": "Push_Pipeline",
                    "pattern": pattern_id,
                    "run": run,
                    "detected": 0,
                    "detection_time_s": detection_time,
                    "cron_sim_detection_s": cron_wait_s,
                    "recovered": 1 if recovered else 0,
                    "recovery_time_s": recovery_time if recovered else TIMEOUT_SECONDS,
                }
            )

            if not recovered:
                run_cmd(
                    "kubectl delete configmap rogue-config --ignore-not-found",
                    log_cmd=False,
                )
                run_cmd(f"kubectl apply -f {MANIFEST_PATH}", log_cmd=False)


if __name__ == "__main__":
    all_results = []
    run_gitops_benchmark(all_results)
    run_push_benchmark(all_results)

    with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "model",
                "pattern",
                "run",
                "detected",
                "detection_time_s",
                "cron_sim_detection_s",
                "recovered",
                "recovery_time_s",
            ],
        )
        writer.writeheader()
        writer.writerows(all_results)

    logging.info(f"FERTIG! Alle Messdaten wurden in '{CSV_FILE}' gespeichert.")
    logging.info(f"Audit-Log wurde in '{LOG_FILE}' gespeichert.")
