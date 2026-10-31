# 集群内 LiteLLM 计量代理

本目录部署开源 LiteLLM 代理（`ghcr.io/berriai/litellm:v1.109.0-dev.2`，经 BerriAI cosign.pub 验签后两步法推入 Harbor），为 LibreChat 提供**按用户 virtual key 独立计量**的 OpenAI 兼容入口。节点上 `192.168.10.30:4000` 的 host 级 LiteLLM 不受本目录管理，仍供 kubepilot / openhands 使用（Open WebUI 已下线，LibreChat 接管聊天入口）。

- Argo CD 应用：`litellm`（wave 0，业务 app）；命名空间 `litellm`。
- 服务：ClusterIP `litellm:4000`，**不创建公开入口**；管理 UI 用 `kubectl -n litellm port-forward svc/litellm 4000` 后访问 `/ui`（master key 登录）。
- 模型：当前仅 `deepseek-v4-flash-v2`（OpenAI 规范，上游 `https://api.siliconrouter.com/v1`，key 来自 Secret，不进 Git）。
- **单价必须显式配置**：`deepseek-v4-flash-v2` 不在 LiteLLM 内置 cost map 里（只有 `deepseek-v4-flash`），不配 `model_info.input_cost_per_token/output_cost_per_token` 时 spend 恒为 0、预算永远不触发。当前按占位价 **输入 $0.3/M、输出 $1.2/M**（`3e-7` / `1.2e-6` 每 token）配置，请对照 siliconrouter 实际账单校准。
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

本版本的 `/key/generate` 语义（实测 v1.109.0-dev.2）：

- `key_name` 是名称（脚本用它）；`key` 字段是要**指定**的 token 本身，必须以 `sk-` 开头。
- 模型限定用 `models: ["..."]`，`model_list` 会被拒绝（401）。
- `key_name` 不唯一，重复建会静默产生第二个 key；脚本用 `/spend/keys` 做幂等检查（`/key/list` 只返回 token 散列，无法按名查找）。
- 删除：`POST /key/delete` body `{"keys": ["<散列或 token>"]}`（散列可直接删）。

阶段二（platform-control）：用户首次访问平台"我的 API Key"面板时，`GET /v1/me/litellm-key` 自动创建并展示 key，无需管理员介入。

用户侧使用：LibreChat → Settings → API Keys → 选择 `LiteLLM` 端点，粘贴自己的 virtual key。模型列表由端点按该 key 的授权动态返回（`models.fetch: true`，无服务器端共享 key）。

注意：LibreChat v0.8.9-rc1 的配置 schema 是 `endpoints.custom`（顶层 `customEndpoints` 会启动报 `ZodError: Unrecognized key`），且 `apiKey: user_provided` 才会在 UI 里向每个用户索要自己的 key。`librechat.yaml` 以 subPath 挂载，ConfigMap 更新后必须 `kubectl -n librechat rollout restart deploy/librechat`。

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
# 计量：master key 查 per-key spend（配了 model_info 后 spend 才非 0）
MASTER=$(kubectl -n litellm get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s -H "Authorization: Bearer $MASTER" http://127.0.0.1:14000/spend/keys | python3 -m json.tool
# 预算 429：把测试 key 的预算压到极小（单价低，靠真实花费跑不完 $0.01）
curl -s -X POST -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
  -d "{\"key\": \"$KEY\", \"max_budget\": 0.00001}" http://127.0.0.1:14000/key/update
# 再请求一次应返回 429（spend > max_budget）
```

LibreChat 侧：登录用户粘贴 key 后，模型列表出现 `deepseek-v4-flash-v2` 且对话成功。

## 回滚

删除 `litellm` ArgoCD 应用即移除 Deployment/Service/NP/ConfigMap（`litellm-secrets` 等外部 Secret 与 PG 库保留，脚本幂等可复用）。LibreChat 的 `endpoints.custom` 条目随其应用回退；回退后用户 key 失效，属预期。
