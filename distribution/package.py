"""Assemble an offline tar from the signed medium and the Git delivery, without copying private workspace data."""
import argparse
import hashlib
import shutil
import json


import tarfile
import tempfile
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def add(tar, name, path, executable=False):
    info = tarfile.TarInfo("starbridge/" + name)
    info.size = path.stat().st_size
    info.mode = 0o755 if executable else 0o644
    with path.open("rb") as stream:
        tar.addfile(info, stream)


def stage(media, transfer, output):
    inventory = json.loads((transfer / "volumes.json").read_bytes())["files"]
    lock = json.loads((media / "release.lock").read_bytes())
    required = {x["path"]: x["sha256"] for x in lock["files"]}
    special = {"starbridge", "release.lock", "release.sig"}
    expected = {x["name"]: x["sha256"] for x in inventory}
    if set(expected) != set(required) | special or any(expected[k] != v for k,v in required.items()):
        raise ValueError("transfer index disagrees with release.lock")
    tmp = output.with_suffix(output.suffix + ".partial")
    if output.exists() or tmp.exists():
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary, tarfile.open(tmp, "x") as tar:
        for row in inventory:
            name = row["name"]
            if "\\" in name or name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(name)
            path = media / name
            if not path.is_file() or digest(path) != expected[name]:
                path = Path(temporary) / "member"
                with path.open("wb") as stream:
                    for key, size in row["parts"]:
                        part = transfer / "parts" / key
                        if part.stat().st_size != size or digest(part) != key:
                            raise ValueError("Bad transfer part for " + name)
                        with part.open("rb") as src:

                            shutil.copyfileobj(src, stream, 1024*1024)
                if digest(path) != expected[name]:
                    raise ValueError("Bad reconstructed file " + name)
            add(tar, "media/" + name, path, name in ("starbridge", "system/k3s") or name.endswith(".sh"))

    print("Media staged", len(expected), flush=True)


def finish(transfer, output, source, distribution, current):
    expected = {r["name"]:r["sha256"] for r in json.loads((transfer/"volumes.json").read_bytes())["files"]}
    tmp = output.with_suffix(output.suffix + ".partial")
    extras = {"ops-source.tar.gz": source, "current.oci.tar": current}
    for path in sorted(distribution.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.name != "package.py":
            extras[path.relative_to(distribution).as_posix()] = path
    with tarfile.open(tmp, "a") as tar:
        for name,path in extras.items():
            add(tar,name,path)
    with tarfile.open(tmp) as tar:
        for name, checksum in expected.items():
            with tar.extractfile("starbridge/media/" + name) as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest() != checksum:
                    raise ValueError("archive readback failed: " + name)
        for name,path in extras.items():
            with tar.extractfile("starbridge/" + name) as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest() != digest(path):
                    raise ValueError(name)
    checksum = digest(tmp)
    tmp.rename(output)
    output.with_suffix(output.suffix+".sha256").write_bytes((checksum+"  "+output.name+"\n").encode())
    print(len(expected), "verified media files;", output.stat().st_size, "bytes;", checksum, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    for name, fields in [("stage", ("media","transfer","output")), ("finish", ("transfer","output","source","distribution","current"))]:
        command=commands.add_parser(name)
        for field in fields: command.add_argument(field,type=Path)
    args=vars(parser.parse_args())
    action=args.pop("action")
    (stage if action=="stage" else finish)(**args)