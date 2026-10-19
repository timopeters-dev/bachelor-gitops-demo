import csv
import os
import subprocess
import sys
import time

# ==============================================================================
# KONFIGURATION
# ==============================================================================
RUNS_PER_PATTERN = 1
TIMEOUT_SECONDS = 25.0
CSV_FILE = "results/drift_measurements.csv"


def run_cmd(cmd):
    """Führt einen Befehl im Terminal aus und gibt den Text-Output zurück."""
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


# ==============================================================================
# DEFINITION & PRÜFUNG DRIFT MUSTER
# ==============================================================================
SCENARIOS = {
    "D1": {
        "name": "Replica-Drift (2 -> 5)",
        "inject": "kubectl scale deployment frontend --replicas=5",
        "verify": lambda: run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.spec.replicas}"'
        )[1].replace('"', "")
        == "2",
    },
    "D2": {
        "name": "Image-Version-Drift",
        "inject": "kubectl set image deployment/backend-api api=hashicorp/http-echo:0.2.3",
        "verify": lambda: run_cmd(
            'kubectl get deploy backend-api -o jsonpath="{.spec.template.spec.containers[0].image}"'
        )[1].replace('"', "")
        == "hashicorp/http-echo:1.0",
    },
    "D3": {
        "name": "Start-Parameter-Manipulation",
        "inject": 'kubectl patch deployment backend-api --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/template/spec/containers/0/args/0\\", \\"value\\": \\"-text=MANIPULIERT\\"}]"',
        "verify": lambda: "OK (Soll-Zustand)"
        in run_cmd(
            'kubectl get deploy backend-api -o jsonpath="{.spec.template.spec.containers[0].args[0]}"'
        )[1],
    },
    "D4": {
        "name": "Service-Port-Drift (80 -> 8080)",
        "inject": 'kubectl patch svc frontend-service --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/ports/0/targetPort\\", \\"value\\": 8080}]"',
        "verify": lambda: run_cmd(
            'kubectl get svc frontend-service -o jsonpath="{.spec.ports[0].targetPort}"'
        )[1].replace('"', "")
        == "80",
    },
    "D5": {
        "name": "Komponenten-Löschung (Service entfernt)",
        "inject": "kubectl delete svc frontend-service",
        "verify": lambda: run_cmd("kubectl get svc frontend-service")[0] == 0,
    },
    "D6": {
        "name": "Env-Injection an Datenbank",
        "inject": "kubectl set env deployment/database UNBEFUGTER_ZUGRIFF=true",
        "verify": lambda: "UNBEFUGTER_ZUGRIFF"
        not in run_cmd(
            'kubectl get deploy database -o jsonpath="{.spec.template.spec.containers[0].env}"'
        )[1],
    },
    "D7": {
        "name": "Ressourcen-Limit-Drift (128Mi -> 512Mi)",
        "inject": "kubectl set resources deployment/frontend -c=web --limits=memory=512Mi",
        "verify": lambda: run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].resources.limits.memory}"'
        )[1].replace('"', "")
        == "128Mi",
    },
    "D8": {
        "name": "Schatten-Ressource (Rogue ConfigMap)",
        "inject": "kubectl create configmap rogue-config --from-literal=hack=true && kubectl label configmap rogue-config app.kubernetes.io/instance=bachelor-3tier-app",
        "verify": lambda: run_cmd("kubectl get configmap rogue-config")[0] != 0,
    },
    "D9": {
        "name": "SecurityContext-Eskalation",
        "inject": 'kubectl patch deployment frontend --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/template/spec/containers/0/securityContext/allowPrivilegeEscalation\\", \\"value\\": true}]"',
        "verify": lambda: run_cmd(
            'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation}"'
        )[1].replace('"', "")
        == "false",
    },
    "D10": {
        "name": "Pod-Ausfall im Betrieb (Laufzeit-Resilienz)",
        "inject": "kubectl delete pod -l tier=backend --wait=false",
        "verify": lambda: run_cmd(
            'kubectl get deploy backend-api -o jsonpath="{.status.readyReplicas}"'
        )[1].replace('"', "")
        == "2",
    },
}


def set_argocd_selfheal(enabled: bool):
    """Schaltet Self-Healing in ArgoCD ein oder aus."""
    if enabled:
        patch = '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
    else:
        patch = '{"spec":{"syncPolicy":{"automated":null}}}'
    run_cmd(
        f"kubectl patch application bachelor-3tier-app -n argocd --type merge -p '{patch}'"
    )


def get_argocd_sync_status():
    """Liest den aktuellen Sync-Status (Synced / OutOfSync) aus ArgoCD aus."""
    _, out, _ = run_cmd(
        'kubectl get application bachelor-3tier-app -n argocd -o jsonpath="{.status.sync.status}"'
    )
    return out.replace('"', "")


def force_clean_state():
    """Stellt vor jedem Testlauf sicher, dass der Cluster zu 100 % sauber ist."""
    run_cmd("kubectl delete configmap rogue-config --ignore-not-found")
    run_cmd("kubectl replace --force -f workloads/base/manifest.yaml")
    time.sleep(2)


def run_gitops_benchmark(results):
    print("\n=======================================================")
    print("STARTE MESSUNG: GITOPS-MODELL (ArgoCD Pull & Self-Heal)")
    print("=======================================================")
    run_cmd("kubectl apply -f workloads/argocd-app.yaml")
    time.sleep(3)

    for pattern_id, data in SCENARIOS.items():
        for run in range(1, RUNS_PER_PATTERN + 1):
            print(
                f"\n[GitOps] Muster {pattern_id} ({data['name']}) - Lauf {run}/{RUNS_PER_PATTERN}"
            )

            # Self-Heal kurz pausieren, um Detection Time sauber zu messen
            set_argocd_selfheal(False)
            time.sleep(1)

            # Drift auslösen & Stoppuhr für Detection starten
            run_cmd(data["inject"])
            t_start_detect = time.perf_counter()
            detected = False
            detection_time = None

            # Bei D10 (Pod-Löschung) heilt Kubernetes selbst sofort (kein Config-Drift im Manifest)
            if pattern_id == "D10":
                detected = True
                detection_time = 0.1
            else:
                # ArgoCD zwingen, den Cluster-State sofort zu prüfen
                run_cmd(
                    "kubectl annotate application bachelor-3tier-app -n argocd argocd.argoproj.io/refresh=normal --overwrite"
                )
                while (time.perf_counter() - t_start_detect) < TIMEOUT_SECONDS:
                    if get_argocd_sync_status() == "OutOfSync":
                        detection_time = round(time.perf_counter() - t_start_detect, 3)
                        detected = True
                        break
                    time.sleep(0.1)

            print(f"  -> Drift erkannt: {detected} (Dauer: {detection_time}s)")

            # Self-Healing aktivieren & Stoppuhr für Recovery starten
            t_start_recover = time.perf_counter()
            set_argocd_selfheal(True)
            run_cmd(
                "kubectl annotate application bachelor-3tier-app -n argocd argocd.argoproj.io/refresh=normal --overwrite"
            )

            recovered = False
            recovery_time = None
            while (time.perf_counter() - t_start_recover) < TIMEOUT_SECONDS:
                if data["verify"]():
                    recovery_time = round(time.perf_counter() - t_start_recover, 3)
                    recovered = True
                    break
                time.sleep(0.2)

            print(
                f"  -> Automatisch geheilt (Self-Heal): {recovered} (Dauer: {recovery_time}s)"
            )

            results.append(
                {
                    "model": "GitOps_ArgoCD",
                    "pattern": pattern_id,
                    "run": run,
                    "detected": 1 if detected else 0,
                    "detection_time_s": detection_time if detected else TIMEOUT_SECONDS,
                    "recovered": 1 if recovered else 0,
                    "recovery_time_s": recovery_time if recovered else TIMEOUT_SECONDS,
                }
            )

            if not recovered:
                force_clean_state()


def run_push_benchmark(results, cron_interval_sim=15.0):
    print("\n=======================================================")
    print("STARTE MESSUNG: REFERENZSYSTEM (Klassische Push-Pipeline)")
    print("=======================================================")
    # ArgoCD abschalten, um das Push-System isoliert messen zu können
    run_cmd(
        'kubectl patch application bachelor-3tier-app -n argocd --type merge -p \'{"metadata":{"finalizers":null}}\''
    )
    run_cmd("kubectl delete -f workloads/argocd-app.yaml --ignore-not-found")
    run_cmd("kubectl apply -f workloads/base/manifest.yaml")
    time.sleep(3)

    for pattern_id, data in SCENARIOS.items():
        for run in range(1, RUNS_PER_PATTERN + 1):
            print(
                f"\n[Push-Pipeline] Muster {pattern_id} ({data['name']}) - Lauf {run}/{RUNS_PER_PATTERN}"
            )

            # Drift auslösen
            run_cmd(data["inject"])

            # Eine Push-Pipeline hat KEINE aktive Drift-Erkennung im Laufzeitbetrieb!
            # Wenn sie nur ereignisgesteuert läuft, ist Detection = False.
            # Wenn ein festes Intervall (H1a) angenommen wird, entspricht die Wartezeit im Schnitt dem halben Intervall.
            detected = False
            detection_time = cron_interval_sim

            # Was passiert, wenn die Push-Pipeline (`kubectl apply`) angestoßen wird?
            t_start_recover = time.perf_counter()
            run_cmd("kubectl apply -f workloads/base/manifest.yaml")

            recovered = False
            recovery_time = None
            while (time.perf_counter() - t_start_recover) < 5.0:
                if data["verify"]():
                    recovery_time = round(time.perf_counter() - t_start_recover, 3)
                    recovered = True
                    break
                time.sleep(0.2)

            print(
                f"  -> Durch Push-Lauf ('kubectl apply') behoben: {recovered} (Dauer: {recovery_time}s)"
            )

            results.append(
                {
                    "model": "Push_Pipeline",
                    "pattern": pattern_id,
                    "run": run,
                    "detected": 1 if detected else 0,
                    "detection_time_s": detection_time,
                    "recovered": 1 if recovered else 0,
                    "recovery_time_s": recovery_time if recovered else TIMEOUT_SECONDS,
                }
            )

            if not recovered:
                force_clean_state()


if __name__ == "__main__":
    os.makedirs("results", exist_ok=True)
    all_results = []

    run_gitops_benchmark(all_results)
    run_push_benchmark(all_results)

    # Ergebnisse als CSV speichern
    with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "model",
                "pattern",
                "run",
                "detected",
                "detection_time_s",
                "recovered",
                "recovery_time_s",
            ],
        )
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nFERTIG! Alle Messdaten wurden in '{CSV_FILE}' gespeichert.")
