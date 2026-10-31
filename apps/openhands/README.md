# OpenHands 单项目验证环境

本部署采用官方 OpenHands Agent Canvas 1.34.0（上游仓库标签 `v1.34.0`，提交 `6c120073f5a1e1c838ba6fdd9a075c79ff997b98`）的 Helm chart。chart 已放入本仓库，唯一的模板改动是增加 `runtimeClassName` 参数，以便使用已安装的 `kata-qemu-runtime-rs`。

**这是内部单用户、单项目验证实例，不是多租户平台。** Agent Canvas 的任务共享同一工作空间和运行实例；不可将这个实例同时提供给相互不信任的用户。后续平台应按项目创建独立沙箱、持久卷和访问凭据，并在控制面校验用户与项目的关系。

## 组件和边界

- Argo CD：`openhands-prereqs` 管理 namespace 和网络策略；`openhands` 管理 chart。
- 运行时：`kata-qemu-runtime-rs`；未授予 Kubernetes 管理权限，也不自动挂载 ServiceAccount token。
- 存储：`longhorn` 20 GiB，保存 `/home/openhands/.openhands` 和 `/home/openhands/workspace`。默认的 SQLite 数据保存在该卷中；当前尚未接入共享 PostgreSQL 或 MinIO。
- 网络：服务为 ClusterIP，不创建公开入口。网络策略限制入站和出站；出站仅允许集群 DNS 和现有模型地址 `192.168.10.30:4000`。地址或网络变化时需要调整策略。
- 认证：上游会在持久卷生成并保存 API key 和加密密钥；请勿将密钥提交进 Git。此机制不是 Authentik SSO，也不是多租户授权。
- 默认不配置 Docker socket、特权容器、模型密钥或 Kubernetes API 权限。Agent 在 Kata Pod 内执行任务；没有为每次对话创建新的沙箱。

## 临时访问

在可信工作站上保持一个终端运行：

```bash
kubectl --kubeconfig /path/to/config -n openhands port-forward svc/openhands-agent-canvas 18000:8000
```

打开 `http://127.0.0.1:18000/canvas`。如果界面要求输入本地会话密钥，在你自己的终端中读取（不要将输出发给他人）：

```bash
kubectl --kubeconfig /path/to/config -n openhands exec statefulset/openhands-agent-canvas -- cat /home/openhands/.openhands/agent-canvas/api-key.txt
```

只有拥有集群管理权限的可信操作者应能执行上述命令。尚未配置模型：需在界面设置或后续控制面集成时配置模型地址、模型名和凭据；不要将凭据写入公开仓库。

## 验证

```bash
helm lint apps/openhands/chart -f apps/openhands/values.yaml
kubectl -n argocd get applications openhands openhands-prereqs
kubectl -n openhands get pods,pvc,svc,networkpolicy
kubectl -n openhands logs statefulset/openhands-agent-canvas --tail=100
kubectl -n openhands exec statefulset/openhands-agent-canvas -- uname -r
```

Pod 内核应为 Kata guest 内核，而不是节点的内核。首次拉取镜像可能需要较长时间。删除应用前应先备份 PVC；StatefulSet PVC 默认保留，namespace 也标记为不被 Argo CD 自动剪除。

更多说明：[官方 chart](https://github.com/OpenHands/OpenHands/tree/v1.34.0/helm/agent-canvas)；[官方自托管安全说明](https://github.com/OpenHands/OpenHands/blob/v1.34.0/docs/SELF_HOSTING.md)。
