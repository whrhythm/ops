# 星桥离线介质使用说明

适用目标为干净的 Ubuntu Server 22.04 amd64 单节点机器。K3s 版本为 v1.36.5+k3s1。本批基础安装使用 K3s 默认 Flannel 网络；现网为 Calico，业务迁移需要另外核对网络策略和地址规划。建议先在空机使用，保留原生产入口。

## 安装前

把整个目录复制到本机文件系统，例如 /opt/starbridge-media。Linux 下执行下列命令。若移动硬盘以 noexec 方式挂载，先复制到本机再执行。在可信交付渠道核对压缩包或可执行文件的 SHA-256，再做介质内签名校验。

```sh
cd /opt/starbridge-media
chmod +x starbridge
./starbridge check --media-only
sudo ./starbridge check --base-only --target-only
sudo ./starbridge check --base-only
```

`--media-only` 校验签名和清单所列文件。`--target-only` 仅检查这台主机。去掉 `--base-only` 可查看当前办公业务配置的 KVM 和存储依赖。完整业务尚未开放时，默认 install 会在写盘前退出。

## 安装、重试、核对

```sh
sudo ./starbridge install --base-only
sudo ./starbridge status
sudo ./starbridge verify --base-only
sudo ./starbridge diagnose
```

基础安装写入 /usr/local/bin/k3s、/etc/systemd/system/k3s.service、/var/lib/rancher/k3s 及 /var/lib/starbridge-installer，需要管理员权限。已有其他 K3s 安装或数据目录时会停止。遇到断电或中断，使用同一套介质重新执行 install。安装前后都会核对主机状态，完成标准包括节点 Ready 和 CoreDNS 可用。存储、计算和模型容量需按实际并发和数据规模验收。

介质如包含 system/Packages，可先通过 `sudo bash system/install-dependencies.sh --simulate` 查看本机需要哪些包。明确要安装时再使用 `--apply`。此操作使用随盘 Ubuntu 包索引，包含 open-iscsi、NFS 客户端、iptables 等依赖。

## 业务资料

source-inventory.json 为现场观察到的镜像和部署清单，包含历史项。site-requirements.json 记录工作负载引用的卷、Secret 名称及键名。coverage.json 区分匹配源配置的已归档业务镜像和待补项。按需任务和旧项目工具需要按归属分别核实。不得把安装盘中的所有历史镜像一并启动。

工程介质另附固定版本的部署包、脱敏的 templates 和 site-plan.json。后者按具体工作负载列出静态容器镜像匹配结果。控制器生成的数据库实例、个人文件空间、文件任务和存储驱动还要分别启动验证。模板中的 __SITE_REQUIRED__ 表示需在目标站点设置的值，请勿直接整目录 apply。

在工程介质通过签名校验后，可运行 `python3 prepare_manifests.py --hostname 目标节点名 --output /新目录`。它把普通工作负载指向本地已归档镜像，设置为禁止拉取，并转换明确指定源节点的选择器。原始模板保留。应用业务配置内的工具审批信息、凭据和证书不会经此步骤生成。

在已经用同一份工程介质安装好的独立演练机，可运行 `sudo python3 import_images.py --all` 导入业务镜像。导入器先校验签名和分包，根据本机容器运行时的实际摘要跳过已完成项。一次只导入一个分包可将相对路径替换 `--all`。首次启动 K3s 时已导入其自带的基础镜像。

新建站点需要独立的登录签名、内部服务凭据、存储与模型入口。迁移老站点另需同一恢复点的加密业务备份、原文件加密密钥与身份配置。两种操作的验收分别记录。基础演练包与业务工程介质使用不同的签名清单，请勿在同一台已安装的演练机上交替执行两套 install。

业务工程介质可在安装前单独检查现场连接。复制 site.example.json 到介质外，填入目标入口地址、聊天域名、账号域名和模型地址，运行 `python3 check_site.py /安全目录/site.json`。隔离模式检查模型地址是否解析到内网，关闭代理与自动重定向。密钥如需使用，填单独权限为 0600 的文件路径。模型目录检查不会产生推理扣费；模型目录未列出型号时仍需用实际对话单独验证。

全新站点如需初始化聊天数据库、对象存储和网关的凭据，可先从本地 K3s 读取 kube-system 命名空间 UID，运行 `sudo python3 init_site_secrets.py --target-uid <UID>` 检查，再追加 `--apply`。仅连接当前机器的回环地址；已有其他来源凭据或在缺少凭据的命名空间发现业务卷、工作负载时停止。重复执行保留已创建的凭据。迁移模式必须另行恢复原有密钥。

如含 backup_check.py，可使用 Python 3.10+ 和 cryptography 对既有 SBKP1 备份进行只读校验。软件包、业务密文和解密密钥分开交付。校验器不向目标实例写入数据。

```sh
python3 backup_check.py /mnt/backup/归档目录 --key-file /mnt/key/恢复密钥
```

此次断网验证覆盖 K3s 基础安装、故障续装、重启与正式 Word 预览镜像渲染报告。异机正式登录、模型聊天、用户文件与计费数据库恢复需要独立完成。云模型、外网搜索和公网邮箱需要对应网络，完全隔离的现场应配置内网替代服务。
