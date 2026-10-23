# AGENTS.md

This repo is an ArgoCD GitOps repository (repo: https://github.com/whrhythm/ops.git, branch `main`). It holds no application code — only Kubernetes manifests and ArgoCD `Application` objects. There is no build/test/lint system.

## Layout

- `apps/<app>/` — manifests for each application (one folder per app).
- `argocd/apps/` — one ArgoCD `Application` object per app. This directory is managed by the app-of-apps parent app.
- `argocd/bootstrap/apps.yaml` — the parent Application that syncs `argocd/apps/`. It must be applied manually once (`kubectl apply -f argocd/bootstrap/apps.yaml`); after that it manages every app in `argocd/apps/`.

## Deploy ordering (sync waves)

Waves are set via the `argocd.argoproj.io/sync-wave` annotation on each `Application`:

- `-1` — shared dependencies (e.g. `kubepilot-deps`: namespace, dev PostgreSQL, dev Redis)
- `0` — the AI foundation: `kubepilot`
- `>= 1` — future business apps (deployed after kubepilot)

## KubePilot

- Chart: `apps/kubepilot/chart/` is a verbatim copy of the official Helm chart from https://github.com/Xnidada/KubePilot (`deploy/helm/kubepilot/`), pinned at upstream commit `63351812984e835fa99d3a85751f0380d4d47482`. Do not edit chart files locally; to upgrade the chart, re-copy from upstream at a new commit and update the SHA in this file.
- Config: `apps/kubepilot/values.yaml` is the only file changed in day-to-day operations. The Application references it via `helm.valueFiles: ["/apps/kubepilot/values.yaml"]` — the leading slash means relative to the repo root (ArgoCD resolves `/`-prefixed value file paths against the repo root).
- The chart does NOT bundle PostgreSQL/Redis. `apps/kubepilot/deps/` provides dev-grade instances; `values.yaml` points `database.host`/`redis.host` at their Service names (`postgres`, `redis`) in namespace `kubepilot`.
- Placeholders that must be replaced before first sync (this repo is public — never commit real secrets):
  - `apps/kubepilot/values.yaml`: `image.repository`/`tag` (placeholder `kubepilot/kubepilot:latest`; build from the upstream Dockerfile and push to your registry), `database.password` and `jwt.secret`
  - `apps/kubepilot/deps/postgres.yaml`: `POSTGRES_PASSWORD` in `postgres-secret` must match `database.password` in `values.yaml`
- After first start, KubePilot requires explicit admin initialization (upstream `scripts/init-admin.go` with `KUBEPILOT_BOOTSTRAP_ADMIN_PASSWORD`); there is no default password.
- `apps/kubepilot/deps/` PVCs need a default StorageClass in the target cluster.

## Adding a new app

1. Manifests (or a vendored chart) under `apps/<app>/`.
2. `argocd/apps/<app>.yaml` — copy an existing Application object; set `source.path`, `destination.namespace`, and the sync-wave annotation.

## Verification

```bash
# render the kubepilot chart with our values (this is what validates the Helm templates)
helm template kubepilot apps/kubepilot/chart -f apps/kubepilot/values.yaml
# parse all plain YAML (chart/templates/ is Go template syntax — skip it, helm template covers it)
python3 -c "import yaml,glob; fs=[f for f in glob.glob('apps/**/*.yaml',recursive=True)+glob.glob('argocd/**/*.yaml',recursive=True) if '/templates/' not in f]; [list(yaml.safe_load_all(open(f))) for f in fs]; print(f'yaml ok ({len(fs)} files)')"
# client-side check (best effort; Application CRs need a cluster context for full validation)
kubectl apply --dry-run=client -f apps/kubepilot/deps/
kubectl apply --dry-run=client -f argocd/apps/ -f argocd/bootstrap/
```
