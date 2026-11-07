# 星桥项目知识服务

2026-11-07，P1-02。由 `argocd/apps/knowledge.yaml` 管理，独立 namespace `knowledge`。它是 Platform Control 私有依赖，不是公开 RAG API，也不配置 LibreChat 的全局 `RAG_API_URL`。

组成：单副本知识 API（FastEmbed 0.7.3、固定中文 ONNX 模型）、单副本 PostgreSQL/pgvector、4 GiB Longhorn PVC、显式 schema 初始化任务和 NetworkPolicy。原有共享 PostgreSQL 不变。CPU/内存上限与镜像摘要见 `app.yaml`，模型及验收记录见相邻 `platform-control-p1/docs/p1-rag-decision.md` 和 `p1-knowledge.md`。

## 前置 Secret

Git 不保存凭据。首次部署前由受控运维流程创建，下次操作保留既有值：

| namespace / Secret | 必需字段 | 用途 |
| --- | --- | --- |
| knowledge / knowledge-postgres | password、app-password、service-token | 保存部署凭据源；初始化数据库管理员与应用角色 |
| knowledge / knowledge-migrator | KNOWLEDGE_ADMIN_URL、KNOWLEDGE_APP_PASSWORD | 仅 schema Job 使用管理员凭据 |
| knowledge / knowledge-service | KNOWLEDGE_DATABASE_URL、KNOWLEDGE_SERVICE_TOKEN | API 使用无 DDL 权限的应用角色 |
| platform-system / platform-knowledge | PLATFORM_KNOWLEDGE_SERVICE_TOKEN、PLATFORM_MODEL_CREDENTIAL_ENCRYPTION_KEY | API/索引器访问服务；加密密钥与现有控制面相同 |

不得因重跑部署生成新数据库密码或更换原文加密密钥。相关脚本必须捕获 Secret 内容而不输出到日志。

## 发布与观察

1. 先部署知识数据库/schema/API，确认模型加载及数据库探针通过。
2. Platform Control 执行迁移 `0024_project_knowledge`，再部署兼容 API 与 `knowledge-worker`。
3. LibreChat 使用兼容镜像并设置 `aiProjects.knowledgeEnabled: true`；通过 UI 导入合成资料并检索，不能仅凭 Pod Running 判断产品已可用。
4. 观察 ArgoCD revision / Synced / Healthy、知识修订状态和实际来源打开。服务内部错误不向用户暴露原始异常或资料内容。

NetworkPolicy 默认拒绝：只有控制面 API/知识索引器可访问知识 API；知识 API 只能访问自己的向量库和 DNS。模型已经固化到镜像，运行时不需要联网下载。

## 关闭与数据保护

关闭功能使用 LibreChat / Platform Control 开关，保留数据。不要回滚到要求旧数据库版本的 API 镜像。PVC 设置 `Prune=false`，不能把删除 Application 当成数据清理流程。

用户删除修订先记录清理意图；向量服务确认墓碑及清理后，控制面才释放逻辑容量。索引失败重试先核验已提交摘要，不能手改状态为 ready。移除当前知识不会自动回退其他历史修订。

此单节点新数据库必须纳入备份清单；当前没有异机备份。保留控制面原文、加密密钥与向量库备份的对应版本。尚未交付向量库全库灾难恢复工具，不能宣称整库丢失后自动重建；正式客户试点前需完整恢复演练。
