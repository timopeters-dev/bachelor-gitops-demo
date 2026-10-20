.PHONY: help init up down argocd-ui test-drift test-mttr test-dr stats benchmark-all clean

help: ## Zeigt diese Übersicht aller verfügbaren Befehle an
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

init: ## Initialisiert Terraform und installiert benötigte Python-Pakete
	terraform -chdir=infrastructure init
	pip install pandas scipy matplotlib seaborn

up: ## Zieht die komplette 2-Ebenen-Architektur (Kind + ArgoCD + 3-Tier-App) hoch
	terraform -chdir=infrastructure apply -auto-approve
	kubectl apply -f workloads/argocd-app.yaml

down: ## Zerstört den gesamten Kubernetes-Cluster restlos (Totalausfall-Simulation)
	terraform -chdir=infrastructure destroy -auto-approve

argocd-ui: ## Gibt das ArgoCD-Admin-Passwort aus und startet das Port-Forwarding auf https://localhost:8080
	@echo -n "ArgoCD Admin-Passwort: "
	@kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo ""
	kubectl port-forward svc/argocd-server -n argocd 8080:443

test-drift: ## Startet Experiment 1 (UF1: Configuration Drift & Self-Healing D1-D10)
	python benchmark-suite/run_experiment.py

test-mttr: ## Startet Experiment 2 (UF2: Release-Rollback & MTTR)
	python benchmark-suite/run_rollback_mttr.py

test-dr: ## Startet Experiment 3 (UF3: Disaster Recovery & Cluster-Re-Initialisierung)
	python benchmark-suite/run_disaster_recovery.py

stats: ## Berechnet Mann-Whitney-U-Tests, Cliff's Delta und generiert alle 6 Grafik-Tafeln (300 DPI)
	python benchmark-suite/evaluate_stats.py

benchmark-all: test-drift test-mttr test-dr stats ## Führt alle 3 Experimente und die statistische Auswertung komplett durch

clean: ## Entfernt temporäre Test-Ressourcen und lokale Kubeconfig-Dateien
	kubectl delete configmap rogue-config --ignore-not-found
	rm -f admin-kubeconfig.yaml
