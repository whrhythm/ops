# 星桥用户入口

更新：2026-11-03。本目录部署 LibreChat 和专用 MongoDB；业务源码在 LibreChat 与 platform-control 仓库。浏览器入口：`https://chat.k3s.local:30443/`。

## 当前使用方式

1. 通过 Authentik 自助注册或登录。平台自动准备或复用个人空间、关联 LiteLLM 用户与密钥；已有账号无需重新注册或手工填写密钥。
2. 输入框内加号旁选择“聊天”讨论需求，选择“编程”调用 OpenHands 创建、修改、测试或发布工程。真实模型可分别选择，两者复用同一聊天上下文和个人磁盘；执行过程中不能切换。写作、音乐尚未开放。
3. 展开“我的空间” → 文件，上传文件或浏览 Agent 生成的代码。文件行提供预览、下载、右侧编辑和确认删除；文件夹行可下载 ZIP。当前删除没有回收站。个人磁盘用量包括工程依赖、虚拟环境和缓存。
4. 在编程对话中要求测试后发布，或在“我的应用”选择包含 Dockerfile 的工程并点击发布按钮。服务应监听 `0.0.0.0:8080`。提交后观察构建/部署进度，运行且出现地址后访问；失败时查看失败原因与清理状态，再明确重试。
5. “我的应用”提供访问链接、启停、日志及 CPU/内存用量。停止或删除应用保留个人工程文件。文件浏览与应用管理分开，不替换主聊天。

模型密钥由平台管理，注册赠送 1 美元仅一次。“套餐与用量”显示实际余额、输入/输出及总 Token、个人磁盘和应用名额。基础版 10 元模拟购买，按订单锁定的后台汇率增加等值美元预算；初始测试规则 7.000000 CNY/USD，10 元增加 1.428571 美元，用户及 key 两级预算同时观察，历史 spend 不清零。重复购买可补充模型额度，较小套餐不缩小原文件和应用权益。真实扣费、订阅、退款尚未接入。

PostgreSQL 项目开通、自动连接、实际用量和明确销毁已接入数据库面板；数据库另有 256 MiB 测试预算，两个 128 MiB 预留，不是硬写入配额。自动准备 256 MiB 原生配额私有对象桶，编程测试和下次发布自动取得连接。账本汇总文件、数据库、对象版本、Harbor 原生计数及额外应用 PVC；全部样本有效才显示完整受管逻辑用量，否则显示小计及未知/过期。分类额度独立，Harbor 无限额度如实标示；套餐暂不扩大数据库、对象或镜像额度。

管理员在 `https://platform.k3s.local:30443/` 的“模型与套餐管理”设置聊天默认模型、编程默认模型、允许模型、结算汇率及套餐。原生智能体模型列表只显示真实模型。新增文案默认中文，保留用户语言偏好；同类编程进度去重并折叠，新对话自动摘要命名。详见[模型与套餐手册](https://github.com/whrhythm/platform-control/blob/main/docs/model-membership-guide.md)和[本阶段验收](https://github.com/whrhythm/platform-control/blob/main/docs/chat-membership-acceptance-20261103.md)。

LibreChat 原生 Projects 用于整理聊天，与持久个人文件空间不同。共享 OpenHands Web 实例不是用户编程运行时；Coding 使用共享网关和用户个人 Kata 环境中的轻量执行进程。

个人存储：[使用手册](https://github.com/whrhythm/platform-control/blob/main/docs/storage-accounting-guide.md)、[真实 UI 验收](https://github.com/whrhythm/platform-control/blob/main/docs/storage-accounting-acceptance-20261103.md)。包含对象应用的聊天测试、发布、启停保留及失败回收；真实支付、跨存储套餐调整、额外应用卷创建与备份恢复尚未接入。

项目数据库：[使用手册](https://github.com/whrhythm/platform-control/blob/main/docs/project-databases-guide.md)、[真实 UI 验收](https://github.com/whrhythm/platform-control/blob/main/docs/project-databases-acceptance-20261103.md)。验收覆盖上传测试、OpenHands 读写数据库、镜像构建推送发布、访问计数持久化、停止恢复、明确空库销毁和逻辑账本用量。

## 部署与配置

- ArgoCD 应用：`librechat`；平台后端：`platform-control`；当前地址解析到 `192.168.10.30`。
- Authentik `platform` OIDC 客户端使用 PKCE；浏览器访问 chat 与 authentik 两个域名及端口 30443，须正常验证内部 CA 证书。
- 文件与应用使用 LibreChat 同源接口，后端转发平台 API；普通用户操作不再依赖浏览器直接请求 platform 域名。
- `librechat.yaml` 中的 `codechat.capabilities` 显式启用能力入口。chat/coding 路由别名需与平台 `PLATFORM_CAPABILITY_*` 配置一致；真实聊天模型由平台允许列表验证，旧 OpenHands 会话保持兼容。
- 安装前创建 `librechat-secrets`、`librechat-ca`、`librechat-codechat-provisioning`。控制面仓库的 `scripts/bootstrap_librechat.py`、`scripts/bootstrap_model_credentials.py` 复用已有密钥，不自动轮换。Secret 不写入 Git。
- MongoDB、LibreChat 上传目录与个人工作卷是不同数据，应一并备份并演练恢复。当前单节点不提供高可用。
- 不启用 Meilisearch、RAG、Dify 或 LibreChat 本地密码注册。当前面向内部可信用户；注册邮箱验证及公网反滥用仍待配置。

详细用户手册、编程协议及验收记录见 [platform-control 文档](https://github.com/whrhythm/platform-control/tree/main/docs)。自动模型开通运维见 [MODEL_KEY_GUIDE.md](MODEL_KEY_GUIDE.md)。
