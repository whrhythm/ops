#!/usr/bin/env bash
# One-time (idempotent) prerequisites for Authentik, run from a machine with
# cluster-admin kubectl access and local python3 + boto3:
#
#   1. namespace `authentik` (this repo does not create namespaces)
#   2. Secret `authentik-config` in it — every AUTHENTIK_* env the chart would
#      otherwise randomize per render (AUTHENTIK_SECRET_KEY) or read from
#      other out-of-band values (CNPG password, MinIO keys). Keys are env var
#      names because the chart consumes the secret via envFrom.
#   3. role `authentik` + database `authentik` in the shared CloudNativePG
#      cluster (same pattern as harbor-prereqs.sh)
#   4. MinIO bucket `authentik` (file storage backend) via a temporary
#      port-forward + local boto3
#
# Safe to re-run: existing secret/role/database/bucket are left untouched.
set -euo pipefail

NS=authentik
PG_CLUSTER=postgres
PG_NS=postgres
PG_INSTANCE=${PG_CLUSTER}-1
MINIO_NS=minio
MINIO_BUCKET=authentik

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found (needed for the MinIO bucket)" >&2; exit 1; }
python3 -c "import boto3" 2>/dev/null || { echo "boto3 not importable (pip install boto3)" >&2; exit 1; }

echo "==> namespace $NS"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

echo "==> secret $NS/authentik-config"
if kubectl -n "$NS" get secret authentik-config >/dev/null 2>&1; then
  echo "    already exists, leaving it alone"
else
  minio_user="$(kubectl -n "$MINIO_NS" get secret minio-root -o jsonpath='{.data.root-user}' | base64 -d)"
  minio_pass="$(kubectl -n "$MINIO_NS" get secret minio-root -o jsonpath='{.data.root-password}' | base64 -d)"
  kubectl -n "$NS" create secret generic authentik-config \
    --from-literal=AUTHENTIK_SECRET_KEY="$(openssl rand -base64 60 | tr -d '\n')" \
    --from-literal=AUTHENTIK_POSTGRESQL__HOST=postgres-rw.postgres.svc.cluster.local \
    --from-literal=AUTHENTIK_POSTGRESQL__PORT=5432 \
    --from-literal=AUTHENTIK_POSTGRESQL__NAME=authentik \
    --from-literal=AUTHENTIK_POSTGRESQL__USER=authentik \
    --from-literal=AUTHENTIK_POSTGRESQL__PASSWORD="$(openssl rand -hex 16)" \
    --from-literal=AUTHENTIK_POSTGRESQL__SSLMODE=require \
    --from-literal=AUTHENTIK_STORAGE__BACKEND=s3 \
    --from-literal=AUTHENTIK_STORAGE__S3__ENDPOINT=http://minio.minio.svc:9000 \
    --from-literal=AUTHENTIK_STORAGE__S3__USE_SSL=false \
    --from-literal=AUTHENTIK_STORAGE__S3__ADDRESSING_STYLE=path \
    --from-literal=AUTHENTIK_STORAGE__S3__BUCKET_NAME="$MINIO_BUCKET" \
    --from-literal=AUTHENTIK_STORAGE__S3__ACCESS_KEY="$minio_user" \
    --from-literal=AUTHENTIK_STORAGE__S3__SECRET_KEY="$minio_pass"
  echo "    created"
fi

echo "==> database role/database in $PG_NS/$PG_CLUSTER"
ready="$(kubectl -n "$PG_NS" get cluster "$PG_CLUSTER" -o jsonpath='{.status.readyInstances}' 2>/dev/null || true)"
if [ "${ready:-0}" != "1" ]; then
  echo "    CloudNativePG cluster $PG_CLUSTER is not ready (readyInstances=${ready:-missing}); is the postgres app synced?" >&2
  exit 1
fi
dbpass="$(kubectl -n "$NS" get secret authentik-config -o jsonpath='{.data.AUTHENTIK_POSTGRESQL__PASSWORD}' | base64 -d)"
kubectl -n "$PG_NS" exec -i -c postgres "$PG_INSTANCE" -- psql -U postgres -v ON_ERROR_STOP=1 -v pw="$dbpass" <<'SQL'
SELECT format('CREATE ROLE authentik LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authentik')\gexec
SELECT 'CREATE DATABASE authentik OWNER authentik'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'authentik')\gexec
SQL
echo "    done"

echo "==> minio bucket $MINIO_NS/$MINIO_BUCKET"
minio_user="$(kubectl -n "$MINIO_NS" get secret minio-root -o jsonpath='{.data.root-user}' | base64 -d)"
minio_pass="$(kubectl -n "$MINIO_NS" get secret minio-root -o jsonpath='{.data.root-password}' | base64 -d)"
# minio is a ClusterIP service; reach it through a temporary port-forward.
# (kubectl port-forward takes `localPort:remotePort`; a `host:local:remote`
# triple is mis-parsed and the forward never starts.)
kubectl -n "$MINIO_NS" port-forward svc/minio 19000:9000 >/dev/null 2>&1 &
pf_pid=$!
trap 'kill "$pf_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  # -f: a 5xx (Bad Gateway while the pod is not serving yet) must NOT count as ready
  if curl -sf --max-time 3 -o /dev/null http://127.0.0.1:19000/minio/health/live; then break; fi
  sleep 0.5
done
python3 - "$MINIO_BUCKET" "$minio_user" "$minio_pass" <<'PY'
import sys, boto3
from botocore.client import Config
from botocore.exceptions import ClientError

bucket, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
s3 = boto3.client(
    "s3",
    endpoint_url="http://127.0.0.1:19000",
    aws_access_key_id=user,
    aws_secret_access_key=password,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    region_name="us-east-1",
)
try:
    s3.head_bucket(Bucket=bucket)
    print(f"    bucket {bucket} already exists")
except ClientError:
    s3.create_bucket(Bucket=bucket)
    print(f"    bucket {bucket} created")
PY

echo
echo "Next: commit/sync the authentik-pki + authentik apps, then open"
echo "https://authentik.k3s.local (hosts entry 192.168.10.53 + internal CA) and"
echo "set the akadmin password in the first-visit setup flow."
