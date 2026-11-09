# Langfuse

ArgoCD installs chart 2.0.0 (Langfuse 4.4.0) with an internal ClusterIP. ClickHouse,
PostgreSQL and Valkey use separate persistent storage. The existing MinIO service
stores Langfuse objects in the private `starbridge-langfuse` bucket. The bundled
SeaweedFS chart requires Helm features that this cluster's ArgoCD lacks.

Run `python3 scripts/bootstrap-langfuse.py` once with the cluster administrator
context before syncing. It creates the runtime, database, API and sign-in
credentials and attaches a bucket-specific MinIO policy. Keep the bootstrap
Secrets with the database backups. Re-running with the same Secrets reuses the
credentials.

To open the administrative dashboard, forward its internal service over a
trusted connection:

```sh
kubectl -n langfuse port-forward svc/langfuse-web 3000:3000
```

Open `http://localhost:3000` and sign in using the bootstrap user stored in the
`langfuse-init` Secret. The project is named **Starbridge Assistant**. Public
sign-up is disabled. Starbridge users view execution records in the assistant
workbench with their existing session.

The LibreChat network policy permits traffic to Langfuse web on port 3000. The
conversation trace reader checks conversation and message ownership before
loading records. The user interface displays execution metadata and has
input/output details disabled.
