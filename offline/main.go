// Build for linux/amd64 with a pinned Ed25519 public key via -X main.trustedKey.
package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var trustedKey string

const stateDir = "/var/lib/starbridge-installer"
const canonicalK3s = "/usr/local/bin/k3s"
const archiveName = "k3s-airgap-images-amd64.tar.zst"
const archiveDir = "/var/lib/rancher/k3s/agent/images"
const unitPath = "/etc/systemd/system/k3s.service"
const unit = "[Unit]\nDescription=Starbridge offline K3s base\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=notify\nExecStart=/usr/local/bin/k3s server --disable=traefik --disable=servicelb --write-kubeconfig-mode=600\nKillMode=process\nDelegate=yes\nRestart=on-failure\nRestartSec=5\nLimitNOFILE=1048576\n[Install]\nWantedBy=multi-user.target\n"

type File struct {
	Path, SHA256 string
	Size         int64
}
type Release struct {
	Format          int
	ID, K3s         string
	Files           []File
	ProductBlocking []string
}
type Record struct {
	Medium, Step string
	Updated      time.Time
}
type Findings []string

func (f *Findings) add(s string, args ...any) { *f = append(*f, fmt.Sprintf(s, args...)) }
func digestFile(path string) (string, error) {
	r, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer r.Close()
	h := sha256.New()
	_, e = io.Copy(h, r)
	return hex.EncodeToString(h.Sum(nil)), e
}
func safe(root, rel string) (string, error) {
	if !filepath.IsLocal(rel) || filepath.Clean(rel) != rel {
		return "", fmt.Errorf("不安全的介质路径 %q", rel)
	}
	path := filepath.Join(root, rel)
	resolved, e := filepath.EvalSymlinks(path)
	if e != nil {
		return "", e
	}
	if resolved != path {
		return "", fmt.Errorf("介质文件为符号链接 %s", rel)
	}
	return path, nil
}
func readRelease(root string) (Release, string, error) {
	var r Release
	raw, e := os.ReadFile(filepath.Join(root, "release.lock"))
	if e != nil {
		return r, "", e
	}
	sig, e := os.ReadFile(filepath.Join(root, "release.sig"))
	if e != nil {
		return r, "", e
	}
	key, e := hex.DecodeString(trustedKey)
	if e != nil || len(key) != 32 || !ed25519.Verify(key, raw, sig) {
		return r, "", errors.New("介质签名无效")
	}
	if e = json.Unmarshal(raw, &r); e != nil {
		return r, "", e
	}
	if r.Format != 1 || r.ID == "" || r.K3s == "" || len(r.Files) == 0 {
		return r, "", errors.New("介质清单结构错误")
	}
	return r, fmt.Sprintf("%x", sha256.Sum256(raw)), nil
}
func audit(root string, r Release) Findings {
	var f Findings
	seen := map[string]bool{}
	for _, entry := range r.Files {
		if seen[entry.Path] || entry.Size <= 0 || len(entry.SHA256) != 64 {
			f.add("清单条目错误 %s", entry.Path)
			continue
		}
		seen[entry.Path] = true
		path, e := safe(root, entry.Path)
		if e != nil {
			f.add("%s: %v", entry.Path, e)
			continue
		}
		st, e := os.Stat(path)
		if e != nil || !st.Mode().IsRegular() || st.Size() != entry.Size {
			f.add("文件不完整 %s", entry.Path)
			continue
		}
		got, e := digestFile(path)
		if e != nil || got != entry.SHA256 {
			f.add("摘要不符 %s", entry.Path)
		}
	}
	for _, p := range []string{"system/k3s", "system/" + archiveName, "source-inventory.json"} {
		if !seen[p] {
			f.add("缺少必需条目 %s", p)
		}
	}
	return f
}
func loadRecord() (Record, error) {
	var s Record
	b, e := os.ReadFile(filepath.Join(stateDir, "state.json"))
	if e != nil {
		return s, e
	}
	e = json.Unmarshal(b, &s)
	return s, e
}
func exists(p string) bool { _, e := os.Lstat(p); return !errors.Is(e, os.ErrNotExist) }
func hostCheck(r Release, id string) Findings {
	var f Findings
	osinfo, _ := os.ReadFile("/etc/os-release")
	fields := map[string]string{}
	for _, ln := range strings.Split(string(osinfo), "\n") {
		k, v, ok := strings.Cut(ln, "=")
		if ok {
			fields[k] = strings.Trim(v, "\"")
		}
	}
	if runtime.GOARCH != "amd64" || fields["ID"] != "ubuntu" || fields["VERSION_ID"] != "22.04" {
		f.add("支持 Ubuntu 22.04 amd64，实际为 %s %s %s", fields["ID"], fields["VERSION_ID"], runtime.GOARCH)
	}
	for _, path := range []string{"/run/systemd/system", "/sys/fs/cgroup", "/proc/sys/net/ipv4/ip_forward"} {
		if !exists(path) {
			f.add("缺少主机能力 %s", path)
		}
	}
	for _, name := range []string{"systemctl", "modprobe", "mount", "iptables"} {
		if _, e := exec.LookPath(name); e != nil {
			f.add("缺少系统工具 %s", name)
		}
	}
	routes, _ := os.ReadFile("/proc/net/route")
	defaultRoute := false
	for _, row := range strings.Split(string(routes), "\n") {
		cols := strings.Fields(row)
		if len(cols) >= 4 && cols[1] == "00000000" && cols[0] != "lo" {
			defaultRoute = true
		}
	}
	if !defaultRoute {
		f.add("缺少默认 IPv4 路由；断网主机也需要指定本地默认路由以选择节点地址")
	}
	var sys syscall.Sysinfo_t
	if syscall.Sysinfo(&sys) != nil || uint64(sys.Totalram)*uint64(sys.Unit) < 2<<30 {
		f.add("基础集群需要至少 2 GiB 物理内存；业务容量另行验证")
	}
	var diskReserve = map[int32]uint64{}
	for path, need := range map[string]uint64{"/usr/local/bin": 256 << 20, "/var/lib": 4 << 30, "/etc": 1 << 20} {
		var st syscall.Statfs_t
		if e := syscall.Statfs(path, &st); e != nil {
			f.add("无法检查文件系统 %s: %v", path, e)
			continue
		}
		var dev syscall.Stat_t
		if e := syscall.Stat(path, &dev); e != nil {
			f.add("无法检查挂载点 %s", path)
			continue
		}
		mount := int32(dev.Dev)
		diskReserve[mount] += need
		if st.Bavail*uint64(st.Bsize) < diskReserve[mount] || st.Ffree < 4096 {
			f.add("基础安装空间或 inode 不足 %s，至少需 %d MiB", path, diskReserve[mount]>>20)
		}
	}
	s, e := loadRecord()
	if e == nil {
		if s.Medium != id {
			f.add("发现其他发行包的安装记录，拒绝覆盖")
		}
	} else if !errors.Is(e, os.ErrNotExist) {
		f.add("本机安装记录无法读取或损坏：%v", e)
	} else {
		for _, path := range []string{canonicalK3s, "/etc/rancher/k3s", "/var/lib/rancher/k3s", unitPath, "/etc/kubernetes/manifests"} {
			if exists(path) {
				f.add("检测到已有安装或数据 %s，禁止覆盖", path)
			}
		}
		for _, port := range []int{6443, 10250} {
			ln, e := net.Listen("tcp", fmt.Sprintf(":%d", port))
			if e != nil {
				f.add("端口 %d 无法使用: %v", port, e)
			} else {
				ln.Close()
			}
		}
	}
	return f
}
func atom(path string, r io.Reader, mode os.FileMode, want string) error {
	d := filepath.Dir(path)
	if e := os.MkdirAll(d, 0755); e != nil {
		return e
	}
	// installBase holds the process lock. Reuse the same bounded temporary slot
	// after power loss without following a symlink or touching the committed file.
	tmp := filepath.Join(d, "."+filepath.Base(path)+".starbridge-part")
	if info, err := os.Lstat(tmp); err == nil && !info.Mode().IsRegular() {
		return fmt.Errorf("临时路径不是普通文件 %s", tmp)
	}
	out, e := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return e
	}
	defer os.Remove(tmp)
	h := sha256.New()
	_, e = io.Copy(io.MultiWriter(out, h), r)
	if e == nil && want != "" && hex.EncodeToString(h.Sum(nil)) != want {
		e = errors.New("复制期间文件摘要发生变化")
	}
	if e == nil {
		e = out.Chmod(mode)
	}
	if e == nil {
		e = out.Sync()
	}
	ce := out.Close()
	if e == nil {
		e = ce
	}
	if e != nil {
		return e
	}
	if e = os.Rename(tmp, path); e != nil {
		return e
	}
	fd, e := os.Open(d)
	if e != nil {
		return e
	}
	defer fd.Close()
	return fd.Sync()
}
func mark(id, step string) error {
	b, e := json.Marshal(Record{id, step, time.Now().UTC()})
	if e != nil {
		return e
	}
	return atom(filepath.Join(stateDir, "state.json"), bytes.NewReader(b), 0600, "")
}
func run(timeout time.Duration, name string, args ...string) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	c := exec.CommandContext(ctx, name, args...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	e := c.Run()
	if ctx.Err() != nil {
		return fmt.Errorf("%s 超时: %w", name, ctx.Err())
	}
	return e
}
func lockInstall() (*os.File, error) {
	if e := os.MkdirAll(stateDir, 0700); e != nil {
		return nil, e
	}
	g, e := os.OpenFile(filepath.Join(stateDir, "lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(g.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		g.Close()
		return nil, errors.New("已有安装进程持锁")
	}
	return g, nil
}
func ready() error {
	if e := awaitResource("nodes"); e != nil {
		return e
	}
	if e := run(185*time.Second, canonicalK3s, "kubectl", "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s", "--request-timeout=182s"); e != nil {
		return e
	}
	if e := awaitResource("deployment/coredns", "-n", "kube-system"); e != nil {
		return e
	}
	return run(185*time.Second, canonicalK3s, "kubectl", "-n", "kube-system", "rollout", "status", "deployment/coredns", "--timeout=180s", "--request-timeout=182s")
}
func awaitResource(kind string, extra ...string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Second)
	defer cancel()
	args := append([]string{"kubectl", "get", kind, "--ignore-not-found", "-o", "name", "--request-timeout=8s"}, extra...)
	fmt.Println("等待资源注册", kind)
	for ctx.Err() == nil {
		out, err := exec.CommandContext(ctx, canonicalK3s, args...).Output()
		if err == nil && len(bytes.TrimSpace(out)) > 0 {
			return nil
		}
		select {
		case <-ctx.Done():
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("等待 %s 注册超时", kind)
}
func installBase(root string, r Release, id string) error {
	if os.Geteuid() != 0 {
		return errors.New("安装需 root 权限，check 可独立运行")
	}
	g, e := lockInstall()
	if e != nil {
		return e
	}
	defer g.Close()
	if f := hostCheck(r, id); len(f) > 0 {
		return errors.New(strings.Join(f, "\n"))
	}
	if e = mark(id, "copying"); e != nil {
		return e
	}
	for _, x := range r.Files {
		var dst string
		mode := os.FileMode(0644)
		switch x.Path {
		case "system/k3s":
			dst = canonicalK3s
			mode = 0755
		case "system/" + archiveName:
			dst = filepath.Join(archiveDir, archiveName)
		default:
			continue
		}
		if got, _ := digestFile(dst); got == x.SHA256 {
			fmt.Println("已核实", dst)
			continue
		}
		in, e := os.Open(filepath.Join(root, x.Path))
		if e != nil {
			return e
		}
		fmt.Println("复制并校验", x.Path)
		e = atom(dst, in, mode, x.SHA256)
		in.Close()
		if e != nil {
			return e
		}
	}
	if old, e := os.ReadFile(unitPath); e == nil && string(old) != unit {
		return errors.New("检测到不同的 k3s 服务配置，停止覆盖")
	}
	if e = atom(unitPath, strings.NewReader(unit), 0644, ""); e != nil {
		return e
	}
	if e = mark(id, "starting"); e != nil {
		return e
	}
	if e = run(30*time.Second, "systemctl", "daemon-reload"); e != nil {
		return e
	}
	if e = run(360*time.Second, "systemctl", "enable", "--now", "k3s.service"); e != nil {
		return e
	}
	if e = ready(); e != nil {
		return fmt.Errorf("节点未就绪，可运行 journalctl -u k3s -n 50：%w", e)
	}
	return mark(id, "base-ready")
}
func execute(args []string) error {
	if len(args) < 1 {
		return errors.New("用法：starbridge check|install|status|verify|diagnose [--base-only | --media-only]")
	}
	cmd := args[0]
	base, media, target, sourcePlan := false, false, false, false
	for _, a := range args[1:] {
		switch a {
		case "--base-only":
			base = true
		case "--media-only":
			media = true
		case "--target-only":
			target = true
		case "--source-plan":
			sourcePlan = true
		default:
			return fmt.Errorf("未知参数 %q", a)
		}
	}
	if (media || target) && cmd != "check" || base && media || target && media {
		return errors.New("参数组合无效")
	}
	exe, e := os.Executable()
	if e != nil {
		return e
	}
	exe, e = filepath.EvalSymlinks(exe)
	if e != nil {
		return e
	}
	root := filepath.Dir(exe)
	r, id, e := readRelease(root)
	if e != nil {
		return e
	}
	if sourcePlan {
		if cmd != "check" || base || media || target {
			return errors.New("源端导出清单参数组合无效")
		}
		need := map[string]bool{"coverage.json": true, "export-node-images.sh": true}
		for _, file := range r.Files {
			if !need[file.Path] {
				continue
			}
			path, err := safe(root, file.Path)
			if err != nil {
				return err
			}
			got, err := digestFile(path)
			if err != nil || got != file.SHA256 {
				return fmt.Errorf("导出清单校验失败 %s", file.Path)
			}
			delete(need, file.Path)
		}
		if len(need) > 0 {
			return errors.New("此介质缺少源端镜像导出清单")
		}
		fmt.Println("源端只读导出清单已验证")
		return nil
	}
	if cmd == "status" || cmd == "diagnose" {
		fmt.Println("介质", r.ID, "集群版本", r.K3s)
		rec, err := loadRecord()
		if err == nil {
			fmt.Println("本机阶段", rec.Step, rec.Updated)
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		} else {
			fmt.Println("本机尚未开始安装")
		}
		for _, s := range r.ProductBlocking {
			fmt.Println("业务交付待完成", s)
		}
		if cmd == "diagnose" {
			for _, s := range hostCheck(r, id) {
				fmt.Println("主机检查", s)
			}
		}
		return nil
	}
	if cmd != "check" && cmd != "install" && cmd != "verify" {
		return errors.New("未知命令 " + strconv.Quote(cmd))
	}
	var f Findings
	if !target {
		f = audit(root, r)
	}
	if !media {
		f = append(f, hostCheck(r, id)...)
		if !base {
			if !exists("/dev/kvm") {
				f.add("当前文件执行配置需要 /dev/kvm，请检查硬件虚拟化并在虚拟机中启用嵌套支持")
			}
			for _, bin := range []string{"iscsiadm", "mount.nfs", "systemctl"} {
				if _, e := exec.LookPath(bin); e != nil {
					f.add("业务存储依赖缺失 %s，先核验随盘 system/debs 软件包", bin)
				}
			}
			if !target {
				f = append(f, r.ProductBlocking...)
			}
		}
	}
	if len(f) > 0 {
		return errors.New(strings.Join(f, "\n"))
	}
	switch cmd {
	case "check":
		fmt.Println("指定范围检查通过", r.ID)
	case "install":
		if !base {
			return errors.New("此版仅提供基础集群安装，业务发行尚未验收")
		}
		return installBase(root, r, id)
	case "verify":
		if !base {
			return errors.New("完整业务验收尚未就绪")
		}
		s, e := loadRecord()
		if e != nil || s.Step != "base-ready" {
			return errors.New("尚未完成基础集群安装")
		}
		return ready()
	}
	return nil
}
func main() {
	if e := execute(os.Args[1:]); e != nil {
		fmt.Fprintln(os.Stderr, "未通过：", e)
		os.Exit(1)
	}
}
