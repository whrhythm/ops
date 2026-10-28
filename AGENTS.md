# AGENTS.md

This repo is an ArgoCD GitOps repository (repo: https://github.com/whrhythm/ops.git, branch `main`). It holds no application code — only Kubernetes manifests and ArgoCD `Application` objects. There is no build/test/lint system.

## Layout

- `apps/<app>/` — manifests for each application (one folder per app).
- `argocd/apps/` — one ArgoCD `Application` object per app. This directory is managed by the app-of-apps parent app.
- `argocd/bootstrap/apps.yaml` — the parent Application that syncs `argocd/apps/`. It must be applied manually once (`kubectl apply -f argocd/bootstrap/apps.yaml`); after that it manages every app in `argocd/apps/`.

## Deploy ordering (sync waves)

Waves are set via the `argocd.argoproj.io/sync-wave` annotation on each `Application`:

- `-4` — `longhorn` (block storage, first so PVCs can use it later)
- `-3` — `metallb`, `cert-manager`, `capsule` (independent infra charts)
- `-2` — `metallb-config` (IPAddressPool + L2Advertisement), `cert-manager-issuer` (self-signed ClusterIssuer), `capsule-tenant` (default Tenant) — each waits on the CRDs of the chart above
- `-1` — `signoz` (UI Service takes a MetalLB IP), `apisix` (dataplane + built-in etcd)
- `0` — `apisix-kic` (needs the APISIX Admin API), `kubepilot`
- `>= 1` — business apps: `aisix` (wave 1)

## Infrastructure components

Single-node k3s cluster (`edge`); all values for one node are set inline in each Application (`helm.values`) — chart-repo values files can only live in the chart's own repo, so only vendored charts (kubepilot) use `valueFiles`.

- **longhorn** — `https://charts.longhorn.io` chart `longhorn` `1.13.0`, ns `longhorn-system`. Single-node values: default StorageClass `longhorn` `replicaCount: 1` (`persistence.defaultClassReplicaCount`), CSI controllers/global-manager/UI all 1 replica, `preUpgradeChecker` disabled (it blocks GitOps syncs). No data redundancy on one node.
- **metallb** — git source `github.com/metallb/metallb` tag `v0.16.1` path `charts/metallb`, ns `metallb`. Native API only: since v0.13 there is no ConfigMap/`configInline`; `apps/metallb/pool.yaml` creates the `IPAddressPool` (192.168.10.50-65) **and an explicit `L2Advertisement`** — without it, pools are not L2-announced.
- **cert-manager** — `https://charts.jetstack.io` chart `cert-manager` `v1.21.3`, ns `cert-manager`. Set `crds.enabled: true` + `crds.keep: true` (the chart ships CRDs in templates, default off). `apps/cert-manager/issuer.yaml` adds a self-signed `ClusterIssuer` named `selfsigned` (via the `-2` app `cert-manager-issuer`).
- **capsule** — `oci://ghcr.io/projectcapsule/charts` chart `capsule` `0.15.2`, ns `capsule-system`. OCI registry, **not** the git repo: the repo's `Chart.yaml` carries `version: 0.0.0` (overwritten at release), which ArgoCD/Helm reject. v0.15 tenants are `Tenant` objects (`capsule.clastix.io/v1beta2`, cluster-scoped), replacing the old `Capsule` kind — `apps/capsule/tenant.yaml` creates the `default` tenant owned by User `default`.
- **signoz** — `https://charts.signoz.io` chart `signoz` `0.147.0`, ns `signoz`. `signoz.service.type: LoadBalancer` (one MetalLB IP, UI on port 8080). Bundled subcharts (clickhouse+zk, postgres, otel-gateway) run single-replica by default.
- **apisix** — git source `github.com/apache/apisix-helm-chart` tag `apisix-ingress-controller-1.4.1`, path `charts/apisix`, ns `apisix`. Image `apache/apisix:3.19.0-ubuntu`, built-in dev etcd (`etcd.replicaCount: 1`), Admin API on svc `apisix-admin:9180` (default admin key, dev only). Service stays NodePort (no LB IP needed).
- **apisix-kic** — same repo/tag, path `charts/apisix-ingress-controller` (KIC image 2.2.0, chart v1.4.1), ns `apisix`. `gatewayProxy.createDefault: true` + `apisix.adminService` point the default `GatewayProxy` at `apisix-admin:9180` (admin key default matches the dataplane). Webhook uses a chart-generated self-signed cert (no cert-manager dependency).

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

## AISIX (api7 AI gateway)

- `apps/aisix/` — hand-written manifests (no Helm chart exists in the OSS repo). Image `ghcr.io/api7/aisix:1.6.0` (runs as uid 10001, non-root).
- Standalone mode: `config.yaml` sets `resources_file: /etc/aisix/resources.yaml` (no etcd). Resources file format: `_format_version: "1"` + resource collections (`provider_keys`, `models`, `api_keys`, ...); values support `${ENV}` interpolation. Currently **no upstreams configured** — add `provider_keys`/`models` (keys via env, never inline in the public repo) and restart/SIGHUP.
- Listeners: proxy `:3000`, admin `:3001` (`/livez` + `/readyz` live here — probes point at it; admin key via `${AISIX_ADMIN_KEY}`), prometheus `:9090`.
- Secret: out-of-band `aisix-admin` in ns `aisix`, key `admin-key` (`kubectl create secret generic aisix-admin -n aisix --from-literal=admin-key=<key>`); injected as env `AISIX_ADMIN_KEY`.

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
