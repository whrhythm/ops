#!/usr/bin/env bash
# One-time (idempotent) prerequisites for the in-cluster LiteLLM metering
# proxy (app `litellm`, apps/litellm/), run from a machine with
# cluster-admin kubectl access:
#
#   1. namespace `litellm` (the repo also ships it with Prune=false)
#   2. Secret `litellm-secrets` in it — LITELLM_MASTER_KEY (random),
#      DATABASE_URL (random password on the shared CloudNativePG cluster)
#      and SILICONROUTER_API_KEY (passed as the only argument; never commit)
#   3. role `litellm` + database `litellm` in the shared CloudNativePG
#      cluster (same pattern as harbor-prereqs.sh / authentik-prereqs.sh),
#      plus the `citext` extension in that database which LiteLLM's prisma
#      schema requires
#   4. Secret `platform-litellm-admin` in ns `platform-system` — the same
#      master key as an env var, for the platform's per-user key
#      provisioning endpoint (phase 2)
#
# Usage: scripts/litellm-prereqs.sh <siliconrouter-api-key>
#
# Safe to re-run: existing secret/role/database are left untouched.
set -euo pipefail

NS=litellm
PLATFORM_NS=platform-system
PG_CLUSTER=postgres
PG_NS=postgres
PG_INSTANCE=${PG_CLUSTER}-1

SILICONROUTER_API_KEY="${1:-}"
[ -n "$SILICONROUTER_API_KEY" ] || { echo "usage: $0 <siliconrouter-api-key>" >&2; exit 1; }

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }

echo "==> namespace $NS"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

echo "==> secret $NS/litellm-secrets"
if kubectl -n "$NS" get secret litellm-secrets >/dev/null 2>&1; then
  echo "    already exists, leaving it alone"
else
  pgpass="$(openssl rand -hex 16)"
  kubectl -n "$NS" create secret generic litellm-secrets \
    --from-literal=LITELLM_MASTER_KEY="$(openssl rand -hex 32)" \
    --from-literal=DATABASE_URL="postgresql://litellm:${pgpass}@postgres-rw.postgres.svc.cluster.local:5432/litellm" \
    --from-literal=SILICONROUTER_API_KEY="$SILICONROUTER_API_KEY"
  echo "    created"
fi

echo "==> database role/database in $PG_NS/$PG_CLUSTER"
ready="$(kubectl -n "$PG_NS" get cluster "$PG_CLUSTER" -o jsonpath='{.status.readyInstances}' 2>/dev/null || true)"
if [ "${ready:-0}" != "1" ]; then
  echo "    CloudNativePG cluster $PG_CLUSTER is not ready (readyInstances=${ready:-missing}); is the postgres app synced?" >&2
  exit 1
fi
dburl="$(kubectl -n "$NS" get secret litellm-secrets -o jsonpath='{.data.DATABASE_URL}' | base64 -d)"
cred="${dburl#postgresql://}"
cred="${cred%%@*}"
pgpass="${cred#*:}"
kubectl -n "$PG_NS" exec -i -c postgres "$PG_INSTANCE" -- psql -U postgres -v ON_ERROR_STOP=1 -v pw="$pgpass" <<'SQL'
SELECT format('CREATE ROLE litellm LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'litellm')\gexec
SELECT 'CREATE DATABASE litellm OWNER litellm'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'litellm')\gexec
SQL
# citext is a superuser-only extension and must exist in the target database.
kubectl -n "$PG_NS" exec -i -c postgres "$PG_INSTANCE" -- psql -U postgres -d litellm -v ON_ERROR_STOP=1 \
  -c 'CREATE EXTENSION IF NOT EXISTS citext;' >/dev/null
echo "    done (role, database, citext)"

echo "==> secret $PLATFORM_NS/platform-litellm-admin"
if kubectl get ns "$PLATFORM_NS" >/dev/null 2>&1; then
  if kubectl -n "$PLATFORM_NS" get secret platform-litellm-admin >/dev/null 2>&1; then
    echo "    already exists, leaving it alone"
  else
    master="$(kubectl -n "$NS" get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)"
    kubectl -n "$PLATFORM_NS" create secret generic platform-litellm-admin \
      --from-literal=PLATFORM_LITELLM_MASTER_KEY="$master"
    echo "    created"
  fi
else
  echo "    namespace $PLATFORM_NS does not exist yet; skip (re-run after platform is deployed)"
fi

echo
echo "Next: sync the `litellm` ArgoCD app (wave 0), then test with"
echo "scripts/litellm-user-key.sh <username> and the verification steps in"
echo "apps/litellm/README.md."
