"""Load verified business images into this machine's K3s; retry by comparing manifest digests."""
import argparse, fcntl, io, json, os, shutil, subprocess, tarfile
from pathlib import Path

CTR=["/usr/local/bin/k3s","ctr","-n","k8s.io","images"]
def imported():
    text=subprocess.check_output(CTR+["list"],text=True,timeout=30)
    return {f[0]:f[2] for line in text.splitlines()[1:] if len(f:=line.split())>=3}

def load_archive(path, rows):
    if path.name.endswith(".oci.tar"):
        subprocess.run(CTR+["import","--platform","linux/amd64",path],check=True,timeout=900)
        return
    proc=subprocess.Popen(["timeout","900",*CTR,"import","--platform","linux/amd64","-"],stdin=subprocess.PIPE)
    try:
        with tarfile.open(path,"r|gz") as src,tarfile.open(fileobj=proc.stdin,mode="w|") as dst:
            for m in src:
                if m.name.startswith("blobs/sha256/") and m.isfile() or m.name=="oci-layout":
                    dst.addfile(m,src.extractfile(m))
                elif m.name=="index.json":
                    # A save by immutable source digest can omit all runtime names.
                    # Use manifest sizes from the archive and carry only verified leaves.
                    entries={}
                    for row in rows:
                        if row.get("inlineManifest"):
                            payload=row["inlineManifest"].encode()
                            header=tarfile.TarInfo("blobs/sha256/"+row["manifest"][7:]);header.size=len(payload)
                            dst.addfile(header,io.BytesIO(payload))
                        desc=dict(digest=row["manifest"],size=row["manifestBytes"],mediaType=row["mediaType"])
                        desc["annotations"]={"io.containerd.image.name":row["runtimeRef"],"org.opencontainers.image.ref.name":row["runtimeRef"]}
                        entries[row["runtimeRef"]]=desc
                    raw=json.dumps(dict(schemaVersion=2,mediaType="application/vnd.oci.image.index.v1+json",manifests=list(entries.values()))).encode()
                    m.size=len(raw);dst.addfile(m,io.BytesIO(raw))
        proc.stdin.close()
        if proc.wait(timeout=300):raise RuntimeError("镜像导入失败")
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
def main():
    root=Path(__file__).resolve().parent
    p=argparse.ArgumentParser()
    choice=p.add_mutually_exclusive_group(required=True)
    choice.add_argument("archive",nargs="?",help="介质内的镜像分包路径");choice.add_argument("--all",action="store_true")
    a=p.parse_args()
    subprocess.run([root/"starbridge","check","--media-only"],check=True)
    release=root/"release.lock"
    signed={r["path"] for r in json.loads(release.read_text())["files"]}
    if "coverage.json" not in signed:raise ValueError("未签名的镜像清单")
    data=json.loads((root/"coverage.json").read_text())
    rows=list(data["cachedVerified"].values())
    for ref in data.get("requiredReferences",{}).values():
        row=ref["local"]
        if row and row.get("manifest"):rows.append(row)
    groups={}
    for row in rows:
        name=row["archive"]
        if name in signed:groups.setdefault(name,{})[row["runtimeRef"]]=row
    if not a.all and a.archive not in groups:raise ValueError("该分包没有已签名的镜像映射")
    if os.geteuid()!=0:raise RuntimeError("请在目标机以 root 执行")
    state=Path("/var/lib/starbridge-installer")
    if not (state/"state.json").exists():raise RuntimeError("请先安装本机基础集群")
    with (state/"image-import.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        known=imported()
        for name,items in sorted(groups.items()):
            if not a.all and a.archive!=name:continue
            if all(known.get(key)==row["manifest"] for key,row in items.items()):continue
            path=root/name
            if shutil.disk_usage("/var/lib/rancher/k3s").free < path.stat().st_size*2+512*1024**2:
                raise RuntimeError("镜像导入剩余空间不足 "+name)
            print("正在导入",name,flush=True)
            load_archive(path,list(items.values()))
            known=imported()
            if any(known.get(key)!=row["manifest"] for key,row in items.items()):
                raise RuntimeError("导入后镜像摘要不符 "+name)
        print("所选镜像分包已核对")
if __name__=="__main__":main()
