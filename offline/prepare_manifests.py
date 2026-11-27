"""Materialize target-local container references without contacting a registry or cluster."""
import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path


def prepare(media, out, hostname):
    if len(hostname)>63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",hostname):
        raise ValueError("无效的节点名称")
    root=json.loads((media/"coverage.json").read_text(encoding="utf8"))
    site=json.loads((media/"site-plan.json").read_text(encoding="utf8"))
    if site["unmatched"] or site.get("inlineUnmatched"):
        raise ValueError("部分镜像未归档，无法生成目标配置")
    aliases={k:v["runtimeRef"] for k,v in site["matched"].items()}
    for k,v in root["requiredReferences"].items():
        if v["scope"]=="platform-discovered" and v["local"]:
            for raw in [k]+v["pins"]:aliases[raw]=v["local"]["runtimeRef"]
    matcher=re.compile(r"(?<![\w./:-])(?:"+"|".join(re.escape(k) for k in sorted(aliases,key=len,reverse=True))+r")(?![\w.@/:-])")
    def visit(obj,infra):
        if isinstance(obj,list):return [visit(item,infra) for item in obj]
        if isinstance(obj,str):return matcher.sub(lambda m:aliases[m[0]],obj) if infra else obj
        if not isinstance(obj,dict):return obj
        result={k:visit(v,infra) for k,v in obj.items()}
        for key in ("containers","initContainers","ephemeralContainers"):
            if isinstance(obj.get(key),list):
                for source,container in zip(obj[key],result[key]):
                    if isinstance(source,dict) and isinstance(source.get("image"),str):
                        container["image"]=aliases[source["image"]]
                        container["imagePullPolicy"]="Never"
        for key in ("kubernetes.io/hostname","nodeName"):
            if obj.get(key)=="edge":result[key]=hostname
        return result
    if out.exists():raise ValueError("请选择新的输出目录")
    out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        stage=Path(tmp)/"site";stage.mkdir()
        hashes={}
        for form in json.loads((media/"render-report.json").read_text(encoding="utf8")):
            if not form.get("rendered"):raise ValueError("存在未渲染的配置 "+form["app"])
            obj=json.loads((media/form["file"]).read_text(encoding="utf8"))
            docs=[]
            for doc in obj["items"]:
                docs.append(doc if doc["kind"] in ("Secret","CustomResourceDefinition") else visit(doc,form["app"] in ("cert-manager","kata-deploy","longhorn")))
            obj["items"]=docs
            data=json.dumps(obj,ensure_ascii=False,indent=2).encode()
            name=form["app"]+".json"
            (stage/name).write_bytes(data);hashes[name]=hashlib.sha256(data).hexdigest()
        (stage/"manifest-index.json").write_text(json.dumps({"hostname":hostname,"files":hashes},indent=2))
        stage.rename(out)
    return len(hashes)


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--hostname",required=True);p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    print("已生成目标站点部署模板",prepare(Path(__file__).resolve().parent,args.output,args.hostname))
