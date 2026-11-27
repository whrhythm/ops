# 星桥用户与离线部署指南

适用于 2026-11-27 的 Linux amd64 单节点交付。正式配置基线为 `d8c64ac`。本指南中的路径以离线包解开后的 `starbridge/` 为起点。仓库内的对应入口是 `distribution/`。

## 1. 交付内容与源码

`projects/` 中有 **7 个一级源码项目**。平台控制面包含 OCR、Word 渲染、计量、知识服务和联网搜索等子模块。产品使用 **8 个定制镜像仓库**。一个源码项目可发布多个镜像，多份不同源码也可以发布到同一镜像仓库。

| 源码项目 | 用途及入口 | 镜像仓库或运行方式 |
|---|---|---|
| office-chat | LibreChat 前后端；api/server、client、packages。release 下保留聊天文件入口和 Word 侧栏的发布补丁 | starbridge-office |
| platform | control_plane、migrations、web 为平台 API、控制器、后台任务、用户空间和计量。deploy/knowledge 为知识服务，deploy/word-pages 为 Word 渲染，services/managed-search 为个人联网搜索 | platform-control、platform-meter、platform-knowledge、office-word-pages；个人联网搜索发布在 starbridge-office |
| codeapi | 聊天内文件、执行任务、沙箱和受限出口；入口在 api、service、docker | platform-control 仓库中不同的 API、worker、runner、files、tools、egress 镜像 |
| search-gateway | 项目共用的 HTTP/MCP 搜索网关，调用 SearXNG；与个人聊天的 office-web-search 分别部署 | starbridge-search-gateway |
| web-search | 项目级网页工具；v1、v11 为分别保存的源码版本 | 按项目授权发布；没有全局常驻实例要求 |
| support-quote | 既有演示报价服务和 OpenAPI 契约 | starbridge-demo-tool |
| pdf-mcp | PDF MCP 运行入口、内嵌页面与依赖锁；按已授权工作空间启用 | starbridge-pdf-mcp |

四个核心命名空间中有 24 个使用定制镜像的常驻 Deployment、23 个不同的定制镜像摘要。另有按需任务、平台扩展、数据库和基础设施。可在 `workloads.json` 查询此次读取到的工作负载，在 `release.json` 查询镜像源摘要与离线运行名。源码目录来自工作树的运行代码，包含相关未提交功能；`source-bases.json` 记录其 Git 基点。部署以归档镜像为准。从源码重建还需对应版本的包缓存、基础镜像、知识模型及字体构建依赖，尚未验证所有源码能逐字节复现发布镜像。

| 包内文件 | 用途 |
|---|---|
| deploy-offline.py | 校验、安装基础 K3s、导入镜像、准备站点、部署与核验 |
| media/ | 带签名的基础和业务工程介质，含 K3s、Ubuntu 软件包、镜像、模板和站点契约 |
| current.oci.tar | 从正式 Harbor 按摘要取回的 5 个补充镜像，涵盖聊天、个人搜索、平台 API、知识服务与其数据库 |
| ops-source.tar.gz | 本次 ops 提交的源码、配置和构建入口 |
| updates.json、templates/ | 相对签名基线的 3 项工作负载更新，以及知识服务与共用搜索两组配置 |

## 2. 选择安装范围

| 目标 | 执行章节 | 完成标志 |
|---|---|---|
| 验证介质 | 3 | 外层 SHA-256、内层签名和补包摘要全部通过 |
| 空机安装 K3s，离线验证真实 Word 转换 | 3、4 | 节点和 CoreDNS 就绪，提交一份 DOCX 得到一页 JPEG，输入摘要一致 |
| 部署完整个人办公链路 | 3 至 7 | 站点已提供数据库、身份、存储、路由、模型、凭据和隔离运行时；业务组上线，并由普通用户完成登录、聊天、生成文档、保存、重开与用量核对 |

目标机使用 Ubuntu Server 22.04 amd64、systemd、本地管理员和可用的默认 IPv4 路由。介质与容器存储要分别预留空间，压缩镜像导入后还需要解包空间。节点报告的历史容量为 20 CPU、62 GiB RAM；完整办公负载的最低容量尚待压测。文件执行需要可用的 /dev/kvm、对应的运行时和设备插件。断网安装不依赖外部镜像仓库；断网聊天需要现场可达的模型 API。公网搜索、天气、邮件和外部模型需要相应网络或已配置的站内替代服务。

## 3. 收包与校验

将 tar 与同名 .sha256 放在同一目录。用交付方通过可信渠道提供的 SHA-256 核对文件，再解到本机可执行、空间充足的文件系统。分发介质不含任何站点用户会话、私钥、数据库备份或模型访问密钥。以下命令在目标 Linux 执行。

```bash
sha256sum -c Starbridge-Offline-20261127-linux-amd64.tar.sha256
mkdir -p /opt/starbridge-release
tar -xf Starbridge-Offline-20261127-linux-amd64.tar -C /opt/starbridge-release
cd /opt/starbridge-release/starbridge
python3 deploy-offline.py check
sudo ./media/starbridge check --base-only --target-only
```

现有非本安装器管理的 K3s 或已有生产数据，请使用独立目标机。媒体中有离线系统包。如预检提示缺依赖，先查看安装计划，再安装。

```bash
(cd media && sudo bash system/install-dependencies.sh --simulate)
(cd media && sudo bash system/install-dependencies.sh --apply)
```

## 4. 基础集群与第一份 Word

```bash
sudo python3 deploy-offline.py base
sudo /usr/local/bin/k3s kubectl get nodes
SITE_UID=$(sudo /usr/local/bin/k3s kubectl get ns kube-system -o jsonpath='{.metadata.uid}')
sudo python3 deploy-offline.py word --target-uid "$SITE_UID"
```

Word 命令导入单个渲染镜像，创建专用命名空间 `starbridge-word-offline` 和一个 renderer，然后通过容器回环地址执行真实文档转换。此步骤不需要用户账号、模型或联网。它验证渲染进程；普通聊天里的文件、授权和计费需要第 7 节单独验证。需要本机调用该渲染器时，保持以下端口转发在前台，再从本机提交 DOCX，响应 JSON 包含 sha256 和 base64 编码的 pages。

```bash
sudo k3s kubectl -n starbridge-word-offline port-forward deploy/renderer 8124:8124
# 在另一个终端运行
curl --fail --data-binary @report.docx http://127.0.0.1:8124/pages -o pages.json
```

## 5. 准备办公站点

```bash
sudo python3 deploy-offline.py images
sudo install -d -m 700 /etc/starbridge-site
sudo python3 deploy-offline.py prepare --hostname "$(hostname -s)" --out /etc/starbridge-site/v1
sudo cp media/site.example.json /etc/starbridge-site/site.json
```

镜像导入检查节点容器运行时的实际摘要，可以在中断后重跑。prepare 使用新的输出目录，生成 27 组模板、manifest-index.json 和 site-dependencies.json。模板中的容器镜像均映射到已归档本地版本。后续编辑在 /etc/starbridge-site 下进行，保留原介质用于比对。

**基础设施管理员须先完成现场配置。** 参考 media/site-requirements.json（凭据名称、引用键、卷、运行时）和生成的 site-dependencies.json（按业务组列出的依赖）。原工程介质使用的网络、域名和存储配置带有源站点假设。逐组核实后部署基础设施或对接已有服务。

| 站点依赖 | 要准备和核验的事项 |
|---|---|
| 网络和存储 | 为节点、负载均衡及 DNS 分配目标站点地址，替换源域名、hostAliases、入口 IP 和节点名。基础 K3s 使用 Flannel；采用 Calico 的站点需在安装前确定 CNI。确认 NetworkPolicy 生效、Longhorn StorageClass 可绑定、工作卷可写 |
| CRD 和控制器 | 按 ops/argocd/infra 的依赖先后建立存储、证书、APISIX、数据库与沙箱控制器，等待 CRD Established 和相应控制器就绪。以 media/templates 和生成的本地镜像模板为离线来源。原 Argo CD Application 含联网 Git/Helm 地址；断网 GitOps 应先将提交和 chart 导入站内仓库，并将同步源切到站内 |
| 数据与身份 | 建立 PostgreSQL 平台库、迁移账号、LiteLLM 库、MinIO 存储桶与受限账户。配置 Authentik 的客户端、回调、issuer、JWKS、平台管理员组、TLS 和入口 DNS。恢复旧站时使用同一恢复点的身份、数据库、文件卷、密钥和余额账本 |
| 模型与执行 | 在 site.json 填模型 base URL、型号、站点 CA 和密钥文件路径。核对 LiteLLM 模型名、鉴权、额度分配、CodeAPI 签发密钥对、KVM 和 Kata。首次外部模型推理可能产生上游费用 |
| 计量与搜索 | 核对 OpenMeter 配置、数据库、消息总线、存储和供应商规则；个人搜索和共用搜索使用各自的服务与凭据。仅启用现场可达的能力。保留模拟支付配置，真实收款需另行配置和验收 |

```bash
sudo python3 media/check_site.py /etc/starbridge-site/site.json
sudo grep -Rl '__SITE_REQUIRED__' /etc/starbridge-site/v1
```

给模型密钥文件设置 0600。对当前平台模板，应核对 platform-api 的环境值和 openmeter-config 中的 template.yaml。可在解开的 ops-source.tar.gz 中查原部署结构以及 apps/platform-control/runtime/openmeter.yaml。站点私密值放在本地 Secret，避免写入提交。创建必要命名空间，包括 librechat、platform-system、codeapi、knowledge、search-system、codechat-builds，以及所选基础设施命名空间。针对全新站点，以下助手可以初始化聊天、MinIO 和 APISIX 三组凭据；其余凭据依 site-dependencies.json 配置。

```bash
sudo python3 media/init_site_secrets.py --target-uid "$SITE_UID"
sudo python3 media/init_site_secrets.py --target-uid "$SITE_UID" --apply
```

进行已有数据迁移时保留原凭据和加密密钥。创建新凭据后再挂老卷可能导致数据库连接或文件解密失败。

## 6. 部署与复核

基础设施和站点值就绪后，逐组运行。以下示例包含共用搜索；只用个人聊天搜索时可省掉 search-system 业务组，仍需建立清单引用的命名空间。source 版 GitOps 与离线命令应使用一致的镜像和站点值，以免 Argo CD 自动复原。

```bash
SITE=/etc/starbridge-site/v1
sudo python3 deploy-offline.py apply --site-dir "$SITE" --target-uid "$SITE_UID" \
  --group knowledge --group codeapi --group platform-control --group librechat --group search-system
sudo python3 deploy-offline.py verify --site-dir "$SITE" --target-uid "$SITE_UID" \
  --group knowledge --group codeapi --group platform-control --group librechat --group search-system
sudo k3s kubectl -n librechat get deployments,statefulsets,pods
```

每组在写入前检查必需 Secret、配置和本地镜像，执行服务器端 dry-run，再提交 Kubernetes 对象、等待迁移 Job 完成并检查 rollout。每组单独提交。后续组失败时，已完成组保留，可仅重跑失败组。遇到 field-manager 冲突先确认现有 GitOps 管理者；脚本不会强制取得对象字段所有权。PDF MCP、项目网页工具按账号与工作空间已有审批、版本和绑定启用。

## 7. 普通用户操作与验收

1. 打开站点配置的 HTTPS 聊天域名，使用普通账号登录。检查账号和剩余免费、充值 token；首次登录用户需已分配可用模型和额度。
2. 新建会话并发送一个简短问题，收到真实回复后刷新，确认会话仍在。对照账号用量记录与同一请求的模型回执。
3. 上传一份无敏感内容的样例资料，让助手生成 Word。打开回复中的文件卡片，在右侧预览并翻页，再下载 DOCX，用 Word 或 WPS 核对正文与表格。
4. 指定一段文字进行修改，预览并保存新版本，从个人文件重开。分别核对新版本和原件；只看到回答中的文件名不算保存成功。
5. 上传已知内容的扫描图片，核对一段正文、一格表格和一个数值，再导出为 Word。用普通账号验证另一账号无法访问该文件。
6. 需要联网搜索时，在具备网络及凭据的站点提问并打开引用。检查搜索关闭后的行为。在完全隔离站点记录哪些来源能在内网提供。
7. 在聊天菜单「本次对话的文件」重开报告，核对预览、下载和历史版本；管理员将请求记录、计量与模型回执对照。模拟充值可用于测试面板，不据此确认实际收款。

## 8. 故障、重试、回退

| 表现 | 检查与恢复 |
|---|---|
| 校验失败、磁盘满、断电 | 核对外层 sha256、磁盘空间和介质完整性。在同一目录使用同一批介质重跑 base 或 images |
| ImagePullBackOff / ErrImageNeverPull | 用 sudo k3s ctr -n k8s.io images list 检查 release.json 的本地运行名及摘要；重新执行 images，检查目标节点一致 |
| Forbidden、找不到 kind 或站点值 | 核对 UID、命名空间、CRD、准入控制器、Secret 键名和 site-dependencies.json。检查目标站点模板，修正后重试对应组 |
| Pod Pending、文档执行失败 | 查看 sudo k3s kubectl -n 命名空间 describe pod POD 和 get events --sort-by=.lastTimestamp；查 PVC、StorageClass、KVM、RuntimeClass、调度节点和资源配额 |
| 登录失败或模型不响应 | 核对外部域名、OIDC 回调、证书、模型地址、模型名称与额度。模型目录可访问后，再进行一次真实对话 |
| 预览可用但聊天没交付文件 | 确认 CodeAPI 调度、文件回执、签发身份、对象存储和持久卷；核对同一账号、会话、任务和版本，保留请求 ID |

升级前备份平台数据库、Mongo、对象存储、文件卷和加密材料，保存原站点配置及镜像映射。应用级回退使用上一批兼容的清单和镜像。数据库已迁移时须先确认旧程序兼容性；需要回退数据时在独立目标恢复同一备份点并核对，再决定切流。诊断可用 `sudo ./media/starbridge diagnose`；基础安装状态可用 `sudo python3 deploy-offline.py status`。

## 9. 开发维护与验收边界

从包中执行 `mkdir ops-src && tar -xzf ops-source.tar.gz -C ops-src` 可取得该次提交源码。修改聊天到 projects/office-chat，修改权限、计量、OCR 或 Word 到 projects/platform，执行沙箱到 projects/codeapi。发布新镜像后，固定摘要、同步 ops/apps 中对应工作负载，并重新生成当前版介质。离线目标机直接导入已构建镜像，无需 npm、pip 或 Docker Hub。知识服务的构建上下文另需固定版本的嵌入模型。构建并替换镜像前应通过该组件回归和真实入口验收。

本次打包核对正式运行引用、OCI 摘要、完整包内容和目标模板。历史断网虚拟机已验证基础 K3s 安装、重试、重启及实际 Word 转 PDF；本次新增的完整多服务流程尚未在另一台净机做网页登录到计费的端到端验收。现场使用第 7 节记录账号、软件版本、请求和结果，再决定开放入口。
