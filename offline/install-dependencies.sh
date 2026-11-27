#!/bin/bash
# Ubuntu 22.04. Only uses Packages and .deb files on the authenticated medium.
set -Eeuo pipefail
mode=${1:---simulate}
case "$mode" in --simulate|--apply) ;; *) echo "用法：install-dependencies.sh --simulate 或 --apply"; exit 2;; esac
media=$(cd "$(dirname "$0")/.." && pwd -P)
"$media/starbridge" check --media-only
[[ $(id -u) == 0 ]] || { echo "请以 root 运行 APT 预演或安装"; exit 1; }
case "$media" in *[[:space:]]*) echo "请将介质挂载到不含空格的路径"; exit 1;; esac
test -s "$media/system/Packages"
work=$(mktemp -d /tmp/starbridge-apt.XXXXXX)
chmod 755 "$work"
mkdir -p "$work/lists/partial" "$work/archives/partial"
echo "deb [trusted=yes] file:$media/system ./" >"$work/sources.list"
opts=(-o "Dir::Etc::sourcelist=$work/sources.list" -o "Dir::Etc::sourceparts=-" -o "Dir::State::lists=$work/lists" -o "Dir::Cache::archives=$work/archives" -o "Acquire::Retries=0" -o "DPkg::Lock::Timeout=25" -o "APT::Get::List-Cleanup=0")
timeout 2m apt-get "${opts[@]}" update
if [[ "$mode" == --simulate ]]; then opts+=(-s); else opts+=(-y); fi
DEBIAN_FRONTEND=noninteractive timeout 15m apt-get "${opts[@]}" --no-remove --no-install-recommends install open-iscsi nfs-common iptables kmod util-linux ca-certificates
