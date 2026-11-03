# LibreChat / CodeChat 用户入口

更新：2026-11-03。本目录部署 LibreChat 和专用 MongoDB；业务源码在 LibreChat 与 platform-control 仓库。浏览器入口：`https://chat.k3s.local:30443/`。

## 当前使用方式

1. 通过 Authentik 自助注册或登录。平台自动准备或复用个人空间、关联 LiteLLM 用户与密钥；已有账号无需重新注册或手工填写密钥。
2. 聊天框上方选择 Chat 讨论需求，选择 Coding 创建、修改、测试或发布工程。两者复用同一聊天和个人磁盘；执行过程中不能切换。Writing、Music 尚未开放。
3. 展开“My space” → Files，上传文件或浏览 Agent 生成的代码。文件行提供预览、下载、右侧编辑和确认删除；当前删除没有回收站。个人磁盘用量包括工程依赖、虚拟环境和缓存。
4. 在 Coding 对话中要求测试后发布，或在 My applications 选择包含 Dockerfile 的工程并点击“Publish this project”。服务应监听 `0.0.0.0:8080`。提交后观察构建/部署进度，运行且出现地址后访问；失败时查看失败原因与清理状态，再明确重试。
5. My applications 提供访问链接、启停、日志及 CPU/内存用量。停止或删除应用保留个人工程文件。文件浏览与应用管理分开，不替换主聊天。

模型密钥由平台管理，一次性 1 美元测试模型预算不会因重复登录或空间升级再次发放；该预算与磁盘容量不同。Storage & plan 已提供测试套餐升级、取消、测试确认和原盘扩容，真实扣费/续费尚未接入。升级观察完成才显示新权益，失败保留文件和原权益。PostgreSQL/MinIO 项目开通及跨存储完整计量仍待实施；未计量分项不显示为零。当前测试价格不可用于财务结算。

LibreChat 原生 Projects 用于整理聊天，与持久个人文件空间不同。共享 OpenHands Web 实例不是用户编程运行时；Coding 使用共享网关和用户个人 Kata 环境中的轻量执行进程。

## 部署与配置

- ArgoCD 应用：`librechat`；平台后端：`platform-control`；当前地址解析到 `192.168.10.30`。
- Authentik `platform` OIDC 客户端使用 PKCE；浏览器访问 chat 与 authentik 两个域名及端口 30443，须正常验证内部 CA 证书。
- 文件与应用使用 LibreChat 同源接口，后端转发平台 API；普通用户操作不再依赖浏览器直接请求 platform 域名。
- `librechat.yaml` 中的 `codechat.capabilities` 显式启用能力入口。chat/coding 路由别名需与平台 `PLATFORM_CAPABILITY_*` 配置一致；真实聊天模型由平台允许列表验证，旧 OpenHands 会话保持兼容。
- 安装前创建 `librechat-secrets`、`librechat-ca`、`librechat-codechat-provisioning`。控制面仓库的 `scripts/bootstrap_librechat.py`、`scripts/bootstrap_model_credentials.py` 复用已有密钥，不自动轮换。Secret 不写入 Git。
- MongoDB、LibreChat 上传目录与个人工作卷是不同数据，应一并备份并演练恢复。当前单节点不提供高可用。
- 不启用 Meilisearch、RAG、Dify 或 LibreChat 本地密码注册。当前面向内部可信用户；注册邮箱验证及公网反滥用仍待配置。

详细用户手册、编程协议及验收记录见 [platform-control 文档](https://github.com/whrhythm/platform-control/tree/main/docs)。自动模型开通运维见 [MODEL_KEY_GUIDE.md](MODEL_KEY_GUIDE.md)。
