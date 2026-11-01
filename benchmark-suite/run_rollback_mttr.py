"""
UF2: Release-Rollback / MTTR (GitOps vs. Push)

Aufruf:
    python run_rollback_mttr.py --runs 2                    # Pilot
    python run_rollback_mttr.py --runs 10                   # Vollmessung
    python run_rollback_mttr.py --runs 10 --polling-runs 3  # zusätzlich Polling-Serie
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

APP_YAML = PROJECT_ROOT / "workloads" / "argocd-app.yaml"
REL_MANIFEST = "workloads/base/manifest.yaml"
MANIFEST = PROJECT_ROOT / REL_MANIFEST
ADMIN_KUBECONFIG = SCRIPT_DIR / "admin-kubeconfig.yaml"

Q_APP_YAML = shlex.quote(str(APP_YAML))
Q_MANIFEST = shlex.quote(REL_MANIFEST)
Q_KUBECONFIG = shlex.quote(str(ADMIN_KUBECONFIG))

CSV_FILE = SCRIPT_DIR / "results" / "mttr_measurements_v2.csv"
LOG_FILE = SCRIPT_DIR / "results" / "mttr_audit_v2.log"
os.makedirs(CSV_FILE.parent, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

# ==============================================================================
# KONFIGURATION
# ==============================================================================
APP = "bachelor-3tier-app"
GOOD_IMAGE = "nginx:1.27-alpine"
BAD_IMAGE = "nginx:broken-release-999"

HEALTHY_TIMEOUT_S = 120.0  # max. Dauer bis Frontend wieder gesund (Rollback)
POLLING_TIMEOUT_S = 420.0  # max. Dauer im Polling-Modus (ohne Refresh-Annotation)
BROKEN_ACTIVE_TIMEOUT_S = 90.0
STABLE_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 0.1
COOLDOWN_SECONDS = 3.0

FIELDS = [
    "ts_utc",
    "model",
    "run",
    "order_pos",
    "valid",
    "stable_wait_s",
    "mttr_seconds",
    "n_commands",
    "uses_admin_kubeconfig",
    "broken_sha",
    "note",
]

STEPS = {"n": 0}
STATE = {"mode": None}  # aktueller Cluster-Modus: "gitops" oder "push"


def run_cmd(cmd, log_cmd=True):
    if log_cmd:
        logging.info(f"EXEC: {cmd}")
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if log_cmd and res.stdout.strip():
        logging.info(f"STDOUT: {res.stdout.strip()}")
    if log_cmd and res.stderr.strip():
        logging.warning(f"STDERR: {res.stderr.strip()}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def step(cmd):
    """Zustandsändernder Rollback-Befehl: wird gezählt (n_commands)."""
    STEPS["n"] += 1
    return run_cmd(cmd)


def jp(resource, path, ns=None):
    n = f"-n {ns} " if ns else ""
    return run_cmd(f"kubectl get {resource} {n}-o jsonpath='{path}'", log_cmd=False)[1]


# ==============================================================================
# KLUSTER-ZUSTAND
# ==============================================================================
def frontend_healthy():
    img = jp("deploy frontend", "{.spec.template.spec.containers[0].image}")
    ready = jp("deploy frontend", "{.status.readyReplicas}")
    updated = jp("deploy frontend", "{.status.updatedReplicas}")
    return img == GOOD_IMAGE and ready == "2" and updated == "2"


def frontend_has_bad_image():
    return BAD_IMAGE in jp(
        "deploy frontend", "{.spec.template.spec.containers[0].image}"
    )


def wait_until(cond, timeout, origin=None):
    """Wartet bis cond() wahr ist. Gibt Sekunden seit 'origin' zurück, bei Timeout None."""
    origin = origin if origin is not None else time.perf_counter()
    t_loop = time.perf_counter()
    while (time.perf_counter() - t_loop) < timeout:
        if cond():
            return round(time.perf_counter() - origin, 3)
        time.sleep(POLL_INTERVAL_S)
    return None


def argocd_field(path):
    return jp(f"application {APP}", path, ns="argocd")


def argocd_refresh():
    """Simulierter Webhook: löst den Refresh-Codepfad aus (ohne externe HTTP-Latenz)."""
    run_cmd(
        f"kubectl annotate application {APP} -n argocd argocd.argoproj.io/refresh=normal --overwrite",
        log_cmd=False,
    )


def argocd_stable():
    return (
        argocd_field("{.status.sync.status}") == "Synced"
        and argocd_field("{.status.health.status}") == "Healthy"
        and argocd_field("{.status.operationState.phase}") != "Running"
    )


# ==============================================================================
# GIT
# ==============================================================================
def git_out(cmd):
    return run_cmd(cmd, log_cmd=False)[1]


def manifest_text():
    return MANIFEST.read_text(encoding="utf-8")


def repo_is_good():
    """Arbeitsbaum sauber und Manifest enthält das gute Image."""
    dirty = git_out(f"git status --porcelain -- {Q_MANIFEST}")
    text = manifest_text()
    return dirty == "" and GOOD_IMAGE in text and BAD_IMAGE not in text


def write_image(old, new):
    text = manifest_text()
    if old not in text:
        return False
    MANIFEST.write_text(text.replace(old, new), encoding="utf-8")
    return True


def commit_and_push(message):
    rc, _, _ = run_cmd(f"git add {Q_MANIFEST}", log_cmd=False)
    if rc != 0:
        return False
    rc, _, _ = run_cmd(f"git commit -m {shlex.quote(message)}")
    if rc != 0:
        return False
    rc, _, _ = run_cmd("git push")
    return rc == 0


def recover_repo():
    """Stellt Repository und Arbeitsbaum auf den guten Stand zurück (nicht gemessen)."""
    run_cmd(
        f"git restore --staged --worktree --source=HEAD -- {Q_MANIFEST}", log_cmd=False
    )
    if BAD_IMAGE in manifest_text():
        logging.warning(
            "Recovery: Manifest enthält fehlerhaftes Image, stelle gutes Image wieder her."
        )
        write_image(BAD_IMAGE, GOOD_IMAGE)
        if not commit_and_push("UF2 recovery: restore good image"):
            logging.critical(
                "FATAL: Recovery-Commit fehlgeschlagen. Repository manuell prüfen."
            )
            sys.exit(1)
    if not repo_is_good():
        logging.critical(
            "FATAL: Repository nach Recovery nicht im guten Zustand. Abbruch."
        )
        sys.exit(1)


# ==============================================================================
# MODUSWECHSEL (GitOps <-> Push)
# ==============================================================================
def switch_to_gitops():
    logging.info("--- Wechsel in den GitOps-Modus ---")
    rc, _, err = run_cmd(f"kubectl apply -f {Q_APP_YAML}")
    if rc != 0:
        logging.critical(f"FATAL: Application nicht anlegbar: {err}")
        sys.exit(1)
    patch = '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
    rc, _, err = run_cmd(
        f"kubectl patch application {APP} -n argocd --type merge -p '{patch}'"
    )
    if rc != 0:
        logging.critical(f"FATAL: syncPolicy nicht setzbar: {err}")
        sys.exit(1)
    argocd_refresh()
    wait_until(argocd_stable, STABLE_TIMEOUT_S)
    STATE["mode"] = "gitops"


def switch_to_push():
    logging.info("--- Wechsel in den Push-Modus ---")
    run_cmd(
        f"kubectl patch application {APP} -n argocd --type merge "
        '-p \'{"metadata":{"finalizers":null}}\'',
        log_cmd=False,
    )
    run_cmd(f"kubectl delete -f {Q_APP_YAML} --ignore-not-found", log_cmd=False)
    rc, _, _ = run_cmd(f"kubectl get application {APP} -n argocd", log_cmd=False)
    if rc == 0:
        logging.critical(
            "FATAL: Application existiert noch. Push-Messung nur ohne ArgoCD. Abbruch."
        )
        sys.exit(1)
    rc, _, err = run_cmd(f"kind get kubeconfig --name bachelor-demo > {Q_KUBECONFIG}")
    if rc != 0 or not ADMIN_KUBECONFIG.is_file():
        logging.critical(f"FATAL: Admin-Kubeconfig nicht erzeugbar: {err}")
        sys.exit(1)
    STATE["mode"] = "push"


def ensure_mode(mode):
    if STATE["mode"] != mode:
        (switch_to_gitops if mode == "gitops" else switch_to_push)()


def ensure_good_state(mode):
    """Stellt sicher: Repo gut, Frontend gesund (ggf. ArgoCD Synced/Healthy). Gibt (ok, Sekunden) zurück."""
    t0 = time.perf_counter()
    if not repo_is_good():
        recover_repo()
    if mode == "push":
        rc, _, err = run_cmd(
            f"kubectl --kubeconfig={Q_KUBECONFIG} apply -f {Q_MANIFEST}", log_cmd=False
        )
        if rc != 0:
            logging.error(f"Push-Ausgangszustand nicht herstellbar: {err}")
            return False, round(time.perf_counter() - t0, 3)
    else:
        argocd_refresh()
    ok = wait_until(frontend_healthy, HEALTHY_TIMEOUT_S) is not None
    if ok and mode == "gitops":
        ok = wait_until(argocd_stable, STABLE_TIMEOUT_S) is not None
    if not ok:
        logging.warning("Ausgangszustand nicht innerhalb der Timeouts stabil.")
    return ok, round(time.perf_counter() - t0, 3)


# ==============================================================================
# CSV
# ==============================================================================
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


# ==============================================================================
# EIN MESSZYKLUS
# ==============================================================================
def run_cycle(model, run, order_pos, rec):
    """rec=None -> Warm-up (nicht gespeichert)."""
    is_gitops = model.startswith("GitOps")
    polling = model == "GitOps_ArgoCD_Polling"
    mode = "gitops" if is_gitops else "push"
    logging.info(f"[{model}] Lauf {run} (Position {order_pos})")

    def emit(**kw):
        if rec is not None:
            rec.add(
                model=model,
                run=run,
                order_pos=order_pos,
                uses_admin_kubeconfig=int(not is_gitops),
                **kw,
            )

    ensure_mode(mode)
    stable, stable_wait = ensure_good_state(mode)
    if not stable:
        emit(valid=0, stable_wait_s=stable_wait, note="Ausgangszustand instabil")
        recover_repo()
        return

    good_sha = git_out("git rev-parse HEAD")

    # 1. Fehlerhaften Release per Commit einspielen (nicht gemessen)
    if not write_image(GOOD_IMAGE, BAD_IMAGE) or not commit_and_push(
        f"UF2 faulty release {model} run {run}"
    ):
        emit(valid=0, stable_wait_s=stable_wait, note="Fehl-Commit fehlgeschlagen")
        recover_repo()
        return
    broken_sha = git_out("git rev-parse HEAD")

    if is_gitops:
        argocd_refresh()  # Deployment des Fehlreleases immer beschleunigt (nicht gemessen)
    else:
        rc, _, err = run_cmd(
            f"kubectl --kubeconfig={Q_KUBECONFIG} apply -f {Q_MANIFEST}"
        )
        if rc != 0:
            emit(
                valid=0,
                stable_wait_s=stable_wait,
                broken_sha=broken_sha,
                note=f"Push-Deploy des Fehlreleases fehlgeschlagen: {err}",
            )
            recover_repo()
            return

    if wait_until(frontend_has_bad_image, BROKEN_ACTIVE_TIMEOUT_S) is None:
        emit(
            valid=0,
            stable_wait_s=stable_wait,
            broken_sha=broken_sha,
            note="Fehlerhaftes Image wurde nicht aktiv",
        )
        recover_repo()
        return
    logging.info("  -> Fehlerhafter Release aktiv. Starte Rollback (Timer).")

    # 2. Rollback (gemessen)
    STEPS["n"] = 0
    t0 = time.perf_counter()
    failed = None
    if is_gitops:
        rc, _, err = step(f"git revert {broken_sha} --no-edit")
        if rc == 0:
            rc, _, err = step("git push")
        if rc != 0:
            failed = f"GitOps-Rollback fehlgeschlagen: {err}"
        elif not polling:
            argocd_refresh()
    else:
        rc, _, err = step("git fetch origin main")
        if rc == 0:
            rc, _, err = step(f"git checkout {good_sha} -- {Q_MANIFEST}")
        if rc == 0:
            rc, _, err = step(
                f"kubectl --kubeconfig={Q_KUBECONFIG} apply -f {Q_MANIFEST}"
            )
        if rc != 0:
            failed = f"Push-Rollback fehlgeschlagen: {err}"
        else:
            run_cmd(
                f"kubectl --kubeconfig={Q_KUBECONFIG} rollout status deployment/frontend --timeout=60s",
                log_cmd=False,
            )

    mttr = None
    if failed is None:
        timeout = POLLING_TIMEOUT_S if polling else HEALTHY_TIMEOUT_S
        mttr = wait_until(frontend_healthy, timeout, origin=t0)
    n_commands = STEPS["n"]

    if mttr is None:
        emit(
            valid=0,
            stable_wait_s=stable_wait,
            broken_sha=broken_sha,
            n_commands=n_commands,
            note=failed or "Timeout bis Frontend gesund",
        )
    else:
        logging.info(
            f"  -> Rollback abgeschlossen: MTTR {mttr} s ({n_commands} Befehle)"
        )
        emit(
            valid=1,
            stable_wait_s=stable_wait,
            mttr_seconds=mttr,
            n_commands=n_commands,
            broken_sha=broken_sha,
        )

    # 3. Aufräumen (nicht gemessen): Repository wieder auf guten Stand
    if not is_gitops:
        run_cmd(
            f"git restore --staged --worktree --source=HEAD -- {Q_MANIFEST}",
            log_cmd=False,
        )
        rc, _, _ = run_cmd(f"git revert {broken_sha} --no-edit")
        if rc == 0:
            rc, _, _ = run_cmd("git push")
        if rc != 0:
            logging.warning("Aufräum-Revert fehlgeschlagen, starte Recovery.")
    recover_repo()
    time.sleep(COOLDOWN_SECONDS)


# ==============================================================================
# START
# ==============================================================================
def log_environment():
    for label, cmd in [
        ("git HEAD", "git rev-parse HEAD"),
        ("kubectl", "kubectl version --client"),
        (
            "argocd-controller image",
            "kubectl get statefulset argocd-application-controller -n argocd "
            "-o jsonpath='{.spec.template.spec.containers[0].image}'",
        ),
        (
            "argocd-cmd-params-cm",
            "kubectl get cm argocd-cmd-params-cm -n argocd -o jsonpath='{.data}'",
        ),
    ]:
        _, out, _ = run_cmd(cmd, log_cmd=False)
        logging.info(f"{label}: {out}")


def preconditions():
    for p in (APP_YAML, MANIFEST):
        if not p.is_file():
            logging.critical(f"FATAL: Datei nicht gefunden: {p}")
            sys.exit(1)
    rc, out, _ = run_cmd("git rev-parse --is-inside-work-tree", log_cmd=False)
    if rc != 0 or out != "true":
        logging.critical("FATAL: Kein Git-Arbeitsbaum im Projektverzeichnis.")
        sys.exit(1)
    run_cmd("git fetch origin", log_cmd=False)
    head = git_out("git rev-parse HEAD")
    upstream = git_out("git rev-parse @{u}")
    if not upstream or head != upstream:
        logging.critical(
            "FATAL: HEAD und Upstream stimmen nicht überein (pull/push zuerst)."
        )
        sys.exit(1)
    if not repo_is_good():
        logging.critical(
            "FATAL: Manifest ist nicht sauber/gut (Image oder uncommittete Änderungen)."
        )
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2, help="Läufe je Modell")
    ap.add_argument(
        "--polling-runs",
        type=int,
        default=0,
        help="zusätzliche GitOps-Läufe ohne simulierten Webhook (Standard-Polling)",
    )
    ap.add_argument(
        "--no-warmup", action="store_true", help="Warm-up-Zyklus je Modell überspringen"
    )
    args = ap.parse_args()

    preconditions()
    log_environment()
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

        if args.polling_runs > 0:
            logging.info(
                "=== Zusatzserie: GitOps ohne simulierten Webhook (Polling) ==="
            )
            for run in range(1, args.polling_runs + 1):
                run_cycle("GitOps_ArgoCD_Polling", run, 1, rec)
    finally:
        try:
            recover_repo()
        finally:
            rec.close()
    logging.info(f"FERTIG. Ergebnisse in '{CSV_FILE}', Log in '{LOG_FILE}'.")
