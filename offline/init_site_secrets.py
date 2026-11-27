"""Create the chat, object storage and gateway bootstrap credentials on a fresh local cluster."""
import argparse
import base64
import json
import secrets
import subprocess
from pathlib import Path

SECRETS={
    ("librechat","librechat-secrets"):{"mongo-password","MONGO_URI","CREDS_KEY","CREDS_IV","JWT_SECRET","JWT_REFRESH_SECRET","OPENID_SESSION_SECRET"},
    ("apisix","apisix-admin-auth"):{"admin","viewer"},
    ("minio","minio-root"):{"root-user","root-password"},
}
OWNER="offline.starbridge.local/site-uid"


def kube(*args,body=None):
    p=subprocess.run(["/usr/local/bin/k3s","kubectl","--kubeconfig=/etc/rancher/k3s/k3s.yaml",
                      "--server=https://127.0.0.1:6443","--request-timeout=15s",*args],
                     input=json.dumps(body) if body is not None else None,capture_output=True,text=True,timeout=25)
    if p.returncode:raise RuntimeError("本机集群操作失败 "+args[0])
    return json.loads(p.stdout) if p.stdout.strip() else None


def credentials(ns):
    if ns=="librechat":
        pw=secrets.token_hex(32)
        values={k:secrets.token_hex(16 if k=="CREDS_IV" else 32) for k in ("CREDS_KEY","CREDS_IV","JWT_SECRET","JWT_REFRESH_SECRET","OPENID_SESSION_SECRET")}
        values.update({"mongo-password":pw,"MONGO_URI":f"mongodb://root:{pw}@librechat-mongo:27017/LibreChat?authSource=admin"})
        return values
    if ns=="apisix":return {k:secrets.token_hex(32) for k in ("admin","viewer")}
    if ns=="minio":return {"root-user":"admin","root-password":secrets.token_hex(32)}
    raise ValueError("未知服务")


def initialize(uid,apply,call=kube):
    namespace=call("get","ns","kube-system","-o","json")
    if not uid or namespace["metadata"]["uid"]!=uid:raise ValueError("目标集群身份不符")
    absent=[]
    for (ns,name),fields in SECRETS.items():
        obj=call("get","secret",name,"-n",ns,"--ignore-not-found","-o","json")
        if not obj:
            if call("get","ns",ns,"--ignore-not-found","-o","json"):
                existing=call("get","pvc,deploy,statefulset","-n",ns,"-o","json")
                if existing and existing["items"]:raise ValueError("该空间已有业务数据或负载，拒绝生成替代密钥 "+ns)
            absent.append((ns,name));continue
        if obj["metadata"].get("labels",{}).get(OWNER)!=uid or fields-set(obj.get("data",{})):
            raise ValueError("已存在其他来源的凭据或原件不完整 "+ns+"/"+name)
    if not apply:return len(absent)
    for ns,name in absent:
        if not call("get","ns",ns,"--ignore-not-found","-o","json"):
            call("create","-f","-","-o","json",body=dict(apiVersion="v1",kind="Namespace",metadata=dict(name=ns)))
        obj=dict(apiVersion="v1",kind="Secret",metadata=dict(namespace=ns,name=name,labels={OWNER:uid}),
                 type="Opaque",data={k:base64.b64encode(v.encode()).decode() for k,v in credentials(ns).items()})
        call("create","-f","-","-o","json",body=obj)
    return len(absent)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--target-uid",required=True);parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    if not Path("/var/lib/starbridge-installer/state.json").is_file():raise SystemExit("未找到本机基础集群安装记录")
    print("新建站点待创建或本次已创建的凭据组数",initialize(args.target_uid,args.apply))
