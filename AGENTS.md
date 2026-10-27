# AGENTS.md

This repo is an ArgoCD GitOps repository (repo: https://github.com/whrhythm/ops.git, branch `main`). It holds no application code — only Kubernetes manifests and ArgoCD `Application` objects. There is no build/test/lint system.

## Layout

- `apps/<app>/` — manifests for each application (one folder per app).
- `argocd/apps/` — one ArgoCD `Application` object per app. This directory is managed by the app-of-apps parent app.
- `argocd/bootstrap/apps.yaml` — the parent Application that syncs `argocd/apps/`. It must be applied manually once (`kubectl apply -f argocd/bootstrap/apps.yaml`); after that it manages every app in `argocd/apps/`.

## Deploy ordering (sync waves)

Waves are set via the `argocd.argoproj.io/sync-wave` annotation on each `Application`:

- `0` — the AI foundation: `kubepilot`
- `>= 1` — business apps (deployed after kubepilot)
- Negative waves are reserved for shared dependencies, if any app ever needs them.

## KubePilot

- Upstream: https://github.com/bwalia/kubepilot (Go module `github.com/kubepilot/kubepilot`), pinned at commit `930f0b5f507a6896e59f63294fcdf67a1ecf075c`.
- Chart: `apps/kubepilot/chart/` is a copy of the official Helm chart from upstream `charts/kubepilot/` (chart v0.4.0). To upgrade, re-copy from upstream at a new commit and update the SHA in this file — **and re-apply the local patch below** (or drop it once upstream fixes it).
- Local patch (upstream bug, chart v0.4.0): the Service selector (`kubepilot.selectorLabels`) only matches `name`+`instance`, so it also selects the `nodeAgent` DaemonSet pods (they carry the same labels). Real traffic is unaffected (the node-agent pods lack the named target ports and never become ready endpoints), but `kubectl port-forward svc/kubepilot` picks a node-agent pod and fails. Fix: `app.kubernetes.io/component: server` added to the Deployment's `spec.selector.matchLabels`, the pod template labels, and the Service selector (marked with `# local patch:` comments in `templates/deployment.yaml` and `templates/service.yaml`).
- Config: `apps/kubepilot/values.yaml` is the only file changed in day-to-day operations. The Application references it via `helm.valueFiles: ["/apps/kubepilot/values.yaml"]` — the leading slash means relative to the repo root (ArgoCD resolves `/`-prefixed value file paths against the repo root).
- The app is stateless: no PostgreSQL/Redis. LLM calls go to an OpenAI-compatible endpoint (`kubepilot.ollamaBaseURL` — works with Ollama or a litellm proxy; model via `kubepilot.ollamaModel`).
- Secrets (this repo is public — never commit real secrets):
  - LLM API key: out-of-band Secret `kubepilot-llm` in namespace `kubepilot` with key `api-key`, created once via `kubectl create secret generic kubepilot-llm -n kubepilot --from-literal=api-key=<key>`; referenced by `kubepilot.ollamaAPIKeySecret` (the chart injects it as `KUBEPILOT_OLLAMA_API_KEY`, never inline).
  - Dashboard auth: enabled by default. Placeholder `kubepilot.auth.password` in values — replace at deploy time (or set `kubepilot.auth.existingSecret`).
- Image: `ghcr.io/kubepilot/kubepilot:latest`, built from the upstream Dockerfile (for CN networks add tuna alpine mirror + `GOPROXY=https://goproxy.cn,direct`), pushed to the ghcr package. The container runs as non-root (uid 65532) with a read-only rootfs — custom Dockerfiles must keep that working.
- Access: dashboard on Service port 8080 (`kubectl port-forward -n kubepilot svc/kubepilot 8080:8080`), MCP on 9090. The app also runs a per-node DaemonSet (`nodeAgent`, default on) exposing hardware info; disable with `nodeAgent.enabled: false`.

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
kubectl apply --dry-run=client -f argocd/apps/ -f argocd/bootstrap/
```
