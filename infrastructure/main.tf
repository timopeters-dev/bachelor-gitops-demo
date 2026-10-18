terraform {
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.6.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12.0"
    }
  }
}

# 1. Lokalen Kubernetes-Cluster in Docker erstellen
provider "kind" {}

resource "kind_cluster" "bachelor_cluster" {
  name           = "bachelor-demo"
  wait_for_ready = true
  # Speichert den Cluster-Zugang automatisch für 'kubectl'
  kubeconfig_path = pathexpand("~/.kube/config")
}

# 2. Verbindung für den Paketmanager Helm zum neuen Cluster herstellen
provider "helm" {
  kubernetes {
    host                   = kind_cluster.bachelor_cluster.endpoint
    client_certificate     = kind_cluster.bachelor_cluster.client_certificate
    client_key             = kind_cluster.bachelor_cluster.client_key
    cluster_ca_certificate = kind_cluster.bachelor_cluster.cluster_ca_certificate
  }
}

# 3. ArgoCD als In-Cluster-Operator installieren
resource "helm_release" "argocd" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  namespace        = "argocd"
  create_namespace = true
  version          = "7.7.12"

  depends_on = [kind_cluster.bachelor_cluster]
}
