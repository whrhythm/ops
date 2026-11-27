# 星桥源码与部署

产品源码在 [projects](projects/)，正式 Kubernetes 配置在 [apps](apps/)，Argo CD 入口在 [argocd](argocd/)。[用户与离线部署指南](USER_GUIDE.md) 包含项目清单、目标机要求、断网安装、用户操作、验收与故障处理。[部署脚本](distribution/deploy-offline.py) 读取交付包中的签名介质和当前版镜像补包。
