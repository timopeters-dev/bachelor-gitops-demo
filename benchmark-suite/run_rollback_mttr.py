import csv
import logging
import os
import subprocess
import sys
import time

# ==============================================================================
# KONFIGURATION FÜR UF2 / HYPOTHESE 2 (MTTR BEI FEHLERHAFTEM RELEASE)
# ==============================================================================
RUNS = 1  # Für finales Experiment auf 10 stellen
CSV_FILE = "results/mttr_measurements.csv"
LOG_FILE = "results/mttr_audit.log"
MANIFEST_PATH = "workloads/base/manifest.yaml"

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


def is_frontend_healthy():
    """Prüft, ob das Frontend wieder mit dem korrekten Image und 2 Ready-Pods läuft."""
    _, img, _ = run_cmd(
        'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].image}"',
        log_cmd=False,
    )
    _, ready, _ = run_cmd(
        'kubectl get deploy frontend -o jsonpath="{.status.readyReplicas}"',
        log_cmd=False,
    )
    _, updated, _ = run_cmd(
        'kubectl get deploy frontend -o jsonpath="{.status.updatedReplicas}"',
        log_cmd=False,
    )
    return (
        img.replace('"', "") == "nginx:1.27-alpine"
        and ready.replace('"', "") == "2"
        and updated.replace('"', "") == "2"
    )


def inject_broken_release_in_file(broken=True):
    """Ändert das Image in der manifest.yaml auf eine kaputte Version oder zurück."""
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    if broken:
        content = content.replace(
            "image: nginx:1.27-alpine", "image: nginx:broken-release-999"
        )
    else:
        content = content.replace(
            "image: nginx:broken-release-999", "image: nginx:1.27-alpine"
        )
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def run_gitops_mttr(results):
    logging.info("=======================================================")
    logging.info("STARTE UF2-MESSUNG: GITOPS ROLLBACK (git revert -> ArgoCD)")
    logging.info("=======================================================")
    run_cmd("kubectl apply -f workloads/argocd-app.yaml")
    time.sleep(3)

    for run in range(1, RUNS + 1):
        logging.info(f"\n[GitOps MTTR] Durchlauf {run}/{RUNS}")

        # 1. Fehlerhaftes Release via Git einspielen
        inject_broken_release_in_file(broken=True)
        run_cmd(
            f'git add {MANIFEST_PATH} && git commit -m "Faulty release run {run}" && git push'
        )
        run_cmd(
            "kubectl annotate application bachelor-3tier-app -n argocd argocd.argoproj.io/refresh=normal --overwrite"
        )

        # Warten, bis der fehlerhafte Zustand im Cluster ausgerollt wurde
        for _ in range(30):
            _, img, _ = run_cmd(
                'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].image}"',
                log_cmd=False,
            )
            if "broken-release-999" in img:
                break
            time.sleep(0.3)

        logging.info(
            "  -> Fehlerhaftes Release ist im Cluster aktiv. Starte Git-Rollback (MTTR-Stoppuhr)..."
        )

        # 2. ROLLBACK STARTEN (Stoppuhr läuft ab 'git revert')
        t_start = time.perf_counter()
        run_cmd("git revert HEAD --no-edit && git push")
        # Webhook-Benachrichtigung von GitHub an ArgoCD simulieren
        run_cmd(
            "kubectl annotate application bachelor-3tier-app -n argocd argocd.argoproj.io/refresh=normal --overwrite"
        )

        while not is_frontend_healthy():
            time.sleep(0.1)

        mttr = round(time.perf_counter() - t_start, 3)
        logging.info(f"  -> GitOps Rollback erfolgreich! MTTR: {mttr}s")

        results.append(
            {
                "model": "GitOps_ArgoCD",
                "run": run,
                "mttr_seconds": mttr,
                "manual_steps": 1,  # 1 Schritt: 'git revert && git push'
            }
        )


def run_push_mttr(results):
    logging.info("\n=======================================================")
    logging.info("STARTE UF2-MESSUNG: PUSH-PIPELINE ROLLBACK (Redeploy Vorversion)")
    logging.info("=======================================================")
    run_cmd(
        'kubectl patch application bachelor-3tier-app -n argocd --type merge -p \'{"metadata":{"finalizers":null}}\''
    )
    run_cmd("kubectl delete -f workloads/argocd-app.yaml --ignore-not-found")
    inject_broken_release_in_file(broken=False)
    run_cmd("kind get kubeconfig --name bachelor-demo > admin-kubeconfig.yaml")
    run_cmd(f"kubectl --kubeconfig=admin-kubeconfig.yaml apply -f {MANIFEST_PATH}")
    time.sleep(3)

    for run in range(1, RUNS + 1):
        logging.info(f"\n[Push-Pipeline MTTR] Durchlauf {run}/{RUNS}")

        # 1. Fehlerhaftes Release per Push-Pipeline ausrollen
        run_cmd(
            "kubectl --kubeconfig=admin-kubeconfig.yaml set image deployment/frontend web=nginx:broken-release-999"
        )
        # Warten, bis der fehlerhafte Container-Versuch im Cluster registriert ist
        for _ in range(30):
            _, img, _ = run_cmd(
                'kubectl get deploy frontend -o jsonpath="{.spec.template.spec.containers[0].image}"',
                log_cmd=False,
            )
            if "broken-release-999" in img:
                break
            time.sleep(0.3)

        logging.info(
            "  -> Fehlerhaftes Release ist aktiv. Starte Pipeline-Redeployment (MTTR-Stoppuhr)..."
        )

        # 2. ROLLBACK STARTEN: Fairer Vergleich inkl. Git-Netzwerk-Fetch & externer Kubeconfig
        t_start = time.perf_counter()
        # Schritt A: CI/CD-Runner holt den letzten funktionierenden Stand über das Netzwerk von GitHub
        run_cmd(f"git fetch origin main && git checkout origin/main -- {MANIFEST_PATH}")
        # Schritt B: Externer Verbindungsaufbau via Admin-Kubeconfig ("God-Mode") & Push-Deployment
        run_cmd(f"kubectl --kubeconfig=admin-kubeconfig.yaml apply -f {MANIFEST_PATH}")
        run_cmd(
            "kubectl --kubeconfig=admin-kubeconfig.yaml rollout status deployment/frontend --timeout=30s"
        )

        while not is_frontend_healthy():
            time.sleep(0.1)

        mttr = round(time.perf_counter() - t_start, 3)
        logging.info(f"  -> Push-Pipeline Rollback erfolgreich! MTTR: {mttr}s")

        results.append(
            {
                "model": "Push_Pipeline",
                "run": run,
                "mttr_seconds": mttr,
                "manual_steps": 2,
            }
        )


if __name__ == "__main__":
    results = []
    run_gitops_mttr(results)
    run_push_mttr(results)

    with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["model", "run", "mttr_seconds", "manual_steps"]
        )
        writer.writeheader()
        writer.writerows(results)

    logging.info(f"FERTIG! MTTR-Messergebnisse gespeichert in '{CSV_FILE}'.")
