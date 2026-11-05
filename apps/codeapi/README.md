# LibreChat 独立代码执行服务

上游为 ClickHouse/code-interpreter，固定源码提交 `9a3f5dbd6fedf42462239d135c2c25ec8e737628`。`chart/` 来自该提交的 Helm chart 0.3.1，保留原许可证；镜像通过内部 Harbor 固定摘要部署。

## 边界

- 用户浏览器只访问 LibreChat。LibreChat 签发最长 300 秒、绑定用户的 Ed25519 JWT；CodeAPI 只保存公钥。禁止 LOCAL_MODE 和匿名执行。
- API、队列、文件服务、执行调度、工具服务、出口网关和执行器分别运行。执行器使用 `/dev/kvm` + libkrun 微虚拟机及 NsJail；不要求关闭现有 OpenHands 的 Kata 隔离。
- 专用设备插件发布 `starbridge.internal/kvm`，执行器请求一个设备份额以获得设备访问权限，无需特权模式；仅挂载设备文件不足以授予 cgroup 设备权限。
- 执行器只能连接受控出口网关；无平台、MinIO、Redis 管理凭据，也不挂载用户个人空间。
- 这是聊天代码块的临时计算环境。OpenHands 编程仍使用个人工作空间。二者不自动共享文件；不要把临时执行产物当作持久项目文件。
- 文件保存在共享 MinIO 的独立 `starbridge-codeapi-files` bucket；仅该 bucket 的运行身份有权限。总容量上限 2 GiB，产物按一天生命周期到期清理。MinIO 生命周期为异步清理，并非精确 24 小时删除。
- 当前为单节点、单套服务。两路任务并发，代码执行时间默认 15 秒、输出 64 KiB；服务有独立资源限制。代码执行临时容量不应冒充用户个人磁盘用量。

## 运维

1. `platform-control/scripts/build_code_interpreter.py` 构建固定上游版本并推送 Harbor。构建依赖 NsJail 固定提交及其固定子模块，禁止跟随 master 更新。
2. `bootstrap_code_interpreter.py` 幂等配置独立凭据和 MinIO 空间。凭据只进入 Kubernetes Secret，不进入 Git 或浏览器。
3. `configure_code_interpreter.py` 生成仅含公钥和镜像摘要的 `values.yaml`。
4. Argo CD Application `codeapi` 管理运行资源。修改后先 `helm template` 和服务器端 dry-run，再同步。
5. JWT 私钥保存在 `librechat/librechat-codeapi`；内部凭据保存在 `codeapi/codeapi-secrets`。恢复集群时需恢复这些 Secret 和 Redis PVC。

本地 chart 差异：不用内置 Redis/MinIO 子 chart；提供独立受限 Redis；Secret 外部管理；API 入站仅允许 LibreChat；Redis 与外部 MinIO 出站限定至实际服务。用户代码不得通过扩宽执行器出站来解决连通问题。
