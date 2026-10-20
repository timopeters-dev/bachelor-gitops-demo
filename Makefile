.PHONY: help init venv up down argocd-ui test-drift test-mttr test-dr stats benchmark-all clean

# ==============================================================================
# KONFIGURATION & PYTHON VENV
# ==============================================================================
VENV ?= .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
SENTINEL := $(VENV)/.installed

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
	kubectl apply -f workloads/argocd-app.yaml

down: ## Zerstört den gesamten Kubernetes-Cluster restlos (Totalausfall-Simulation)
	terraform -chdir=infrastructure destroy -auto-approve
	@kind delete cluster --name bachelor-demo 2>/dev/null || true

argocd-ui: ## Gibt das ArgoCD-Admin-Passwort aus und startet das Port-Forwarding auf https://localhost:8080
	@echo -n "ArgoCD Admin-Passwort: "
	@kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo ""
	kubectl port-forward svc/argocd-server -n argocd 8080:443

test-drift: $(SENTINEL) ## Startet Experiment 1 (UF1: Configuration Drift & Self-Healing D1-D10)
	$(PYTHON) benchmark-suite/run_experiment.py

test-mttr: $(SENTINEL) ## Startet Experiment 2 (UF2: Release-Rollback & MTTR)
	$(PYTHON) benchmark-suite/run_rollback_mttr.py

test-dr: $(SENTINEL) ## Startet Experiment 3 (UF3: Disaster Recovery & Cluster-Re-Initialisierung)
	$(PYTHON) benchmark-suite/run_disaster_recovery.py

stats: $(SENTINEL) ## Berechnet Mann-Whitney-U-Tests, Cliff's Delta und generiert alle 6 Grafik-Tafeln (300 DPI)
	$(PYTHON) benchmark-suite/evaluate_stats.py

benchmark-all: test-drift test-mttr test-dr stats ## Führt alle 3 Experimente und die statistische Auswertung komplett durch

clean: ## Entfernt temporäre Test-Ressourcen und lokale Kubeconfig-Dateien
	kubectl delete configmap rogue-config --ignore-not-found
	rm -f admin-kubeconfig.yaml
