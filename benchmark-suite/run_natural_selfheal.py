"""
UF1-B: Unberührtes Self-Healing (ArgoCD) vs. Push-Referenz, Muster D1-D11.

Gemessen wird: Zeit von Injektionsende (t_start) bis Verifikation des Soll-Zustands.
- GitOps: ArgoCD arbeitet während des Messfensters ohne Eingriff des Skripts
          (selfHeal/prune einmalig aktiviert).
- Push:   Das Skript startet nach der Injektion genau einen 'kubectl apply' (Admin-Kubeconfig).
          Die Push-Zeit enthält damit den Prozessstart von kubectl, die GitOps-Zeit nicht.

Zusatz (ArgoCD v2.13.x): Der Self-Heal-Backoff-Zähler (autoHealAttemptsCount) wird laut
GitHub-Issue #21599 (https://github.com/argoproj/argo-cd/issues/21599) nach erfolgreichem Heilen nicht zurückgesetzt.
Das Skript protokolliert den Zähler vor und nach jedem Lauf. Mit --reset-counter wird zwischen den Läufen (außerhalb des Messfensters)
ein manueller Sync ausgelöst; ob der Zähler dadurch sinkt, steht in der CSV.

Aufruf:
    python run_natural_selfheal.py --runs 3                    # Pilot
    python run_natural_selfheal.py --runs 3 --reset-counter    # Pilot mit Zähler-Reset
    python run_natural_selfheal.py --runs 10                   # Vollmessung
    python run_natural_selfheal.py --runs 10 --only push
"""

import argparse
import csv
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ==============================================================================
# PFADE & ARBEITSVERZEICHNIS
# ==============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
os.chdir(PROJECT_ROOT)

APP_YAML = PROJECT_ROOT / "workloads" / "argocd-app.yaml"
MANIFEST = PROJECT_ROOT / "workloads" / "base" / "manifest.yaml"
ADMIN_KUBECONFIG = SCRIPT_DIR / "admin-kubeconfig.yaml"

# Für Shell-Aufrufe gequotet (Leerzeichen im Pfad)
Q_APP_YAML = shlex.quote(str(APP_YAML))
Q_MANIFEST = shlex.quote(str(MANIFEST))
Q_KUBECONFIG = shlex.quote(str(ADMIN_KUBECONFIG))

CSV_FILE = SCRIPT_DIR / "results" / "natural_selfheal_measurements.csv"
LOG_FILE = SCRIPT_DIR / "results" / "natural_selfheal_audit.log"
os.makedirs(CSV_FILE.parent, exist_ok=True)

# ==============================================================================
# LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

if not APP_YAML.is_file():
    logging.critical(f"FATAL: Datei nicht gefunden: {APP_YAML}. Abbruch.")
    sys.exit(1)
if not MANIFEST.is_file():
    logging.critical(f"FATAL: Datei nicht gefunden: {MANIFEST}. Abbruch.")
    sys.exit(1)

# ==============================================================================
# KONFIGURATION
# ==============================================================================
WINDOW_SECONDS = 20.0  # identisches Beobachtungsfenster für alle Muster und Modelle
POLL_INTERVAL_S = 0.1
COOLDOWN_SECONDS = 6.0  # gewählte Pause zwischen Läufen
STABLE_TIMEOUT_S = 45.0  # max. Wartezeit auf stabilen ArgoCD-Zustand
ROLLOUT_TIMEOUT = "60s"
OPERATION_IDLE_TIMEOUT_S = 20.0

APP = "bachelor-3tier-app"
DEPLOYMENTS = ("database", "backend-api", "frontend")

CFG = {"reset_counter": False}  # wird per --reset-counter gesetzt

FIELDS = [
    "ts_utc",
    "model",
    "pattern",
    "run",
    "valid",
    "stable_before",
    "stable_wait_s",
    "reset_counter",
    "heal_attempts_before",
    "heal_attempts_after",
    "recovered",
    "remediation_time_s",
    "post_window_sync_status",
]


def run_cmd(cmd, log_cmd=True):
    if log_cmd:
        logging.info(f"EXEC: {cmd}")
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if log_cmd and res.stdout.strip():
        logging.info(f"STDOUT: {res.stdout.strip()}")
    if log_cmd and res.stderr.strip():
        logging.warning(f"STDERR: {res.stderr.strip()}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def jp(resource, path):
    """Liest einen jsonpath-Wert."""
    return run_cmd(f"kubectl get {resource} -o jsonpath='{path}'", log_cmd=False)[1]


# ==============================================================================
# TESTKATALOG D1-D11
# ==============================================================================
C0 = "{.spec.template.spec.containers[0]"

SCENARIOS = {
    "D1": {
        "name": "Replica-Drift (2 -> 5)",
        "inject": "kubectl scale deployment frontend --replicas=5",
        "verify": lambda: jp("deploy frontend", "{.spec.replicas}") == "2",
    },
    "D2": {
        "name": "Image-Version-Drift",
        "inject": "kubectl set image deployment/backend-api api=hashicorp/http-echo:0.2.3",
        "verify": lambda: jp("deploy backend-api", C0 + ".image}")
        == "hashicorp/http-echo:1.0",
    },
    "D3": {
        "name": "Start-Parameter-Manipulation",
        "inject": (
            "kubectl patch deployment backend-api --type=json -p "
            '\'[{"op":"replace","path":"/spec/template/spec/containers/0/args/0",'
            '"value":"-text=MANIPULIERT"}]\''
        ),
        "verify": lambda: "OK (Soll-Zustand)"
        in jp("deploy backend-api", C0 + ".args[0]}"),
    },
    "D4": {
        "name": "Service-Port-Drift (targetPort 80 -> 8080)",
        "inject": (
            "kubectl patch svc frontend-service --type=json -p "
            '\'[{"op":"replace","path":"/spec/ports/0/targetPort","value":8080}]\''
        ),
        "verify": lambda: jp("svc frontend-service", "{.spec.ports[0].targetPort}")
        == "80",
    },
    "D5": {
        "name": "Komponenten-Löschung (Service entfernt)",
        "inject": "kubectl delete svc frontend-service",
        "verify": lambda: run_cmd("kubectl get svc frontend-service", log_cmd=False)[0]
        == 0,
    },
    "D6": {
        "name": "Datenbank-Env-Modifikation (REDIS_AOF_ENABLED -> yes)",
        "inject": "kubectl set env deployment/database REDIS_AOF_ENABLED=yes",
        "verify": lambda: jp(
            "deploy database",
            C0 + '.env[?(@.name=="REDIS_AOF_ENABLED")].value}',
        )
        == "no",
    },
    "D7": {
        "name": "Ressourcen-Limit-Drift (128Mi -> 512Mi)",
        "inject": "kubectl set resources deployment/frontend -c=web --limits=memory=512Mi",
        "verify": lambda: jp("deploy frontend", C0 + ".resources.limits.memory}")
        == "128Mi",
    },
    "D8": {
        "name": "Schatten-Ressource (Rogue ConfigMap mit App-Tracking)",
        "inject": (
            "kubectl create configmap rogue-config --from-literal=hack=true && "
            f"kubectl label configmap rogue-config argocd.argoproj.io/instance={APP} "
            f"app.kubernetes.io/instance={APP} --overwrite && "
            "kubectl annotate configmap rogue-config "
            f'argocd.argoproj.io/tracking-id="{APP}:/ConfigMap:default/rogue-config" --overwrite'
        ),
        "verify": lambda: run_cmd("kubectl get configmap rogue-config", log_cmd=False)[
            0
        ]
        != 0,
    },
    "D9": {
        "name": "SecurityContext-Eskalation",
        "inject": (
            "kubectl patch deployment frontend --type=json -p "
            '\'[{"op":"replace","path":"/spec/template/spec/containers/0/'
            'securityContext/allowPrivilegeEscalation","value":true}]\''
        ),
        "verify": lambda: jp(
            "deploy frontend", C0 + ".securityContext.allowPrivilegeEscalation}"
        )
        == "false",
    },
    "D10": {
        "name": "Service-Selector-Hijacking (tier=frontend -> tier=backend)",
        "inject": (
            "kubectl patch svc frontend-service --type=json -p "
            '\'[{"op":"replace","path":"/spec/selector/tier","value":"backend"}]\''
        ),
        "verify": lambda: jp("svc frontend-service", "{.spec.selector.tier}")
        == "frontend",
    },
    "D11": {
        "name": "Additive Env-Injection (UNBEFUGTER_ZUGRIFF=true, nicht im Manifest deklariert)",
        "inject": "kubectl set env deployment/database UNBEFUGTER_ZUGRIFF=true",
        "verify": lambda: "UNBEFUGTER_ZUGRIFF"
        not in jp("deploy database", C0 + ".env[*].name}"),
    },
}


# ==============================================================================
# ARGOCD-HILFSFUNKTIONEN
# ==============================================================================
def argocd_field(path):
    return jp(f"application {APP} -n argocd", path)


def sync_status():
    return argocd_field("{.status.sync.status}")


def _find_key(obj, key):
    """Sucht einen Schlüssel rekursiv (der genaue JSON-Pfad hängt von der ArgoCD-Version ab)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                return v
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def get_heal_attempts():
    """
    Liest autoHealAttemptsCount aus dem Application-Status.
    Fehlt das Feld (omitempty bei 0), wird 0 zurückgegeben; bei Lesefehlern ein leerer String.
    """
    rc, out, _ = run_cmd(
        f"kubectl get application {APP} -n argocd -o json", log_cmd=False
    )
    if rc != 0 or not out:
        return ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return ""
    value = _find_key(data.get("status", {}), "autoHealAttemptsCount")
    return value if value is not None else 0


def wait_operation_idle(timeout=OPERATION_IDLE_TIMEOUT_S):
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        if argocd_field("{.status.operationState.phase}") != "Running":
            return True
        time.sleep(0.2)
    return False


def reset_heal_counter():
    """
    Löst außerhalb des Messfensters einen manuellen Sync aus. Laut GitHub-Issue tritt das
    Zählerproblem bei manuellem Sync nicht auf; ob der Zähler hier tatsächlich sinkt,
    zeigen heal_attempts_before/-after in der CSV.
    """
    wait_operation_idle()
    patch = '{"operation":{"initiatedBy":{"username":"admin"},"sync":{"prune":true}}}'
    rc, _, err = run_cmd(
        f"kubectl patch application {APP} -n argocd --type merge -p '{patch}'",
        log_cmd=False,
    )
    if rc != 0:
        logging.warning(f"Manueller Sync zum Zähler-Reset fehlgeschlagen: {err}")
        return
    time.sleep(0.5)
    wait_operation_idle()


def log_argocd_settings():
    """Dokumentiert Controller-Parameter und Startzeit des Controllers (wirkt der Neustart?)."""
    _, params, _ = run_cmd(
        "kubectl get cm argocd-cmd-params-cm -n argocd -o jsonpath='{.data}'",
        log_cmd=False,
    )
    logging.info(f"ArgoCD Parameter (argocd-cmd-params-cm): {params}")
    _, started, _ = run_cmd(
        "kubectl get pod -n argocd -l app.kubernetes.io/name=argocd-application-controller "
        "-o jsonpath='{.items[0].status.startTime}'",
        log_cmd=False,
    )
    logging.info(f"Startzeit Application-Controller-Pod: {started}")
    logging.info(f"Zähler-Reset zwischen Läufen aktiv: {CFG['reset_counter']}")


# ==============================================================================
# ZUSTANDSHERSTELLUNG
# ==============================================================================
def cleanup_residuals():
    """Entfernt Reste aus nicht geheilten Läufen (D8, D11)."""
    run_cmd("kubectl delete configmap rogue-config --ignore-not-found", log_cmd=False)
    if "UNBEFUGTER_ZUGRIFF" in jp("deploy database", C0 + ".env[*].name}"):
        run_cmd(
            "kubectl set env deployment/database UNBEFUGTER_ZUGRIFF-", log_cmd=False
        )


def wait_for_rollouts():
    ok = True
    for d in DEPLOYMENTS:
        rc, _, err = run_cmd(
            f"kubectl rollout status deployment/{d} --timeout={ROLLOUT_TIMEOUT}",
            log_cmd=False,
        )
        if rc != 0:
            logging.warning(f"Rollout von {d} nicht abgeschlossen: {err}")
            ok = False
    return ok


def ensure_stable(is_gitops):
    """
    Stellt den Ausgangszustand her. Gibt (stable_ok, stable_wait_s) zurück;
    stable_wait_s umfasst die gesamte Vorbereitung (Cleanup, Rollouts, Reset, Argo-Wartepunkt).
    """
    t0 = time.perf_counter()
    cleanup_residuals()

    if not is_gitops:
        rc, _, err = run_cmd(
            f"kubectl --kubeconfig={Q_KUBECONFIG} apply -f {Q_MANIFEST}",
            log_cmd=False,
        )
        if rc != 0:
            logging.error(f"Push-Ausgangszustand nicht herstellbar: {err}")
            return False, round(time.perf_counter() - t0, 3)

    ok = wait_for_rollouts()

    if is_gitops:
        if CFG["reset_counter"]:
            reset_heal_counter()

        # Eigener Startzeitpunkt: Rollout-Dauer verbraucht nicht das Argo-Timeout.
        t_loop = time.perf_counter()
        stable = False
        while (time.perf_counter() - t_loop) < STABLE_TIMEOUT_S:
            if (
                sync_status() == "Synced"
                and argocd_field("{.status.health.status}") == "Healthy"
                and argocd_field("{.status.operationState.phase}") != "Running"
            ):
                stable = True
                break
            time.sleep(0.3)
        if not stable:
            logging.warning(
                f"ArgoCD nach {STABLE_TIMEOUT_S} s nicht Synced/Healthy "
                f"(Sync: {sync_status()}, Health: {argocd_field('{.status.health.status}')})."
            )
        ok = ok and stable

    return ok, round(time.perf_counter() - t0, 3)


# ==============================================================================
# CSV
# ==============================================================================
class Recorder:
    """Schreibt jede Zeile sofort in die CSV."""

    def __init__(self, path):
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=FIELDS)
        self.w.writeheader()
        self.f.flush()

    def add(self, **row):
        row["ts_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row["reset_counter"] = int(CFG["reset_counter"])
        self.w.writerow({k: row.get(k, "") for k in FIELDS})
        self.f.flush()

    def close(self):
        self.f.close()


# ==============================================================================
# EIN MESSLAUF
# ==============================================================================
def measure_one(model, pattern_id, data, run, is_gitops, rec):
    logging.info(f"[{model}] {pattern_id} ({data['name']}) - Lauf {run}")
    stable, stable_wait_s = ensure_stable(is_gitops)
    heal_before = get_heal_attempts() if is_gitops else "N/A"
    base = dict(
        model=model,
        pattern=pattern_id,
        run=run,
        stable_wait_s=stable_wait_s,
        heal_attempts_before=heal_before,
    )

    # Instabiler Ausgangszustand: Lauf überspringen (kein Restdrift-Kaskadeneffekt).
    # 'recovered' bleibt bei ungültigen Läufen leer, damit sie nicht als "nicht geheilt" zählen.
    if not stable:
        post = sync_status() if is_gitops else "N/A"
        logging.warning(
            f"Lauf übersprungen: Ausgangszustand nach {stable_wait_s} s instabil (Sync: {post})."
        )
        rec.add(
            **base,
            valid=0,
            stable_before=0,
            recovered="",
            remediation_time_s="",
            heal_attempts_after=heal_before,
            post_window_sync_status=post,
        )
        time.sleep(COOLDOWN_SECONDS)
        return

    rc, _, err = run_cmd(data["inject"])
    if rc != 0:
        logging.error(f"Injektion fehlgeschlagen ({err}): Lauf ungültig.")
        rec.add(
            **base,
            valid=0,
            stable_before=1,
            recovered="",
            remediation_time_s="",
            heal_attempts_after=heal_before,
            post_window_sync_status="",
        )
        time.sleep(COOLDOWN_SECONDS)
        return

    t_start = time.perf_counter()
    if not is_gitops:
        rc_apply, _, err_apply = run_cmd(
            f"kubectl --kubeconfig={Q_KUBECONFIG} apply -f {Q_MANIFEST}"
        )
        if rc_apply != 0:
            logging.error(
                f"Push 'kubectl apply' fehlgeschlagen (rc={rc_apply}): {err_apply}."
            )
            rec.add(
                **base,
                valid=0,
                stable_before=1,
                recovered="",
                remediation_time_s="",
                heal_attempts_after="N/A",
                post_window_sync_status="N/A",
            )
            time.sleep(COOLDOWN_SECONDS)
            return

    recovered, t_fix = False, ""
    while (time.perf_counter() - t_start) < WINDOW_SECONDS:
        if data["verify"]():
            t_fix = round(time.perf_counter() - t_start, 3)
            recovered = True
            break
        time.sleep(POLL_INTERVAL_S)

    post = sync_status() if is_gitops else "N/A"
    heal_after = get_heal_attempts() if is_gitops else "N/A"
    logging.info(
        f"  -> geheilt: {recovered} | Dauer: {t_fix} s | Sync nach Fenster: {post} "
        f"| Heal-Zähler: {heal_before} -> {heal_after}"
    )

    rec.add(
        **base,
        valid=1,
        stable_before=1,
        recovered=int(recovered),
        remediation_time_s=t_fix,
        heal_attempts_after=heal_after,
        post_window_sync_status=post,
    )
    time.sleep(COOLDOWN_SECONDS)


# ==============================================================================
# MODELLE
# ==============================================================================
def run_gitops(runs, rec):
    logging.info("=== UF1-B: GITOPS (ArgoCD, selfHeal/prune einmalig aktiviert) ===")
    rc, _, err = run_cmd(f"kubectl apply -f {Q_APP_YAML}")
    if rc != 0:
        logging.critical(f"FATAL: Application konnte nicht angelegt werden: {err}")
        sys.exit(1)

    patch = '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
    rc, _, err = run_cmd(
        f"kubectl patch application {APP} -n argocd --type merge -p '{patch}'"
    )
    if rc != 0:
        logging.critical(f"FATAL: syncPolicy konnte nicht gesetzt werden: {err}")
        sys.exit(1)

    log_argocd_settings()
    ok, waited = ensure_stable(True)
    if not ok:
        logging.warning(
            f"Ausgangszustand vor der Messreihe nach {waited} s nicht stabil; "
            "einzelne Läufe werden ggf. übersprungen."
        )
    time.sleep(COOLDOWN_SECONDS)

    for pid, data in SCENARIOS.items():
        for run in range(1, runs + 1):
            measure_one("GitOps_ArgoCD_Natural", pid, data, run, True, rec)


def run_push(runs, rec):
    logging.info("=== UF1-B: PUSH-REFERENZ (Admin-Kubeconfig, ein Apply je Lauf) ===")
    rc, _, err = run_cmd(
        f"kubectl patch application {APP} -n argocd --type merge "
        '-p \'{"metadata":{"finalizers":null}}\''
    )
    if rc != 0:
        logging.warning(f"Entfernen der Finalizer meldete: {err}")

    rc, _, err = run_cmd(f"kubectl delete -f {Q_APP_YAML} --ignore-not-found")
    if rc != 0:
        logging.warning(f"Löschen der Application meldete: {err}")

    rc, _, _ = run_cmd(f"kubectl get application {APP} -n argocd", log_cmd=False)
    if rc == 0:
        logging.critical(
            f"FATAL: Application '{APP}' existiert noch. Push-Messung darf nicht mit "
            "aktivem ArgoCD laufen. Abbruch."
        )
        sys.exit(1)
    logging.info(f"Verifiziert: Application '{APP}' existiert nicht mehr.")

    rc, _, err = run_cmd(f"kind get kubeconfig --name bachelor-demo > {Q_KUBECONFIG}")
    if rc != 0 or not ADMIN_KUBECONFIG.is_file():
        logging.critical(
            f"FATAL: {ADMIN_KUBECONFIG} konnte nicht erzeugt werden: {err}"
        )
        sys.exit(1)

    ok, _ = ensure_stable(False)
    if not ok:
        logging.warning("Push-Ausgangszustand vor der Messreihe nicht stabil.")
    time.sleep(COOLDOWN_SECONDS)

    for pid, data in SCENARIOS.items():
        for run in range(1, runs + 1):
            measure_one("Push_Pipeline", pid, data, run, False, rec)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Läufe je Muster (1-3 = Pilot, 10 = Vollmessung)",
    )
    ap.add_argument("--only", choices=["gitops", "push"], help="nur ein Modell messen")
    ap.add_argument(
        "--reset-counter",
        action="store_true",
        help="zwischen GitOps-Läufen manuellen Sync zum Zurücksetzen des Heal-Zählers auslösen",
    )
    args = ap.parse_args()
    CFG["reset_counter"] = args.reset_counter

    rec = Recorder(CSV_FILE)
    try:
        if args.only != "push":
            run_gitops(args.runs, rec)
        if args.only != "gitops":
            run_push(args.runs, rec)
    finally:
        rec.close()
    logging.info(f"FERTIG. Ergebnisse in '{CSV_FILE}', Log in '{LOG_FILE}'.")
