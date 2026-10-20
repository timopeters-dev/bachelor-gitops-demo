import subprocess
import sys


def run_cmd(cmd):
    """Führt einen Terminal-Befehl aus und gibt das Ergebnis zurück."""
    print(f"Führe aus: {cmd}")
    result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if result.returncode != 0:
        print(f"Fehler: {result.stderr}")
    else:
        print(result.stdout.strip())


# Katalog der 10 definierten Drift-Muster (D1 bis D10)
DRIFT_PATTERNS = {
    "D1": {
        "name": "Horizontale Skalierung (Replica-Drift: 2 -> 5)",
        "cmd": "kubectl scale deployment frontend --replicas=5",
    },
    "D2": {
        "name": "Unautorisiertes Image-Tag (Version-Drift am Backend)",
        "cmd": "kubectl set image deployment/backend-api api=hashicorp/http-echo:0.2.3",
    },
    "D3": {
        "name": "Manipulation von Start-Parametern (Config-Drift)",
        "cmd": 'kubectl patch deployment backend-api --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/template/spec/containers/0/args/0\\", \\"value\\": \\"-text=MANIPULIERT\\"}]"',
    },
    "D4": {
        "name": "Netzwerk-Port-Verstellung (Service-Drift: Port 80 -> 8080)",
        "cmd": 'kubectl patch svc frontend-service --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/ports/0/targetPort\\", \\"value\\": 8080}]"',
    },
    "D5": {
        "name": "Löschung einer Komponente (Service komplett entfernen)",
        "cmd": "kubectl delete svc frontend-service",
    },
    "D6": {
        "name": "Umgebungsvariable eingeschleust (Env-Injection an DB)",
        "cmd": "kubectl set env deployment/database UNBEFUGTER_ZUGRIFF=true",
    },
    "D7": {
        "name": "Ressourcen-Limit verändert (Memory-Limit von 128Mi auf 512Mi)",
        "cmd": "kubectl set resources deployment/frontend -c=web --limits=memory=512Mi",
    },
    "D8": {
        "name": "Schatten-Ressource eingeschleust (Unautorisierte ConfigMap mit App-Label)",
        "cmd": "kubectl create configmap rogue-config --from-literal=geheimnis=123 && kubectl label configmap rogue-config argocd.argoproj.io/instance=bachelor-3tier-app app.kubernetes.io/instance=bachelor-3tier-app --overwrite",
    },
    "D9": {
        "name": "Sicherheits-Eskalation (allowPrivilegeEscalation auf true setzen)",
        "cmd": 'kubectl patch deployment frontend --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/template/spec/containers/0/securityContext/allowPrivilegeEscalation\\", \\"value\\": true}]"',
    },
    "D10": {
        "name": "Service-Selector-Drift (Routing-Hijacking: tier=frontend -> tier=backend)",
        "cmd": 'kubectl patch svc frontend-service --type=json -p="[{\\"op\\": \\"replace\\", \\"path\\": \\"/spec/selector/tier\\", \\"value\\": \\"backend\\"}]"',
    },
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].upper() not in DRIFT_PATTERNS:
        print("Verfügbare Drift-Muster für das Experiment:")
        for key, val in DRIFT_PATTERNS.items():
            print(f"  {key}: {val['name']}")
        print("\nAufruf-Beispiel: python benchmark-suite/drift_catalog.py D1")
        sys.exit(1)

    pattern_id = sys.argv[1].upper()
    pattern = DRIFT_PATTERNS[pattern_id]
    print(f"--- Starte Drift-Muster {pattern_id}: {pattern['name']} ---")
    run_cmd(pattern["cmd"])
