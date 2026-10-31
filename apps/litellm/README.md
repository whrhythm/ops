# 集群内 LiteLLM 计量代理

本目录部署开源 LiteLLM 代理（`ghcr.io/berriai/litellm:v1.109.0-dev.2`，经 BerriAI cosign.pub 验签后两步法推入 Harbor），为 LibreChat 提供**按用户 virtual key 独立计量**的 OpenAI 兼容入口。节点上 `192.168.10.30:4000` 的 host 级 LiteLLM 不受本目录管理，仍供 kubepilot / openhands 使用（Open WebUI 已下线，LibreChat 接管聊天入口）。

- Argo CD 应用：`litellm`（wave 0，业务 app）；命名空间 `litellm`。
- 服务：ClusterIP `litellm:4000`，**不创建公开入口**；管理 UI 用 `kubectl -n litellm port-forward svc/litellm 4000` 后访问 `/ui`（master key 登录）。
- 模型：当前仅 `deepseek-v4-flash-v2`（OpenAI 规范，上游 `https://api.siliconrouter.com/v1`，key 来自 Secret，不进 Git）。
- 数据库：共享 CNPG 集群上的 `litellm` 库（LiteLLM 的 prisma 启动时自动迁移；需要 `citext` 扩展，前置脚本已建）。

## 一次性前置

在具备 cluster-admin kubectl 的机器上运行（幂等）：

```bash
scripts/litellm-prereqs.sh <siliconrouter-api-key>
```

创建：ns、Secret `litellm-secrets`（`LITELLM_MASTER_KEY` 随机、`DATABASE_URL` 随机密码、`SILICONROUTER_API_KEY`）、PG role/数据库 + `citext`、ns `platform-system` 的 Secret `platform-litellm-admin`（平台自动发 key 用，值同 master key）。已存在的 Secret 不覆盖。

## 给用户发 key

阶段一（管理员手动，注册后执行）：

```bash
scripts/litellm-user-key.sh <username> [月度预算美元]   # 默认 $5/月
```

key 限定模型 `deepseek-v4-flash-v2`，`max_budget` 按月重置，超额后 LiteLLM 返回 429。**token 只在创建时打印一次**，请立刻交付用户；丢失需到 LiteLLM UI 轮换或删除后重建。

阶段二（platform-control）：用户首次访问平台"我的 API Key"面板时，`GET /v1/me/litellm-key` 自动创建并展示 key，无需管理员介入。

用户侧使用：LibreChat → Settings → API Keys → 选择 `LiteLLM` 端点，粘贴自己的 virtual key。模型列表由端点按该 key 的授权动态返回（`models.fetch: true`，无服务器端共享 key）。

## 验证

```bash
kubectl -n litellm get pods,svc
# 建一个测试 key（默认 $5/月）
scripts/litellm-user-key.sh e2e-test
# 用返回的 key：
KEY=<上面的token>
kubectl -n litellm port-forward svc/litellm 14000:4000 &
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:14000/v1/models | python3 -m json.tool
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash-v2","messages":[{"role":"user","content":"hi"}],"max_tokens":16}' \
  http://127.0.0.1:14000/v1/chat/completions
# 计量：master key 查 per-key spend
MASTER=$(kubectl -n litellm get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s -H "Authorization: Bearer $MASTER" http://127.0.0.1:14000/spend/keys | python3 -m json.tool
# 预算：scripts/litellm-user-key.sh budget-test 0.01 后持续请求，超额应 429
```

LibreChat 侧：登录用户粘贴 key 后，模型列表出现 `deepseek-v4-flash-v2` 且对话成功。

## 回滚

删除 `litellm` ArgoCD 应用即移除 Deployment/Service/NP/ConfigMap（`litellm-secrets` 等外部 Secret 与 PG 库保留，脚本幂等可复用）。LibreChat 的 `customEndpoints` 条目随其应用回退；回退后用户 key 失效，属预期。
