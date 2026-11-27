"""Read-only preflight for site DNS and the model service. Uses only Python's standard library."""
import argparse
import ipaddress
import json
import os
import socket
import signal
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def check(site, directory):
    issues = []
    if site.get("network") not in ("isolated", "external-model"):
        issues.append("network 请选择 isolated 或 external-model")
    def resolve(name):
        if not name: raise ValueError("缺少主机名")
        return {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)}
    try:
        expected = {ipaddress.ip_address(ip) for ip in site["entryAddresses"]}
        if not expected: raise ValueError()
    except (KeyError, ValueError, TypeError):
        expected = set(); issues.append("请填写目标入口的内网 IP")
    for field in ("chatHost", "accountHost"):
        try:
            found = resolve(site.get(field))
            if not found.intersection(expected): issues.append(field+" 未解析到目标入口")
        except (ValueError, OSError):issues.append(field+" DNS 未就绪")
    try:
        base = site.get("modelBaseUrl", "")
        url = urllib.parse.urlsplit(base)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("需要有效的模型服务地址")
        ips=resolve(url.hostname)
        if site.get("network")=="isolated" and any(a.is_global for a in ips):
            raise ValueError("隔离模式的模型地址解析到了公网")
        headers={}
        key_file=site.get("modelKeyFile")
        if key_file:
            path=directory/key_file
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode&0o077:
                raise ValueError("模型密钥文件须仅本用户可读")
            if url.scheme!="https" and not all(a.is_loopback for a in ips):
                raise ValueError("带密钥的模型接口需要 HTTPS")
            headers["Authorization"]="Bearer "+path.read_text().strip()
        context=ssl.create_default_context(cafile=str(directory/site["caFile"]) if site.get("caFile") else None)
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=context))
        req=urllib.request.Request(base.rstrip("/")+"/models",headers=headers)
        with client.open(req,timeout=6) as r:
            body=r.read(2*1024**2+1)
        if len(body)>2*1024**2:raise ValueError("模型列表响应过大")
        models={m["id"] for m in json.loads(body).get("data",[]) if isinstance(m,dict) and "id" in m}
        model=site.get("model")
        if not model:raise ValueError("缺少要使用的模型名称")
        if model not in models:issues.append("模型列表未列出目标模型，需另做一次真实对话验证")
    except urllib.error.HTTPError as e:issues.append("模型列表接口返回 HTTP "+str(e.code))
    except (KeyError, AttributeError, OSError, ValueError) as e:
        issues.append("模型服务检查未通过 "+(str(e) if type(e) is ValueError else type(e).__name__))
    return issues


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("config",type=Path);a=p.parse_args()
    if hasattr(signal,"SIGALRM"):
        def expired(*args):raise SystemExit("现场连接检查 30 秒超时，请检查 DNS 或网络后重试")
        signal.signal(signal.SIGALRM,expired);signal.alarm(30)
    errors=check(json.loads(a.config.read_text(encoding="utf8")),a.config.resolve().parent)
    for error in errors:print(error)
    if errors:raise SystemExit(1)
    print("入口 DNS 和模型列表检查通过；尚未发送模型推理请求")
