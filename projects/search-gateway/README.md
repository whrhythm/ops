# 共用搜索包装

SearXNG 负责向已配置的搜索源查询。包装服务对结果清洗、去重、按查询词与标题和摘要的匹配度重排，限制返回数量。项目原文读取工具继续独立运行，保留已有的登记、审核和版本。

| 入口 | 输入 | 输出 |
| --- | --- | --- |
| 集群内 HTTP `/search` | `q`、可选 `time_range` | `status`、`results[{title,url,content}]` |
| Streamable HTTP MCP `/mcp` 的 `web_search` | `query`、可选 `limit`（默认 5，最多 12） | `status`、`results[{title,url,snippet}]` |

HTTP 为项目原文读取工具保留 SearXNG 字段名。MCP 的 `snippet` 是搜索摘要。需要引用原文时，调用项目现有的原文读取能力。

成功且有结果的查询在单个实例中缓存 60 秒，最多保留 128 个查询与时间范围组合。返回上限改变时复用同一份排序结果。空结果、超时和上游错误不进缓存。返回状态区分 `results`、`no_results`、`degraded` 和 `unavailable`。运行日志只记录状态和条数。

候选服务地址为 `http://search-gateway.search-system.svc.cluster.local:8080`，由网络策略限制访问。SearXNG 的会话密钥来自集群 Secret。包装服务只允许连接 SearXNG 和集群 DNS，网页原文由授权的项目工具另行获取。

部署配置见 `k8s/`。先给 `search-system` 创建 `searxng-session` Secret，再应用 `searxng.yaml`。构建包装镜像，使用不可变镜像摘要替换 `gateway.yaml` 中的 `SEARCH_GATEWAY_IMAGE` 后应用。以 `python -m unittest -v test_app.py` 运行包装回归。
