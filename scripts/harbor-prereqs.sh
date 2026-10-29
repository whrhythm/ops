#!/usr/bin/env bash
# One-time (idempotent) prerequisites for Harbor, run from a machine with
# cluster-admin kubectl access:
#
#   1. namespace `harbor` (this repo does not create namespaces)
#   2. Secret `harbor-secrets` in it — every value the chart would otherwise
#      randomize per render (ArgoCD cannot run helm `lookup` offline)
#   3. database `registry` + role `harbor` in the shared CloudNativePG cluster
#
# Safe to re-run: existing secret/role/database are left untouched.
set -euo pipefail

NS=harbor
PG_CLUSTER=postgres
PG_NS=postgres
PG_INSTANCE=${PG_CLUSTER}-1

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v htpasswd >/dev/null || { echo "htpasswd (apache2-utils) not found" >&2; exit 1; }

echo "==> namespace $NS"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

echo "==> secret $NS/harbor-secrets"
if kubectl -n "$NS" get secret harbor-secrets >/dev/null 2>&1; then
  echo "    already exists, leaving it alone"
else
  regpass="$(openssl rand -hex 8)"
  # Harbor's registry reads an htpasswd file; Go's bcrypt accepts $2a/$2b/$2y,
  # normalise to $2a like Harbor's own docs example.
  htpasswd_line="$(htpasswd -nbB harbor_registry_user "$regpass" | sed 's/^\([^:]*\):\$2y\$/\1:$2a$/')"
  kubectl -n "$NS" create secret generic harbor-secrets \
    --from-literal=secretKey="$(openssl rand -hex 24)" \
    --from-literal=secret="$(openssl rand -hex 16)" \
    --from-literal=CSRF_KEY="$(openssl rand -hex 16)" \
    --from-literal=JOBSERVICE_SECRET="$(openssl rand -hex 16)" \
    --from-literal=REGISTRY_HTTP_SECRET="$(openssl rand -hex 16)" \
    --from-literal=REGISTRY_PASSWD="$regpass" \
    --from-literal=REGISTRY_HTPASSWD="$htpasswd_line" \
    --from-literal=password="$(openssl rand -hex 12)" \
    --from-literal=HARBOR_ADMIN_PASSWORD="$(openssl rand -hex 12)"
  echo "    created (admin password: kubectl -n $NS get secret harbor-secrets -o jsonpath='{.data.HARBOR_ADMIN_PASSWORD}' | base64 -d)"
fi

echo "==> database role/database in $PG_NS/$PG_CLUSTER"
ready="$(kubectl -n "$PG_NS" get cluster "$PG_CLUSTER" -o jsonpath='{.status.readyInstances}' 2>/dev/null || true)"
if [ "${ready:-0}" != "1" ]; then
  echo "    CloudNativePG cluster $PG_CLUSTER is not ready (readyInstances=${ready:-missing}); is the postgres app synced?" >&2
  exit 1
fi
dbpass="$(kubectl -n "$NS" get secret harbor-secrets -o jsonpath='{.data.password}' | base64 -d)"
kubectl -n "$PG_NS" exec -i -c postgres "$PG_INSTANCE" -- psql -U postgres -v ON_ERROR_STOP=1 -v pw="$dbpass" <<'SQL'
SELECT format('CREATE ROLE harbor LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'harbor')\gexec
SELECT 'CREATE DATABASE registry OWNER harbor'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'registry')\gexec
SQL
echo "    done"

echo
echo "Next: commit/sync the harbor-pki + harbor apps, then create the project and"
echo "pull robot in the Harbor UI, and run scripts/harbor-node-registry.sh on every node."
