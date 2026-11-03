# 平台控制面部署

本目录为 GitOps 部署，业务源码位于同工作区 `platform-control/`。后端提供个人持久空间、文件管理、编程执行、构建发布、项目 PostgreSQL、自动私有 MinIO 桶及五类存储计量；管理员资源申请控制台保留规划功能。普通用户从 [LibreChat](../librechat/README.md) 操作。真实支付、跨服务统一硬配额和对象/镜像付费扩容仍待实施。

## 存储计量部署（2026-11-03）

- `runtime/storage.yaml` 是独立对象准备和计量服务，复用已验证的应用控制器 SA 与用户 RoleBinding。只使用专用非 root MinIO 管理账号；业务 API 不挂载此账号。
- 先在业务仓库执行 `scripts/bootstrap_object_storage.py`，指定已构建的 `PLATFORM_STORAGE_BOOTSTRAP_IMAGE`。脚本通过临时本机转发创建或核验 Secret `platform-object-storage-provisioner`；不输出凭据，不改变已有对象桶，不对外暴露 MinIO。
- 新库迁移为 `0013_storage_providers`。迁移 hook 先完成 schema 与受限 grants，再启动新后台。对象连接由用户 namespace 的 Secret 注入应用，不进入 Git 或镜像。
- `PLATFORM_STORAGE_METERING_ENABLED` 与 `PLATFORM_OBJECT_STORAGE_ENABLED` 在配置中显式开启。MinIO 当前为内部 HTTP 9000；每桶原生测试额度 256 MiB；完成的对象版本纳入计量，不设置自动销毁已完成对象的规则。
- 后台 worker 账号预算至少 16 个连接；已有环境执行 `scripts/configure_worker_connections.py`，只上调原受管角色的连接数，不改密码。四个后台部署分别限制连接池 1 个常驻连接、最多 1 个溢出，预留滚动更新与探针余量。API 保留独立连接预算。
- 当前应用不创建额外 PVC；完整核验仅有原个人卷才记额外应用盘为零。未知额外卷显示未完整计量。Harbor 原生无限配额如实显示，不强制缩小既有用户权益。
- 文件套餐测试升级仅扩大文件盘和应用数。数据库、对象及镜像额度分别执行，尚未形成可挪用的统一硬配额。

完整协议、用户手册与真实验收在业务仓库 `docs/storage-accounting-*.md`；部署清单固定不可变 digest。回滚镜像不自动降级 schema、删除对象桶或缩回用户磁盘。

用户操作见 [使用手册](USER_GUIDE.md)。已完成真实 PKCE 双用户登录、项目创建、幂等、计划 worker、配额预留/释放、跨租户拒绝和 API Pod 替换后的持久化验收。临时测试账号和元数据已清理，成功操作审计保留。

## 访问

- 平台：`https://platform.k3s.local:30443/`
- 登录：`https://authentik.k3s.local:30443/`
- 通过 APISIX TLS NodePort `30443`，两者使用同一节点地址、不同 SNI/Host。
- Tailscale 可达的客户端将上述两个域名解析到 `100.92.98.122`；节点原局域网内可解析到 `192.168.10.30`。
- 客户端需信任现有内部 CA。证书由 cert-manager `internal-ca` 签发；没有关闭后端、数据库或镜像仓库的 TLS 校验。

若浏览器所在系统无法直接访问该 Tailscale 网络，可保持 SSH 隧道运行：

```bash
ssh -N -L 127.0.0.1:30443:192.168.10.30:30443 edge@100.92.98.122
```

此时将两个域名都解析到 `127.0.0.1`，访问 URL 不变，以保持 OIDC issuer、PKCE redirect 和证书域名一致。不要直接改用 IP 登录。

使用现有 Authentik 用户登录。`akadmin` 已加入非 Authentik 超级权限的 `platform-admins` 组；平台管理员权限仅在平台自己的管理接口生效，不会把普通平台管理员变成 Authentik 超级管理员。

## 历史初始镜像（2026-10-31）

- 标签：`harbor.k3s.local/internal/platform-control:0.1.0-20261031.1`
- 部署固定 digest：`sha256:ad2973826dac4965dcc7ffdc3e1386ed7e30b5fda9d018373dd907003ab2160b`；模拟支付默认关闭。
- 镜像包含已编译 React 界面和 Python API/计划 worker，运行时三个进程分别用于迁移、API、worker。
- 本地完整镜像构建和 Python 3.12 镜像内测试已通过；完整归档跨 VPN 上传持续超时，因此改用 SHA256 校验过的小构建包，在 edge 用同一源码、前端产物和带哈希的锁定依赖重建并推送 Harbor。完整本地归档未传完，不冒充完成。
- 目标主机发布目录：`/home/edge/platform-releases/20261031/`，构建日志 `build.log`，镜像发布结果 `published.json`。临时 Harbor 登录目录会在推送结束自动删除。

## 应用与配置

- `platform-prereqs`：namespace（Prune=false）、平台证书、NetworkPolicy、Authentik 蓝图和登录入口。
- `platform-control`：公共 ConfigMap、迁移 Sync hook（wave 1）、API/worker（wave 2）和平台路由（wave 3）。
- Authentik chart 挂载 `platform-authentik-blueprint`，公共客户端 `platform-console`，只开放 authorization_code；返回地址严格匹配平台 URL。API 要求 RS256、正确 issuer/audience、有效期和 platform-api scope。
- 网关为现有 APISIX 新增 TLS NodePort `30443`，保留原 HTTP NodePort `30097`；平台与 Authentik 的 HTTP 路由跳转到 HTTPS。
- APISIX Admin API 与 KIC 共用外部 Secret `apisix-admin-auth`（admin/viewer），已替换 chart 的公开默认密钥。因当前服务 SNAT，管理 ACL 允许节点与 Pod 地址段，并配有管理端口 NetworkPolicy。不要将 Secret 内容写入 Git 或日志。

## 一次性外部配置

从业务源码目录运行 `scripts/bootstrap_cluster.py`，需 Python + PyYAML + psycopg、SSH 和目标主机 kubectl 权限。凭据不进入 Git：

| 配置 | 用途 |
| --- | --- |
| Secret `platform-db-migrator` | `platform_migrator`，数据库 owner，只给迁移任务 |
| Secret `platform-db-api` | `platform_api`，元数据 DML；审计只 SELECT/INSERT |
| Secret `platform-db-worker` | `platform_worker`，只读租户、受限资源/任务更新、审计 INSERT |
| ConfigMap `platform-ca` | 内部 CA 与 PostgreSQL CA 的公开证书 |

共享 PostgreSQL 数据库为 `platform_control`。CNPG pg_hba 将三个平台角色限制为只连接此数据库；原有应用角色不受影响。连接使用 `sslmode=verify-full` 和 `postgres-rw.postgres.svc.cluster.local`，不是不校验证书的 require 模式。

迁移 Job 先执行 Alembic，再授予运行角色必要权限。API/worker 不持有迁移凭据、不挂载 Kubernetes token、没有 Kubernetes RBAC、非 root、只读根文件系统。数据保存在共享 PostgreSQL，不使用 API 本地 SQLite。

CA 更新后应重新运行 bootstrap 同步公开信任束，并滚动重启 API/worker；无需重新生成数据库密码。不能删除 Secret 后盲目运行 bootstrap 重置已有角色密码。

## 当前网络待整改

已确认节点 PodCIDR 为 `10.42.0.0/24`，Calico 地址池为 `172.16.0.0/16`，APISIX 日志中的控制器来源因此表现为节点 `192.168.10.30`。本次未重启或改写 k3s 的集群地址段。真正开放租户代码执行之前，应统一配置并实测服务访问及跨租户 NetworkPolicy，不能只依赖当前的来源标签假设。

## 验证与回退

```bash
kubectl -n argocd get applications platform-prereqs platform-control
kubectl -n platform-system get pods,svc,certificate,networkpolicy
kubectl -n platform-system logs deployment/platform-api --tail=50
kubectl -n platform-system logs deployment/platform-worker --tail=50
```

API `/healthz` 是存活检查，`/readyz` 验证数据库连接；不带 token 的 `/v1/me` 应返回 401。正常登录后应能创建个人空间、项目和资源申请，worker 将 requested 改为 planned，并返回 `executable=false`。

回退时恢复前一镜像 digest 和对应配置；数据库迁移需独立评估，不能为回滚镜像自动降级或删除平台数据库。删除平台 Application 不会自动删除外部数据库与 Secret；Namespace 标记为不自动剪除。APISIX HTTPS 与 Authentik 新 provider 的回退要分别恢复对应 Git 提交。
