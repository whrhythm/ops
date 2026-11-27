"""Publish and restore signed media on removable drives with a single-file size limit."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

BLOCK = 256 * 1024
LIMIT = 1024**3


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(BLOCK), b""):
            h.update(b)
    return h.hexdigest()


def publish(source, dest, limit=LIMIT):
    dest.mkdir(parents=True, exist_ok=True)
    data = dest / "parts"
    data.mkdir(exist_ok=True)
    release = json.loads((source / "release.lock").read_text(encoding="utf8"))
    members = [(r["path"], r["sha256"]) for r in release["files"]]
    members += [(p, sha(source / p)) for p in ("starbridge", "release.sig", "release.lock")]
    previous=dest/"volumes.json"
    prev={r["name"]:r for r in json.loads(previous.read_text(encoding="utf8"))["files"]} if previous.exists() else {}
    files = []
    for name, expected in members:
        original = source / name
        old=prev.get(name)
        if old and old["sha256"]==expected and old["parts"] and all(
                re.fullmatch(r"[0-9a-f]{64}",k) and (data/k).is_file() and (data/k).stat().st_size==n and sha(data/k)==k
                for k,n in old["parts"]):
            files.append(old)
            continue
        full = hashlib.sha256()
        chunks = []
        with original.open("rb") as src:
            while first := src.read(min(BLOCK, limit)):
                tmp = data / ".writing"
                h = hashlib.sha256()
                size = 0
                with tmp.open("wb") as out:
                    piece = first
                    while piece:
                        out.write(piece)
                        full.update(piece)
                        h.update(piece)
                        size += len(piece)
                        if size == limit: break
                        piece = src.read(min(BLOCK, limit - size))
                key = h.hexdigest()
                blob = data / key
                tmp.replace(blob)
                chunks.append([key, size])
        if full.hexdigest() != expected:
            raise ValueError("源文件在打包期间有变化 " + name)
        files.append(dict(name=name, sha256=expected, parts=chunks))
    manifest = dest / "volumes.json"
    tmp = dest / "volumes.json.partial"
    tmp.write_text(json.dumps(dict(format=1, files=files), indent=2), encoding="utf8")
    tmp.replace(manifest)
    shutil.copyfile(__file__, dest / "volumes.py")
    (dest/"SHA256SUMS").write_bytes((sha(manifest)+"  volumes.json\n"+sha(dest/"volumes.py")+"  volumes.py\n").encode("ascii"))
    dest.with_name(dest.name+".sha256").write_bytes((sha(manifest)+"  volumes.json\n").encode("ascii"))
    print("volumes.json SHA-256", sha(manifest), flush=True)


def receive(source, target, expected):
    manifest = source / "volumes.json"
    if sha(manifest) != expected: raise ValueError("索引摘要不符")
    doc = json.loads(manifest.read_text(encoding="utf8"))
    if doc.get("format") != 1: raise ValueError("未知格式")
    chunks = {}
    names = set()
    for row in doc["files"]:
        name = row["name"]
        rel = PurePosixPath(name)
        if (rel.is_absolute() or ".." in rel.parts or str(rel) != name or "\\" in name or ":" in name
                or name in names or not rel.parts or not re.fullmatch("[0-9a-f]{64}", row["sha256"])):
            raise ValueError("索引文件路径或摘要无效")
        names.add(name)
        for key, size in row["parts"]:
            if (not re.fullmatch("[0-9a-f]{64}", key) or not isinstance(size,int) or not 0<size<=LIMIT
                    or chunks.setdefault(key,size)!=size): raise ValueError("无效分包")
    bad=[]
    for key,size in chunks.items():
        p=source/"parts"/key
        if p.is_symlink() or not p.is_file() or p.stat().st_size!=size or sha(p)!=key:bad.append(key)
    if bad:raise ValueError("请重新复制下列分包\n"+"\n".join(bad))
    print("已校验",len(chunks),"个分包",flush=True)
    if not target:return
    if target.is_symlink():raise ValueError("目标不可为符号链接")
    target=target.resolve()
    if target==source.resolve() or source.resolve() in target.parents:raise ValueError("目标必须独立于分包目录")
    target.mkdir(parents=True,exist_ok=True)
    marker=target/".starbridge-volumes"
    if not marker.exists():
        if any(target.iterdir()):raise ValueError("目标目录已有其他文件")
        with marker.open("x",encoding="ascii") as f:f.write(expected)
    if marker.is_symlink() or marker.read_text(encoding="ascii")!=expected:raise ValueError("目标属于另一份发行包")
    import fcntl
    with marker.open("r") as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        todo=[]
        for row in doc["files"]:
            p=target/row["name"]
            if p.resolve()!=p:raise ValueError("目标包含符号链接")
            if not p.is_file() or sha(p)!=row["sha256"]:todo.append((p,row))
        need=sum(n for _,r in todo for _,n in r["parts"])
        if shutil.disk_usage(target).free<need:raise ValueError("目标可用磁盘不足")
        for p,row in todo:
            p.parent.mkdir(parents=True,exist_ok=True)
            tmp=p.with_name("."+p.name+".unpack-part")
            fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,"wb") as out:
                for key,_ in row["parts"]:
                    with (source/"parts"/key).open("rb") as part:shutil.copyfileobj(part,out,BLOCK)
                out.flush();os.fsync(out.fileno())
            if sha(tmp)!=row["sha256"]:raise ValueError("重组后摘要不符 "+row["name"])
            tmp.chmod(0o755 if p.name=="starbridge" or p.suffix==".sh" else 0o644)
            tmp.replace(p)
        print("介质已重组，本次写入文件",len(todo))


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("action",choices=["publish","verify","unpack"])
    ap.add_argument("directory",type=Path)
    ap.add_argument("--target",type=Path)
    ap.add_argument("--sha256")
    a=ap.parse_args()
    if a.action in ("publish","unpack") and a.target is None:ap.error("需要 --target")
    if a.action=="publish":publish(a.directory,a.target)
    else:
        if not a.sha256:ap.error("需要 --sha256 指定随交付提供的索引摘要")
        receive(a.directory,a.target if a.action=="unpack" else None,a.sha256)
