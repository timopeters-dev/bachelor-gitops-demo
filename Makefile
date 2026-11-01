.PHONY: help init venv up down patch-argocd argocd-ui check-env reset-state test-drift test-mttr test-dr stats benchmark-all clean
.NOTPARALLEL:

# ==============================================================================
# KONFIGURATION & PYTHON VENV
# ==============================================================================
VENV ?= .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
SENTINEL := $(VENV)/.installed

# Standardmäßig Vollmessung (10 Läufe); überschreibbar mit z. B. 'make benchmark-all RUNS=2'
RUNS ?= 10
ARGS := $(if $(RUNS),--runs $(RUNS),)
RESULTS_DIR ?= benchmark-suite/results

help: ## Zeigt diese Übersicht aller verfügbaren Befehle an
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(SENTINEL): requirements.txt
	@echo "==> Initialisiere Python Virtual Environment in '$(VENV)'..."
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install --upgrade pip
	@$(PIP) install -r requirements.txt
	@touch $(SENTINEL)

venv: $(SENTINEL) ## Erstellt das venv und installiert alle Python-Pakete aus requirements.txt

init: venv ## Initialisiert Terraform und die Python Virtual Environment (venv)
	terraform -chdir=infrastructure init

up: ## Zieht die komplette 2-Ebenen-Architektur (Kind + ArgoCD + 3-Tier-App) hoch
	terraform -chdir=infrastructure apply -auto-approve
	$(MAKE) patch-argocd
	kubectl apply -f workloads/argocd-app.yaml

down: ## Zerstört den gesamten Kubernetes-Cluster restlos (Totalausfall-Simulation)
	terraform -chdir=infrastructure destroy -auto-approve
	@kind delete cluster --name bachelor-demo 2>/dev/null || true

patch-argocd: ## Deaktiviert exponentiellen Backoff in Argo CD (Workaround für Upstream-Bug #21599)
	kubectl patch cm argocd-cmd-params-cm -n argocd --type merge -p '{"data":{"controller.self.heal.backoff.timeout.seconds":"0"}}'
	kubectl rollout restart statefulset/argocd-application-controller -n argocd
	kubectl rollout status statefulset/argocd-application-controller -n argocd --timeout=60s

argocd-ui: ## Gibt das ArgoCD-Admin-Passwort aus und startet das Port-Forwarding auf https://localhost:8080
	@echo -n "ArgoCD Admin-Passwort: "
	@kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo ""
	kubectl port-forward svc/argocd-server -n argocd 8080:443

check-env: ## Prüft Cluster, Argo-Backoff und Git-Zugang vor einer Messung
	@kubectl get nodes >/dev/null || (echo "Cluster nicht erreichbar"; exit 1)
	@test "$$(kubectl get cm argocd-cmd-params-cm -n argocd -o jsonpath='{.data.controller\.self\.heal\.backoff\.timeout\.seconds}')" = "0" || (echo "Backoff aktiv: erst 'make patch-argocd'"; exit 1)
	@git diff --cached --quiet || (echo "Gestagte Änderungen im Repo"; exit 1)
	@git push --dry-run >/dev/null 2>&1 || (echo "git push nicht möglich"; exit 1)

reset-state: ## Räumt Reste aus UF1 auf (Rogue-ConfigMap, D11-Variable)
	kubectl delete configmap rogue-config --ignore-not-found
	-kubectl set env deployment/database UNBEFUGTER_ZUGRIFF-
	kubectl apply -f workloads/base/manifest.yaml

test-drift: $(SENTINEL) check-env ## Startet Experiment 1 (UF1: Configuration Drift & Self-Healing D1-D11)
	$(PYTHON) benchmark-suite/run_natural_selfheal.py $(ARGS)

test-mttr: $(SENTINEL) check-env ## Startet Experiment 2 (UF2: Release-Rollback & MTTR)
	$(PYTHON) benchmark-suite/run_rollback_mttr.py $(ARGS)

test-dr: $(SENTINEL) ## Startet Experiment 3 (UF3: Disaster Recovery & Cluster-Re-Initialisierung)
	$(PYTHON) benchmark-suite/run_disaster_recovery.py $(ARGS)

stats: $(SENTINEL) ## Berechnet Mann-Whitney-U, Holm-Korrektur, Cliff's Delta und generiert Grafiken und Tabellen
	$(PYTHON) benchmark-suite/evaluate_stats.py --results-dir $(RESULTS_DIR)

benchmark-all: test-drift reset-state test-mttr test-dr stats ## Führt alle 3 Experimente und die statistische Auswertung komplett durch

clean: reset-state ## Entfernt temporäre Test-Ressourcen und lokale Kubeconfig-Dateien
	rm -f admin-kubeconfig.yaml benchmark-suite/admin-kubeconfig.yaml
