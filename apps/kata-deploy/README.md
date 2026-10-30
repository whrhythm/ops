# Kata Containers on the single-node k3s cluster

Kata Containers 4.3.1 was installed on node `edge` on 2026-10-30.
The official chart archive was fetched via the NJU GHCR mirror; its SHA-256
`c25077416d37c178bf72df0204ea3ba89418efad1b1299b0d5d34ec02556187e`
matches the upstream 4.3.1 release asset. The chart is vendored because the
cluster's Argo CD / Helm version cannot reliably use OCI chart sources.

The child Argo CD Application has **manual sync**, so syncing the parent only
creates/updates the child; an operator must explicitly sync `kata-deploy` after
the prerequisites below are met. The installer is privileged, writes host files
and containerd configuration, and restarts the `k3s` service. The single-node
API and applications can be interrupted. Installation does not change the
default runtime; only Pods explicitly requesting `kata-qemu-runtime-rs` use Kata.

## Checks and release gate

- Node `edge`: Ubuntu 22.04, x86-64, Intel VT-x, `/dev/kvm`,
  `/dev/vhost-vsock` and `/dev/vhost-net` verified on 2026-10-30.
- The official chart is configured for `k3s` and the explicit node `edge`.
- On 2026-10-30, unprivileged image probes confirmed k3s containerd pulled
  and started both install images, including their actual layers:
  `quay.io/kata-containers/kata-deploy:4.3.1` and
  `ghcr.nju.edu.cn/kata-containers/k8s-job-dispatcher:0.3.0`. The installer
  image took about 10 minutes to pull and consumed several GiB when unpacked.
  If pulls later fail, mirror the images into Harbor and update the values
  before sync. Avoid placing registry credentials in Git.
- Check spare disk space. After cleanup, the node reported 572 GiB available
  and 35% used on 2026-10-30; monitor Longhorn free capacity during install.
- Confirm an operator can access the node console and restore k3s if it does
  not restart successfully. SSH login was available, but `sudo -n` required a
  password; keep an independent recovery path.
- Confirm workload disruption is acceptable and take the cluster's usual
  backups. Record the existing containerd configuration and k3s service state.

## Validate before syncing

```bash
helm template kata-deploy apps/kata-deploy/kata-deploy \
  -n kube-system -f apps/kata-deploy/values.yaml > /tmp/kata-rendered.yaml
kubectl apply --dry-run=server -f argocd/infra/kata-deploy.yaml
kubectl apply --dry-run=server -f /tmp/kata-rendered.yaml
```

Review the rendered Job, cluster RBAC, privileged containers, host mounts,
node selection, `RuntimeClass` and Helm hook behavior with the cluster's Argo
CD version before performing the manual sync. Do not assume a Helm hook works
identically under Argo CD: verify the child Application completes successfully.

## Smoke test after installation

The job's successful completion alone is insufficient. Check node readiness,
the k3s service, other Argo CD applications, and the RuntimeClass:

```bash
kubectl get nodes
kubectl get runtimeclass kata-qemu-runtime-rs
kubectl -n kube-system get jobs,pods | grep kata
kubectl get applications -A
```

Run `apps/kata-deploy/smoke-test.yaml` manually in a dedicated test namespace.
Compare its `uname -r` output with `uname -r` on the host: a Kata guest kernel
should differ. Verify networking, storage, shutdown and scheduling as needed.
Delete only the test Job/namespace after checking results.

The 2026-10-30 smoke test completed with guest kernel `6.18.56`; the host ran
`5.15.0-198-generic`. Argo CD reported `kata-deploy` Synced/Healthy after the
installer restarted k3s. This verifies a basic Kata Job, not OpenHands,
network/storage isolation, Kubernetes Secret propagation or multi-tenancy.

## Rollback

Never delete the installer's Argo CD Application as an assumed rollback:
Helm pre-delete/post-delete hooks and Argo CD deletion semantics differ, and
node files may remain. Drain/stop Kata workloads first. Consult the pinned
chart's uninstall jobs and perform the chart's supported cleanup against this
release, then confirm the `kata-qemu-runtime-rs` handler, node label and host
drop-ins were removed and k3s returned Ready. This removal also restarts k3s.
Until cleanup has been rehearsed, the safe immediate mitigation is to stop
scheduling new Kata workloads and leave the existing runtime files intact.

Sources: https://github.com/kata-containers/kata-containers/releases/tag/4.3.1
and https://github.com/kata-containers/kata-containers/blob/4.3.1/docs/helm-configuration.md
