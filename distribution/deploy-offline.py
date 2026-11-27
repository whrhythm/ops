"""Offline install and site deployment for the bundled single-node K3s release."""
import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
K3S = "/usr/local/bin/k3s"
KUBE = [K3S, "kubectl", "--kubeconfig=/etc/rancher/k3s/k3s.yaml", "--server=https://127.0.0.1:6443"]
APPS = {"knowledge":"knowledge", "codeapi":"codeapi", "platform-control":"platform-system",
        "librechat":"librechat", "search-system":"search-system"}


def read(path):
    return json.loads(path.read_bytes())


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(*args, capture=False):
    return subprocess.run(list(map(str,args)), check=True, stdout=subprocess.PIPE if capture else None,
                          text=True).stdout


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def prepare(media, out, hostname):
    if out.exists():
        raise ValueError("输出目录已存在。请保留站点修改并选择新目录。")
    release = read(ROOT/"release.json")
    refs = release["runtime"]
    old = read(media/"site-plan.json")["matched"]
    out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        stage = Path(tmp)/"prepared"
        run(sys.executable, media/"prepare_manifests.py", "--hostname",hostname,"--output",stage)
        for patch in read(ROOT/"updates.json"):
            path = stage/(patch["app"]+".json")
            docs = read(path)
            obj = next(x for x in docs["items"] if x["kind"]=="Deployment" and x["metadata"]["name"]==patch["workload"])
            container = next(c for c in obj["spec"]["template"]["spec"]["containers"] if c["name"]==patch["container"])
            if container["image"] != old[patch["previous"]]["runtimeRef"]:
                raise ValueError("介质基线不匹配 "+patch["workload"])
            container["image"] = refs[patch["image"]]
            env = {x["name"]:x for x in container.get("env",[])}
            env.update({x["name"]:x for x in patch["env"]})
            container["env"] = list(env.values())
            write(path,docs)
        for path in (ROOT/"templates").glob("*.json"):
            obj = read(path)
            for node in walk(obj):
                for c in node.get("containers",[]) + node.get("initContainers",[]):
                    c["image"] = refs[c["image"]]
                    c["imagePullPolicy"] = "Never"
            write(stage/path.name,obj)
        coverage = read(media/"coverage.json")
        aliases = dict(refs)
        for key, row in coverage["requiredReferences"].items():
            local = row.get("local") or {}
            if local.get("runtimeRef"):
                for name in [key, *row.get("pins", [])]:
                    aliases.setdefault(name,local["runtimeRef"])
        for name,row in coverage["cachedVerified"].items():
            aliases.setdefault(name,row["runtimeRef"])
        # Controllers also reference images in environment variables and text config.
        # Keep application-owned references intact if the release has no matching archive.
        import re
        image_ref = re.compile(r"harbor\.k3s\.local/[\w./:-]+@sha256:[a-f0-9]{64}")
        requirements={}
        for group,namespace in APPS.items():
            path=stage/(group+".json")
            doc=read(path)
            unresolved=set()
            def replace_image(m):
                name=m.group()
                if name not in aliases:unresolved.add(name)
                return aliases.get(name,name)
            doc=json.loads(image_ref.sub(replace_image,json.dumps(doc,ensure_ascii=False)))
            requirements[group]={"namespace":namespace,"references":[list(x) for x in requests(doc["items"])],
                                 "unarchivedInlineReferences":sorted(unresolved)}
            write(path,doc)
        write(stage/"site-dependencies.json",requirements)
        files = {p.name:checksum(p) for p in sorted(stage.glob("*.json")) if p.name not in ("manifest-index.json","site-dependencies.json")}
        write(stage/"manifest-index.json",dict(hostname=hostname,gitops=release["gitops"],files=files))
        stage.rename(out)
    print("已生成",len(files),"组模板。站点值与前置服务见 USER_GUIDE.md。")


def import_current():
    release = read(ROOT/"release.json")
    path = ROOT/"current.oci.tar"
    if checksum(path)!=release["archiveSHA256"]:
        raise ValueError("当前镜像补包摘要不符")
    ctr = [K3S,"ctr","-n","k8s.io","images"]
    def ready():
        rows = [r.split() for r in run(*ctr,"list",capture=True).splitlines()[1:]]
        have = {r[0]:r[2] for r in rows if len(r)>=3}
        return all(have.get(ref)=="sha256:"+ref.rsplit(":",1)[1] for ref in release["additional"].values())
    if not ready():
        run(*ctr,"import","--platform","linux/amd64",path)
    if not ready():
        raise ValueError("运行时的补包镜像摘要不符")


def target(uid):
    actual = run(*KUBE,"get","namespace","kube-system","-o","jsonpath={.metadata.uid}",capture=True)
    if not uid or uid!=actual:
        raise ValueError("目标集群 UID 不符")


def workloads(obj):
    return obj.get("spec",{}).get("template",{}).get("spec",{})


def requests(docs):
    needs = set()
    for obj in docs:
        spec = workloads(obj)
        for c in spec.get("containers",[])+spec.get("initContainers",[]):
            for entry in c.get("envFrom",[]):
                for key,kind in (("secretRef","secret"),("configMapRef","configmap")):
                    if key in entry and not entry[key].get("optional"): needs.add((kind,entry[key]["name"],""))
            for entry in c.get("env",[]):
                for key,kind in (("secretKeyRef","secret"),("configMapKeyRef","configmap")):
                    ref=entry.get("valueFrom",{}).get(key)
                    if ref and not ref.get("optional"): needs.add((kind,ref["name"],ref["key"]))
        for volume in spec.get("volumes",[]):
            for key,kind,name in (("secret","secret","secretName"),("configMap","configmap","name"),("persistentVolumeClaim","persistentvolumeclaim","claimName")):
                ref=volume.get(key)
                if ref and not ref.get("optional"):needs.add((kind,ref[name],""))
    return sorted(needs)


def rollout(group, directory, verify):
    ns = APPS[group]
    docs = read(directory/(group+".json"))["items"]
    identity = lambda ob: (ob["kind"].lower(),ob["metadata"]["name"])
    if not verify:
        if "__SITE_REQUIRED__" in json.dumps(docs):
            raise ValueError("请在 "+group+".json 填写站点值")
        defined={identity(x):x for x in docs}
        run(*KUBE,"get","namespace",ns,"-o","name")
        for kind,name,key in requests(docs):
            ob=defined.get((kind,name))
            if ob is None:
                query = [*KUBE,"-n",ns,"get",kind,name]
                # Extract only key names for credentials.
                if key:
                    keys=run(*query,"-o",'go-template={{range $k,$v := .data}}{{$k}}{{"\\n"}}{{end}}',capture=True).splitlines()
                    if key not in keys:raise ValueError(f"{ns}/{kind}/{name} 缺字段 {key}")
                else:run(*query,"-o","name",capture=True)
            elif key and key not in ob.get("data",{}) and key not in ob.get("stringData",{}):
                raise ValueError(f"{ns}/{kind}/{name} 缺字段 {key}")
        images={line.split()[0] for line in run(K3S,"ctr","-n","k8s.io","images","list",capture=True).splitlines()[1:] if line.split()}
        for obj in docs:
            if obj.get("metadata",{}).get("namespace",ns) not in {ns,"codechat-builds","search-system"} and obj["kind"]!="Namespace":
                raise ValueError("站点对象命名空间不匹配 "+str(identity(obj)))
            for node in walk(obj):
                for c in node.get("containers",[])+node.get("initContainers",[]):
                    if c["image"] not in images or c.get("imagePullPolicy")!="Never":
                        raise ValueError("目标机缺镜像或拉取策略有误 "+c["image"])
        # Schema/admission errors are reported before this group is changed.
        for obj in docs:
            scope=obj["metadata"].get("namespace",ns)
            subprocess.run([*KUBE,"-n",scope,"apply","--server-side","--field-manager=starbridge-offline",
                            "--dry-run=server","-f","-","-o","name"],input=json.dumps(obj),text=True,check=True)
        def order(obj):
            kind,name=identity(obj)
            if kind in ("deployment","statefulset","daemonset"):
                return 1 if name=="knowledge-postgres" else 3
            return 2 if kind=="job" else 0
        for obj in sorted(docs,key=order):
            # Server-side apply does not generate a last-applied copy of Secret data.
            subprocess.run([*KUBE,"-n",obj["metadata"].get("namespace",ns),"apply","--server-side","--field-manager=starbridge-offline","-f","-","-o","name"],
                           input=json.dumps(obj),text=True,check=True)
            if obj["kind"]=="Job":
                run(*KUBE,"-n",ns,"wait","--for=condition=complete","job/"+obj["metadata"]["name"],"--timeout=5m")
    for obj in docs:
        if obj["kind"] in ("Deployment","StatefulSet","DaemonSet") and obj.get("spec",{}).get("replicas",1)>0:
            run(*KUBE,"-n",ns,"rollout","status","/".join(identity(obj)),"--timeout=8m")


def word(media, uid):
    """Start the loopback-only renderer and check an actual, locally generated DOCX."""
    import base64
    import io
    import zipfile
    target(uid)
    ref = next(v for k,v in read(media/"coverage.json")["cachedVerified"].items() if "/office-word-pages@" in k)
    run(sys.executable,media/"import_images.py",ref["archive"])
    name="starbridge-word-offline"
    docs=[{"apiVersion":"v1","kind":"Namespace","metadata":{"name":name}},
          {"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":"renderer","namespace":name},
           "spec":{"replicas":1,"selector":{"matchLabels":{"app":"renderer"}},
                   "template":{"metadata":{"labels":{"app":"renderer"}},"spec":{
                       "automountServiceAccountToken":False,"securityContext":{"seccompProfile":{"type":"RuntimeDefault"}},
                       "containers":[{"name":"renderer","image":ref["runtimeRef"],"imagePullPolicy":"Never",
                                      "resources":{"requests":{"cpu":"50m","memory":"96Mi"},"limits":{"cpu":"1","memory":"768Mi"}},
                                      "securityContext":{"runAsNonRoot":True,"runAsUser":10002,"runAsGroup":10002,
                                                         "allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,"capabilities":{"drop":["ALL"]}},
                                      "volumeMounts":[{"name":"tmp","mountPath":"/tmp"}]}],
                       "volumes":[{"name":"tmp","emptyDir":{"sizeLimit":"192Mi"}}]}}}}]
    for doc in docs:
        subprocess.run([*KUBE,"apply","--server-side","--field-manager=starbridge-offline","-f","-","-o","name"],
                       input=json.dumps(doc),text=True,check=True)
    run(*KUBE,"-n",name,"rollout","status","deployment/renderer","--timeout=3m")
    sample=io.BytesIO()
    with zipfile.ZipFile(sample,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Starbridge offline verification</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')
    body=sample.getvalue()
    client="import sys,urllib.request; req=urllib.request.Request('http://127.0.0.1:8124/pages',data=sys.stdin.buffer.read());sys.stdout.buffer.write(urllib.request.urlopen(req,timeout=90).read())"
    raw=subprocess.run([*KUBE,"-n",name,"exec","-i","deploy/renderer","--","python3","-c",client],
                       input=body,stdout=subprocess.PIPE,check=True,timeout=120).stdout
    result=json.loads(raw)
    if result["sha256"]!=hashlib.sha256(body).hexdigest() or len(result["pages"])!=1 or not base64.b64decode(result["pages"][0],validate=True).startswith(b"\xff\xd8"):
        raise ValueError("DOCX 转页图读回失败")
    print("Word 服务已渲染并读回一页 JPEG，原件 SHA-256 已匹配。")

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--media",type=Path,default=ROOT/"media")
    commands=p.add_subparsers(dest="cmd",required=True)
    for n in ("check","base","images","status"):
        commands.add_parser(n)
    word_cmd=commands.add_parser("word")
    word_cmd.add_argument("--target-uid",required=True)
    prep=commands.add_parser("prepare")
    prep.add_argument("--out",type=Path,required=True)
    prep.add_argument("--hostname",default=socket.gethostname().lower())
    for n in ("apply","verify"):
        cmd=commands.add_parser(n)
        cmd.add_argument("--site-dir",type=Path,required=True)
        cmd.add_argument("--target-uid",required=True)
        cmd.add_argument("--group",choices=APPS,action="append",required=True)
    args=p.parse_args()
    media=args.media.resolve()
    binary=media/"starbridge"
    if args.cmd=="prepare":
        prepare(media,args.out,args.hostname)
    elif args.cmd=="check":
        run(binary,"check","--media-only")
        if checksum(ROOT/"current.oci.tar")!=read(ROOT/"release.json")["archiveSHA256"]:raise ValueError("补包摘要不符")
        print("基础介质与当前镜像补包校验通过")
    elif args.cmd=="base":
        run(binary,"install","--base-only")
        run(binary,"verify","--base-only")
    elif args.cmd=="images":
        run(sys.executable,media/"import_images.py","--all")
        import_current()
    elif args.cmd=="word":
        word(media,args.target_uid)
    elif args.cmd=="status":
        run(binary,"status")
    else:
        target(args.target_uid)
        for group in dict.fromkeys(args.group):
            rollout(group,args.site_dir,args.cmd=="verify")


if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,LookupError,subprocess.CalledProcessError) as exc:
        sys.exit("部署步骤失败: "+str(exc))
