#!/usr/bin/env python3
"""Create the in-cluster Langfuse credentials and its scoped MinIO bucket."""

import base64
import json
import secrets
import shlex
import subprocess


def kubectl(*args, payload=None):
    return subprocess.run(
        ["kubectl", *args], input=payload, text=True, check=True, stdout=subprocess.PIPE
    ).stdout


def apply(resource):
    kubectl("apply", "--server-side", "-f", "-", payload=json.dumps(resource))


def secret(namespace, name, values):
    current = kubectl("-n", namespace, "get", "secret", name, "--ignore-not-found", "-o", "json")
    if current:
        return {key: base64.b64decode(value).decode() for key, value in json.loads(current)["data"].items()}
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
        }
    )
    return values


def main():
    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "langfuse"}})
    secret("langfuse", "langfuse-runtime", {key: secrets.token_hex(32) for key in
           ("salt", "encryption", "nextauth", "clickhouse")})
    secret("langfuse", "langfuse-postgresql-auth", {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "POSTGRES_DB": "langfuse",
        "USERDB_USER": "langfuse",
        "USERDB_PASSWORD": secrets.token_hex(32),
    })
    secret("langfuse", "langfuse-redis-auth", {"default": secrets.token_hex(32)})
    s3 = secret("langfuse", "langfuse-s3-auth", {
        "accessKey": "starbridge-langfuse", "secretKey": secrets.token_hex(24),
    })
    init = secret("langfuse", "langfuse-init", {
        "LANGFUSE_INIT_ORG_ID": "starbridge",
        "LANGFUSE_INIT_ORG_NAME": "Starbridge",
        "LANGFUSE_INIT_PROJECT_ID": "starbridge-assistant",
        "LANGFUSE_INIT_PROJECT_NAME": "Starbridge Assistant",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(18),
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-" + secrets.token_hex(24),
        "LANGFUSE_INIT_USER_EMAIL": "langfuse-admin@starbridge.invalid",
        "LANGFUSE_INIT_USER_NAME": "Starbridge Observability",
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_hex(24),
    })
    user = shlex.quote(s3["accessKey"])
    password = shlex.quote(s3["secretKey"])
    provision = f"""set -eu
config_dir=$(mktemp -d)
export MC_CONFIG_DIR="$config_dir"
trap 'rm -f "$config_dir/policy.json" "$config_dir/config.json"; rmdir "$config_dir" 2>/dev/null || true' EXIT
mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
mc mb --ignore-existing local/starbridge-langfuse >/dev/null 2>&1
cat > "$config_dir/policy.json" <<'EOF'
{{"Version":"2012-10-17","Statement":[
  {{"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket","s3:ListBucketMultipartUploads"],"Resource":["arn:aws:s3:::starbridge-langfuse"]}},
  {{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::starbridge-langfuse/*"]}}
]}}
EOF
mc admin policy create local starbridge-langfuse "$config_dir/policy.json" >/dev/null 2>&1
if ! mc admin user info local {user} >/dev/null 2>&1; then
    mc admin user add local {user} {password} >/dev/null 2>&1
fi
mc admin policy attach local starbridge-langfuse --user {user} >/dev/null 2>&1
"""
    try:
        kubectl("-n", "minio", "exec", "-i", "sts/minio", "--", "sh", "-s", payload=provision)
    except subprocess.CalledProcessError as exc:
        raise SystemExit("Langfuse MinIO bucket provisioning failed") from exc
    secret("librechat", "librechat-langfuse", {
        "LANGFUSE_PUBLIC_KEY": init["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
        "LANGFUSE_SECRET_KEY": init["LANGFUSE_INIT_PROJECT_SECRET_KEY"],
        "LANGFUSE_PROJECT_ID": init["LANGFUSE_INIT_PROJECT_ID"],
    })
    print("Langfuse credentials and isolated bucket are ready")


if __name__ == "__main__":
    main()
