"""pack_engine: hermes package 的黑名单打包引擎（纯 stdlib，路径匹配自带）。

规则语法（内置默认 + 用户 --exclude-file 追加，gitignore 风格子集）：
  /name/            锚定 home 根的目录（连子内容整棵剪枝）
  /a/b*.json        锚定根的相对路径 glob
  *.lock            任意深度的文件名 glob
  !<pattern>        否定规则（后匹配优先，可放行前面误杀的）
新增的插件/数据目录默认自动进包 —— 黑名单只杀列出的东西。
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import socket
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

# ── 内置黑名单（2026-10-31 实测盘点 hermes-home-sync-audit 的产物）──
# 设计原则：①可再生（源码树/环境/工具/缓存）②运行时残渣（pid/lock/tmp/心跳）
# ③过期快照与 .bak 全家桶。绝不含 *.db-wal / *.db-shm（必须随主库同迁）。
DEFAULT_EXCLUDES = [
    # 可再生大件（新机自动重建，~12GB 大头）
    "/hermes-agent/", "/installs/", "/tools/", "/cache/", "/lsp/", "/bin/",
    "/mnemosyne-venv/", "/gateway-service/",
    # 运行时状态目录（新进程自己长出来，搬走反而有害）
    "/logs/", "/telemetry/", "/source-checks/", "/plugin-update-checks/",
    "/session-hold/", "/terminal-sessions/", "/tui-pending-inputs/",
    "/pending_messages/", "/audio_cache/", "/image_cache/",
    "/composer-pastes/", "/state/", "/locks/", "/gateway/", "/kanban/",
    "/sandboxes/", "/desktop/", "/runtime/", "/profiles/",
    # 死物与过期快照
    "/state-snapshots/", "/backups/", "/.curator_backups/",
    # 运行时根级文件（锚定，避免误杀插件同名文件）
    "/gateway.pid", "/gateway.sock", "/gateway.lock", "/gateway-starts.log",
    "/gateway_state.json", "/processes.json",
    "/.restart_pending.json", "/.restart_last_processed.json",
    "/.update_check", "/.update_exit_code", "/.skip_upstream_prompt",
    "/.skills_prompt_snapshot.json", "/.hermes_history",
    "/.feishu_seen_message_ids*.tmp", "/feishu_seen_message_ids.json",
    "/models_dev_cache.json", "/models_dev_cache.etag",
    "/ollama_cloud_models_cache.json", "/provider_models_cache.json",
    "/web-ui-build-stamp.json", "/tui-theme-boot.json", "/.npm_lock_hash_*",
    "/.scope-recall-install-receipt.json", "/.backup.lock",
    # 任意深度：锁/临时/备份残渣
    "*.lock", "*.tmp", "*.bak", "*.bak-*", "*.orig", "*.part",
    "/SOUL.md.old.*", "/SOUL.md.original",
    "/sessions/request_dump_*.json",
]


class Blacklist:
    def __init__(self, rules):
        self.rules = []  # (kind, pattern, negate)
        for raw in rules:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            anchored = line.startswith("/")
            line = line.lstrip("/")
            if line.endswith("/"):
                kind, pat = ("dir", line.rstrip("/"))
                if anchored:
                    kind = "dir-anchored"
            elif "/" in line or anchored:
                # 带前导 / 即锚定根的精确路径规则（哪怕没有内部斜杠），
                # 不能降级成任意深度的文件名匹配
                kind, pat = ("anchored", line)
            else:
                kind, pat = ("base", line)
            self.rules.append((kind, pat, negate))

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """last-match-wins（gitignore 语义），返回是否排除。"""
        verdict = False
        for kind, pat, neg in self.rules:
            hit = False
            if kind == "dir":
                parts = rel.split("/")
                hit = parts[-1] == pat or pat in parts[:-1] or (is_dir and pat in parts)
            elif kind == "dir-anchored":
                hit = rel == pat or rel.startswith(pat + "/")
            elif kind == "anchored":
                hit = fnmatch.fnmatchcase(rel, pat)
            else:  # base：任意深度的文件名匹配
                base = rel.rsplit("/", 1)[-1]
                hit = fnmatch.fnmatchcase(base, pat)
            if hit:
                verdict = not neg
        return verdict


def resolve_home(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_dir():
            raise SystemExit(f"[hermes-pack] 目录不存在: {p}")
        return p
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve()
    except Exception as exc:
        env = os.environ.get("HERMES_HOME", "").strip()
        if env:
            return Path(env).resolve()
        raise SystemExit(f"[hermes-pack] 无法解析 HERMES_HOME（{exc}），请用 --home 指定")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True  # 查不到退出码就当作活着
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def gateway_running(home: Path) -> tuple[bool, str]:
    pidfile = home / "gateway.pid"
    if not pidfile.exists():
        return False, ""
    try:
        m = re.search(r"\d+", pidfile.read_text(encoding="utf-8", errors="replace"))
        pid = int(m.group()) if m else -1
    except OSError:
        return False, ""
    if _pid_alive(pid):
        return True, f"gateway (pid {pid})"
    return False, f"陈旧 pid 文件（pid {pid} 已退出）"


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def collect(home: Path, bl: Blacklist, out_path: Path):
    """遍历 home，黑名单剪枝。返回 (files, skipped_dirs, total_bytes)。"""
    files, skipped = [], []
    total = 0
    out_resolved = out_path.resolve() if out_path else None
    for dirpath, dirnames, filenames in os.walk(home, topdown=True, followlinks=False):
        d = Path(dirpath)
        keep_dirs = []
        for name in dirnames:
            sub = d / name
            if os.path.islink(sub) or getattr(os.path, "isjunction", lambda _: False)(sub):
                skipped.append(_rel(sub, home) + "/ (链接,跳过)")
                continue
            rel = _rel(sub, home)
            if bl.excluded(rel, True):
                skipped.append(rel + "/")
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in filenames:
            f = d / name
            rel = _rel(f, home)
            if bl.excluded(rel, False):
                continue
            if out_resolved is not None:
                try:
                    if f.resolve() == out_resolved:
                        continue  # 不吃掉自己
                except OSError:
                    pass
            try:
                total += f.stat().st_size
                files.append(f)
            except OSError:
                skipped.append(rel + " (stat 失败)")
    return files, skipped, total


def package_main(args) -> int:
    home = resolve_home(args.home)
    rules = [] if args.no_default_excludes else list(DEFAULT_EXCLUDES)
    if args.exclude_file:
        user = Path(args.exclude_file).expanduser().resolve()
        if not user.is_file():
            print(f"[hermes-pack] 黑名单文件不存在: {user}")
            return 2
        rules.extend(user.read_text(encoding="utf-8", errors="replace").splitlines())
        print(f"[hermes-pack] 已加载用户黑名单 {user.name}（{len(user.read_text(encoding='utf-8', errors='replace').splitlines())} 行）")
    bl = Blacklist(rules)

    out_path = Path(args.output).expanduser()
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    if out_path.suffix.lower() != ".zip":
        out_path = out_path.with_suffix(".zip")
    if out_path.exists() and not args.dry_run:
        print(f"[hermes-pack] 目标已存在，拒绝覆盖: {out_path}（改名或先删）")
        return 2

    running, why = gateway_running(home)
    if running and not args.force and not args.dry_run:
        print(f"[hermes-pack] 中止：{why} 正在运行——热拷 SQLite 会撕裂。")
        print("             请先停 gateway/桌面端，或加 --force 自担风险。")
        return 3
    if running and (args.force or args.dry_run):
        print(f"[hermes-pack] 注意：{why} 在跑{'（--force，WAL 一致性风险自负）' if args.force and not args.dry_run else '（dry-run 只读，安全）'}。")
    elif why:
        print(f"[hermes-pack] 提示：{why}，忽略。")

    t0 = time.time()
    files, skipped, total_bytes = collect(home, bl, out_path if not args.dry_run else None)
    scan_s = time.time() - t0

    print(f"[hermes-pack] home   = {home}")
    print(f"[hermes-pack] 收录   = {len(files)} 个文件 / {total_bytes/1073741824:.2f} GB（打包前原始大小）")
    dirs_skipped = sorted(s for s in skipped if s.endswith("/") and " " not in s.rstrip("/"))
    if dirs_skipped:
        print(f"[hermes-pack] 黑名单剪掉 {len(dirs_skipped)} 个目录:")
        for s in dirs_skipped[:25]:
            print(f"    - {s}")
        if len(dirs_skipped) > 25:
            print(f"    ...+{len(dirs_skipped)-25}")

    if args.dry_run:
        print("[hermes-pack] dry-run 结束，未写任何文件。")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = zipfile.ZIP_STORED if args.store else zipfile.ZIP_DEFLATED
    manifest = {
        "tool": "hermes-pack", "pack_version": 1,
        "source_home": str(home), "hostname": socket.gethostname(),
        "os": os.name, "python": sys.version.split()[0],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "file_count": len(files), "raw_bytes": total_bytes,
        "gateway_was_running": bool(running),
    }
    written = 0
    vanished = 0
    t1 = time.time()
    with zipfile.ZipFile(out_path, "w", mode, allowZip64=True, compresslevel=6 if mode == zipfile.ZIP_DEFLATED else None) as zf:
        zf.writestr("_hermes-pack-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for f in files:
            try:
                zf.write(f, _rel(f, home))
            except (FileNotFoundError, zipfile.BadZipFile) as exc:
                # 扫描与写入之间文件消失/被重写（SQLite -wal/-shm checkpoint、临时件轮换）。
                # 旁文件本应随主库走，主库在包里，个别 sidecar 蒸发不构成损坏——计数继续。
                vanished += 1
                if vanished <= 10:
                    print(f"  [跳过] {f.name}: {type(exc).__name__}（竞态蒸发）")
                continue
            except OSError as exc:
                # 占用/权限/尺寸中途变化：同样计数跳过，不拖垮整包
                vanished += 1
                if vanished <= 10:
                    print(f"  [跳过] {f.name}: {exc}")
                continue
            written += 1
            if written % 2000 == 0:
                pct = written / max(1, len(files)) * 100
                print(f"  ... {written}/{len(files)} ({pct:.0f}%) {f.name}")
    if vanished:
        print(f"[hermes-pack] 竞态跳过 {vanished} 个文件（gateway 在跑时属预期；正式打包请先停服务）")
    zip_bytes = out_path.stat().st_size
    dur = time.time() - t1
    ratio = zip_bytes / max(1, total_bytes) * 100
    print(f"[hermes-pack] ✔ {out_path}")
    print(f"[hermes-pack]   {written+1} 条目 / {zip_bytes/1073741824:.2f} GB（压缩率 {ratio:.0f}%），扫描 {scan_s:.0f}s + 写入 {dur:.0f}s")
    print(f"[hermes-pack]   恢复端：解压到目标机 HERMES_HOME 后先跑 environment-migration-remap（.env/config.yaml 绝对路径重映射），再启动 Hermes。")
    return 0
