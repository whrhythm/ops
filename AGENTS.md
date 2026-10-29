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
- `-1` — `signoz` (UI Service takes a MetalLB IP), `apisix` (dataplane + built-in etcd), `otel-gateway` + `otel-agent` (standalone OpenTelemetry Collectors)
- `0` — `apisix-kic` (needs the APISIX Admin API), `kubepilot`
- `>= 1` — business apps: `aisix` (wave 1)

## Infrastructure components

Single-node k3s cluster (`edge`); all values for one node are set inline in each Application (`helm.values`) — chart-repo values files can only live in the chart's own repo, so only vendored charts (kubepilot) use `valueFiles`.

- **longhorn** — `https://charts.longhorn.io` chart `longhorn` `1.13.0`, ns `longhorn-system`. Single-node values: default StorageClass `longhorn` `replicaCount: 1` (`persistence.defaultClassReplicaCount`), CSI controllers/global-manager/UI all 1 replica, `preUpgradeChecker` disabled (it blocks GitOps syncs). No data redundancy on one node.
- **metallb** — git source `github.com/metallb/metallb` tag `v0.16.1` path `charts/metallb`, ns `metallb`. Native API only: since v0.13 there is no ConfigMap/`configInline`; `apps/metallb/pool.yaml` creates the `IPAddressPool` (192.168.10.50-65) **and an explicit `L2Advertisement`** — without it, pools are not L2-announced.
- **cert-manager** — `https://charts.jetstack.io` chart `cert-manager` `v1.21.3`, ns `cert-manager`. Set `crds.enabled: true` + `crds.keep: true` (the chart ships CRDs in templates, default off). `apps/cert-manager/issuer.yaml` adds a self-signed `ClusterIssuer` named `selfsigned` (via the `-2` app `cert-manager-issuer`).
- **capsule** — vendored chart `apps/capsule/chart/` = published `oci://ghcr.io/projectcapsule/charts/capsule:0.15.2` package (incl. capsule-proxy subchart), ns `capsule-system`. OCI source unusable: ArgoCD 2.12's repo-server bundles helm 3.15.2 whose `helm pull --repo oci://` is broken (routes OCI repos through the HTTP index path → "object required"). To upgrade, re-extract the new published chart here.
  - **CRDs render inline, not via the crd-lifecycle hook**: the Application sets `helm.skipCrds: true` + `crds.inline: true`. The default hook mode wraps every CRD in a ConfigMap (`tenants` ~427KB, `rulestatuses` ~716KB) that blows the 256KiB `last-applied-configuration` annotation limit of client-side apply; and ArgoCD would additionally apply the chart's raw `crds/*.yaml` (without the `.patch` overlays — conversion webhook + `cert-manager.io/inject-ca-from`) as wave-0 resources, which never matches the patched live CRDs. Inline mode renders the patched CRDs as ordinary wave-0 resources instead.
  - All 12 CRDs get the per-resource annotation `argocd.argoproj.io/sync-options: ServerSideApply=true` (via `crds.annotations` in the Application's `helm.values` — the chart's own values key is a dead typo, `annnotations`) → they are applied with SSA (no 256KiB limit).
  - **Do NOT set app-level `syncPolicy.syncOptions: [ServerSideApply=true]`**: ArgoCD then enables structured-merge-diff for every resource of the app, and its SMD library (built against k8s 1.31 types) rejects the k8s 1.36 `Deployment.status.terminatingReplicas` field → `ComparisonError` ("field not declared in schema") for all resources. Per-resource annotations only (as above) keep SMD limited to the CRDs.
  - **This ArgoCD (2.12.3) Application CRD has no `spec.source.ignoreDifferences` (it lives at spec level) and no `spec.syncPolicy.createNamespace`** — such fields are silently pruned on write, keeping the app-of-apps parent permanently OutOfSync. Not needed for capsule anyway: the classic diff ignores live-only fields (server-defaulted `spec.events`, cert-manager-injected `caBundle`), and SMD does not flag fields owned by other managers.
  - `manager.options.impersonation` is pinned explicitly (`tenantDefaultServiceAccount: default`, `skipTlsVerify: false`, `caSecretKey: ca.crt`) to match the CRD defaults the API server materializes.
  - v0.15 tenants are `Tenant` objects (`capsule.clastix.io/v1beta2`, cluster-scoped), replacing the old `Capsule` kind — `apps/capsule/tenant.yaml` creates the `default` tenant owned by User `default`.
- **signoz** — `https://charts.signoz.io` chart `signoz` `0.147.0`, ns `signoz`. `signoz.service.type: LoadBalancer` (one MetalLB IP, UI on port 8080). Bundled subcharts (clickhouse+zk, postgres) run single-replica by default. The main chart's `otelCollector` component runs the custom `signoz/signoz-otel-collector` image — the storage writer (custom ClickHouse exporters) listening on OTLP 4317/4318 + Jaeger 14250/14268. It is **OAMp-managed by the SigNoz frontend**: it only starts its OTLP receivers once the frontend's OAMp server can create the agent, which needs an org/account to exist (SigNoz onboarding in the UI). Until onboarding is done it runs with only extensions (health_check/pprof/zpages) and refuses 4317. (The separate `signoz-otel-gateway` subchart is off by default and not used here.)
- **otel (standalone OpenTelemetry Collector)** — official chart `https://open-telemetry.github.io/opentelemetry-helm-charts`, `opentelemetry-collector` `0.178.0`, image `otel/opentelemetry-collector-contrib:0.163.2`, ns `otel`. Two Applications, both wave `-1`, forwarding to SigNoz's bundled collector (`signoz-otel-collector.signoz.svc.cluster.local:4317`, `tls.insecure`):
  - `otel-gateway` (`mode: deployment`, 1 replica): `otlp` receiver (4317 grpc / 4318 http) → `batch` → `otlp` exporter; passthrough of **traces/metrics/logs**; ClusterIP Service `otel-gateway` — this is where app SDKs point their OTLP exporters.
  - `otel-agent` (`mode: daemonset`): **metrics** pipeline — `hostmetrics` (chart preset, host `/` mounted at `/hostfs`, `root_path: /hostfs`) → `resourcedetection` → `batch` → `otlp` (direct to the SigNoz collector; `host.name` from `OTEL_RESOURCE_ATTRIBUTES=host.name=$(NODE_NAME)`). No Service.
  - `otel-agent` also collects **all container logs in the cluster** (`/var/log/pods/**/*.log`) → gateway `4317` → SigNoz. Important details:
    - There is **no `k8sfilelog`/`k8slog` receiver** in contrib 0.163.2 (check with `otelcol-contrib components`). Use `file_log` (written as `filelog` in values; the chart renames it) + the `container` operator. `add_metadata_from_filepath` defaults to true (needs `include_file_path: true`) and puts `k8s.pod.name`, `k8s.pod.uid`, `k8s.namespace.name`, `k8s.container.name`, `k8s.container.restart_count` into the **resource** without any API call.
    - Do not use the chart's `presets.logsCollection` — it adds its own `file_log` receiver to the logs pipeline (double collection). The volume is `extraVolumes`/`extraVolumeMounts` hostPath `/var/log/pods` (readOnly).
    - k3s pod log files are `0640 root:root`, so the container needs `securityContext: {runAsUser: 0, runAsGroup: 0, allowPrivilegeEscalation: false}` — otherwise nothing is readable (fails silently-ish, only visible as empty pipelines).
    - `k8sattributes` (for `k8s.deployment.name` + the `app.kubernetes.io/name` label) needs `clusterRole.create: true` with `pods: get,list,watch`; pod association is `resource_attribute: k8s.pod.uid` (already set by the container operator). `extract.metadata` must list `k8s.deployment.name` explicitly; `extract.labels` uses `tag_name`/`key`/`from` and the output attribute name is the `tag_name` (`from: pod`).
    - `service.name` comes from a `transform` processor (3 statements, first wins / last applied wins): pod name → deployment name → `app.kubernetes.io/name` label, each guarded with `where resource.attributes["x"] != nil`. **OTTL has no `in` operator** ("`x` in attributes" is a syntax error — `error_mode: ignore` hides it and the transform silently does nothing; check with `otelcol-contrib validate`).
    - Self-collection loop is prevented by `exclude` globs for this repo's own pods: `/var/log/pods/otel_otel-agent-*/opentelemetry-collector/*.log` and `.../otel_otel-gateway-*/...` (pod dirs are `<ns>_<pod>_<uid>`).
  - `otel-gateway`'s logs pipeline is `otlp` → `memory_limiter` → `filter/noise` → `batch` → SigNoz. `filter/noise` drops probe access lines (`GET|HEAD|POST /livez|readyz|healthz`) with `error_mode: ignore`, because several apps log every kubelet probe. Both apps set explicit `batch` timeouts (`send_batch_size: 1024`, `timeout: 10s`) so low-volume data (and every pipeline of the gateway) flushes promptly.
  - Both set `alternateConfig` (full custom config — the chart's default `config` is NOT merged; it would leave unused `debug`/`jaeger`/`zipkin` receivers). The chart's default `rewriteDeprecatedComponentNames: true` renames the exporter `otlp`→`otlp_grpc` (matches 0.163.2 naming; the `otlp` receiver is unaffected). `command.name: otelcol-contrib` (contrib image binary). Jaeger/zipkin ports disabled.
  - Where the data lands (ClickHouse, `chi-signoz-clickhouse-cluster-0-0-0`): logs in `signoz_logs.logs_v2` (`resources_string` map, no top-level `service_name` column), metrics in `signoz_metrics.samples_v4`, **traces in `signoz_traces.signoz_index_v3`** (v3 schema; the legacy `signoz_spans`/`signoz_index_v2` tables stay empty and are not what the UI reads).
  - **End-to-end depends on the SigNoz onboarding note above**: until SigNoz has an org/account, the bundled collector won't listen on 4317, so these collectors buffer/retry (their own OTLP receivers on 4317/4318 are up and accept data regardless).
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
# server-side check (needs cluster; strict-decodes against the live Application
# CRD, catching fields this ArgoCD version prunes, e.g. syncPolicy.createNamespace,
# source.ignoreDifferences)
kubectl apply --dry-run=server -f argocd/apps/ -f argocd/bootstrap/ -o name
```
