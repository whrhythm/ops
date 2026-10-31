# LibreChat 用户入口（部署中的集成基础）

本目录部署 LibreChat 和专用 MongoDB。项目管理、付费权益、OpenHands 和发布尚未接入，不表示这些能力已经可用。现有共享 OpenHands 不得作为多租户运行时。

- Argo CD 应用：`librechat`；域名：`https://chat.k3s.local:30443/`。
- 登录使用 Authentik 的 `platform` 公共 OIDC 客户端及 PKCE，复用 issuer / subject；本地邮箱登录关闭。平台 API 仍独立验证令牌、成员和角色；仅在 LibreChat 登录不代表项目权限打通。
- 安装前需在命名空间中创建 `librechat-secrets` 和 `librechat-ca`；可使用控制面仓库的 `scripts/bootstrap_librechat.py`。脚本复用已有密钥，不自动轮换。不要将 Secret 写入 Git。
- MongoDB 与上传目录使用 Longhorn PVC。备份需覆盖 MongoDB、上传文件和 Secret，定期执行恢复演练。当前单节点不提供高可用。
- 不启用 LibreChat 的 Meilisearch、RAG、Dify 或内置注册；它们不是本阶段的交付能力。

安装后应检查 Argo 同步、Pod、PVC、证书、实际 OIDC 登录与数据持久化。环境使用内部 CA，访问终端须信任该 CA；域名需解析到可访问 APISIX 30443 的节点地址。
