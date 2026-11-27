"""Release audit: reconcile archived static workloads with the observed source release."""
import argparse
import hashlib
import json
import tarfile
from pathlib import Path

def load(path):
    return json.loads(path.read_bytes())

def audit(dist,site,media,oci):
    release=load(dist/"release.json")
    observed=load(dist/"workloads.json")
    core={"librechat":"librechat","platform-system":"platform-control","codeapi":"codeapi","knowledge":"knowledge","search-system":"search-system"}
    docs={}
    for namespace,app in core.items():
        for obj in load(site/(app+".json"))["items"]:
            docs[namespace,obj["kind"],obj["metadata"]["name"]]=obj
    matched=0
    coredeploy=0
    repos=set()
    for w in observed:
        key=(w["namespace"],w["kind"],w["name"])
        if key not in docs:raise ValueError("missing workload "+str(key))
        current={c["name"]:c for c in docs[key]["spec"]["template"]["spec"]["containers"]}
        custom=False
        for name,ref in w["images"].items():
            if ref not in release["runtime"]:continue
            c=current[name]
            if c["image"]!=release["runtime"][ref] or c["imagePullPolicy"]!="Never":raise ValueError(str(key)+" "+name)
            matched+=1
            repo=ref.split("/")[-1].split("@")[0].split(":")[0]
            if repo in {"starbridge-office","platform-control","office-word-pages","starbridge-demo-tool","platform-knowledge","platform-meter"}:
                custom=True;repos.add(repo)
        if w["namespace"]!="search-system" and w["kind"]=="Deployment" and custom:coredeploy+=1
    coverage=load(media/"coverage.json")
    signed={x["path"] for x in load(media/"release.lock")["files"]}
    rows=list(coverage["cachedVerified"].values())+[x["local"] for x in coverage["requiredReferences"].values() if x.get("local")]
    available={x.get("runtimeRef") for x in rows if x.get("archive") in signed}
    with tarfile.open(oci) as t:
        def blob(d):
            h=hashlib.sha256()
            with t.extractfile("blobs/sha256/"+d["digest"].split(":")[1]) as f:
                for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
            if "sha256:"+h.hexdigest()!=d["digest"]:raise ValueError(d["digest"])
        checked=set()
        index=json.load(t.extractfile("index.json"))
        for d in index["manifests"]:
            blob(d)
            m=json.load(t.extractfile("blobs/sha256/"+d["digest"][7:]))
            for layer in [m["config"],*m["layers"]]:
                if layer["digest"] not in checked:blob(layer);checked.add(layer["digest"])
            available.add(d["annotations"]["io.containerd.image.name"])
    if set(release["runtime"].values())-available:raise ValueError("missing archived references")
    print(f"{len(observed)} observed workloads, {matched} registry container references, {coredeploy} custom core deployments, {len(repos)} core image repositories, {len(checked)} verified OCI blobs")
if __name__=="__main__":
    p=argparse.ArgumentParser()
    for x in ("dist","site","media","oci"):p.add_argument(x,type=Path)
    audit(**vars(p.parse_args()))
