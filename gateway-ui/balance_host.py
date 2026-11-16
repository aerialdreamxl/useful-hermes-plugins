# -*- coding: utf-8 -*-
"""balance_host —— 宿主侧余额查询/校验/快照（CONTRACT §10，余额气泡 BalanceBubble）。

架构（额度纪律 + Token Plan 实测 18-28s）：
- worker 点气泡零网络调用，只读宿主刷好的 balance_view.json；
- 本模块周期拉取（默认 300s，钳 60-3600）+ 手动刷新请求（节流 10s、幂等 64 id）；
- binding 在宿主 python -I 子进程隔离执行（runpy run_name='_binding_' 不触发
  binding 的 __main__ 双打印），崩溃/超时不拖垮其余 provider；
- SLOW=True 源（qianwen CLI 类）单独计时 + 后台线程慢拉，不阻塞快源周期；
- 密钥纪律：error/display/extra 一律 sk- 样式打码后才入 view/log；
- enabled=false 或投放目录空 → 不写 view 不拉取（线程可起但空转）。

纯 stdlib。BalanceWatcher 构造全注入（bus_dir/providers_dir/get_cfg/python_exe），
测试不碰真实总线/config/网络。接线（收口接线）：register 起线程 run(stop)，
consumer 每拍可再调 write_view()（内容无变不空写）。
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量 ----
TIMEOUT_FAST_S = 10.0       # 普通 binding 超时钳
TIMEOUT_SLOW_S = 45.0       # SLOW=True（qianwen CLI 类，binding 内部自设更长）
MANUAL_THROTTLE_S = 10.0    # 手动刷新同 provider 节流（防连点烧钱）
REQ_POLL_S = 0.25           # balance_refresh_req.json 轮询拍
ID_KEEP = 64                # 幂等：最近 64 个已处理请求 id
ERROR_MAX = 120             # error 串截断（view 渲染再截 24，日志留长一点）
LOG_MAX_BYTES = 400_000
LOG_KEEP_LINES = 2000
WRITE_RETRIES = 4           # 原子写 Windows 共享冲突重试（读侧瞬时打开目标文件）

_SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")
_HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 子进程环境 ----
# F26：binding 子进程不再继承全量 os.environ。providers_dir 里的 *.py 是"可投放
# 代码"（作者/任何能写该目录的人写的都能跑），全量继承=把网关密钥、云凭证、代理
# 令牌一并交出去。只给进程启动/编码必需项 + Hermes 自有命名空间。
_ENV_KEEP = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PYTHONUTF8", "PYTHONIOENCODING",
)
# 出厂 binding 实测需要的（读 balance-providers/*.py 逐条核对，别删）：
#   HERMES_HOME            deepseek.py 定位 <home>/.env（凭证后缀来源）
#   HERMES_QIANWEN_PATH    等 HERMES_<CLI>_PATH 覆盖 CLI 路径（bailian_*.py）
#   HERMES_BALANCE_*       token_plan_edition 等显式注入（extra_env，见 _fetch_one）
#   DEEPSEEK_API_KEY       deepseek.py 进程环境优先的凭证（缺则回落 <home>/.env）
_ENV_KEEP_KEYS = ("DEEPSEEK_API_KEY",)
_ENV_KEEP_PREFIX = "HERMES_"
DRAIN_TIMEOUT_S = 2.0       # 超时杀树后的管道收尾上限（不与 sleep 型测试的 <5s 冲突）

# runner 注入：runpy 以 run_name='_binding_' 执行 binding——不触发其
# if __name__=="__main__" 分支（避免双打印），直接取模块级 fetch() 结果。
_RUNNER_CODE = (
    "import runpy,json,sys;"
    "m=runpy.run_path(sys.argv[1],run_name='_binding_');"
    "f=m.get('fetch');"
    "print(json.dumps(f() if callable(f) else "
    "{'ok':False,'error':'binding missing fetch()'},ensure_ascii=False))"
)


def mask_secrets(text) -> str:
    """sk- 样式密钥打码（view 与日志都过这道）。"""
    try:
        return _SK_RE.sub("sk-***", str(text))
    except Exception:
        return "sk-***"


def _short(msg, limit=ERROR_MAX) -> str:
    s = mask_secrets(msg).replace("\r", " ").replace("\n", " ").strip()
    return s[:limit]


def _clamp_refresh(v, default=300) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = default
    return max(60, min(3600, n))


# ---------------------------------------------------------------- 发现 ----
def _const_from_node(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def discover_providers(directory) -> list:
    """扫 *.py（跳过 _ 开头），ast 静态解析 NAME/UNIT/ID/SLOW 常量（绝不 exec）。
    ID 缺省文件名 stem；同 ID 去重后者胜并记警告（返回 meta.warnings）。
    返回 [{"id","name","unit","slow","path","warnings":[str]}]，按文件名排序。"""
    out = []
    try:
        d = Path(directory)
        files = sorted(p for p in d.glob("*.py")
                       if p.is_file() and not p.name.startswith("_"))
    except Exception:
        return out
    by_id = {}
    order = []
    for p in files:
        meta = {"id": p.stem, "name": p.stem, "unit": "", "slow": False,
                "path": str(p), "warnings": []}
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            meta["warnings"].append(f"parse failed: {_short(exc, 60)}")
            tree = None
        if tree is not None:
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                val = _const_from_node(node.value)
                if val is None:
                    continue
                for tgt in node.targets:
                    if not isinstance(tgt, ast.Name):
                        continue
                    if tgt.id == "NAME" and isinstance(val, str):
                        meta["name"] = val
                    elif tgt.id == "UNIT" and isinstance(val, str):
                        meta["unit"] = val
                    elif tgt.id == "ID" and isinstance(val, str) and val:
                        meta["id"] = val
                    elif tgt.id == "SLOW":
                        meta["slow"] = bool(val)
        pid = meta["id"]
        if pid in by_id:
            w = f"duplicate id {pid!r}: {Path(by_id[pid]['path']).name} replaced by {p.name}"
            meta["warnings"].append(w)
            by_id[pid] = meta
        else:
            by_id[pid] = meta
            order.append(pid)
    for pid in order:
        out.append(by_id[pid])
    return out


# ---------------------------------------------------------------- 执行 ----
def _child_env(extra_env=None) -> dict:
    """binding 子进程环境（F26 白名单，见模块顶部 _ENV_KEEP 注释）。
    extra_env 是宿主显式注入的（HERMES_BALANCE_TOKEN_PLAN_EDITION 等），始终覆盖。"""
    env = {k: v for k, v in os.environ.items()
           if k in _ENV_KEEP or k in _ENV_KEEP_KEYS
           or k.startswith(_ENV_KEEP_PREFIX)}
    env["PYTHONIOENCODING"] = "utf-8"
    if isinstance(extra_env, dict):
        env.update({str(k): str(v) for k, v in extra_env.items()})
    return env


def _kill_tree(pid) -> None:
    """N23：超时后杀整棵进程树。subprocess 的 timeout 只 kill 直系子进程；binding
    经 shell 包装 CLI 时（bailian_* → cmd /c qianwen → node），孙进程会脱管泄漏。
    Windows 用 taskkill /F /T（静默、5s 上限）；其它平台交给 Popen.kill() 兜底。"""
    try:
        if os.name != "nt" or not pid:
            return
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
    except Exception:
        pass


def run_provider(path, timeout=None, python_exe=None, slow=False, log=None, **kwargs) -> dict:
    """宿主 python -I -X utf8=1 子进程隔离跑 binding。
    超时钳：普通 10s / SLOW 45s（显式 timeout 参数可覆盖，测试用）。
    stdout 取最后一行合法 JSON dict；失败/超时/非 dict → {ok:False,error:短消息}。
    error 一律打码；stderr 脱敏后进日志（log 回调，缺省丢弃）。"""
    limit = float(timeout if timeout is not None
                  else (TIMEOUT_SLOW_S if slow else TIMEOUT_FAST_S))
    py = str(python_exe or sys.executable)
    cmd = [py, "-I", "-X", "utf8=1", "-c", _RUNNER_CODE, str(path)]
    # 余额气泡修订：binding 自定义配置经环境变量注入（宿主 get_cfg 读 config，
    # binding 子进程只读 env，不碰 config.yaml——worker/binding 零配置纪律）。
    env = _child_env(kwargs.get("extra_env"))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                env=env)
    except Exception as exc:
        return {"ok": False, "error": _short(f"spawn failed: {exc}")}
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        # N23：先杀整棵树（Windows taskkill /T），再收管道；收尾限时不拖慢调用方。
        _kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.kill()
        try:
            proc.communicate(timeout=DRAIN_TIMEOUT_S)
        except Exception:
            with contextlib.suppress(Exception):
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
        return {"ok": False, "error": f"timeout {limit:g}s"}
    out = out or ""
    err = err or ""
    rc = proc.returncode
    if log is not None and err.strip():
        try:
            log(f"{Path(path).name} stderr: {_short(err.strip(), 300)}")
        except Exception:
            pass
    result = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            result = obj
            break
    if result is None:
        # 崩溃取证：优先取 stderr 最后的异常行（含错误消息），而非尾部文件路径堆栈。
        err_lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        exc_line = ""
        for ln in reversed(err_lines):
            if "Error" in ln or "Exception" in ln:
                exc_line = ln
                break
        detail = exc_line or (err or out or "").strip()[-200:]
        return {"ok": False,
                "error": _short(f"binding failed (rc={rc}): {detail}")}
    if not isinstance(result.get("ok"), bool):
        result["ok"] = False
    result["error"] = _short(result.get("error") or "")
    return result


# ---------------------------------------------------------------- 峰谷 ----
_PEAK_MOD = None
_PEAK_LOCK = threading.Lock()


def _peak_rules():
    """importlib 加载 worker/peak_rules.py —— 单一事实源（宿主/worker 同一份）。"""
    global _PEAK_MOD
    with _PEAK_LOCK:
        if _PEAK_MOD is None:
            p = _HERE / "worker" / "peak_rules.py"
            spec = importlib.util.spec_from_file_location("gateway_ui_peak_rules", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PEAK_MOD = mod
        return _PEAK_MOD


def _peak_view(peak, now):
    """normalize binding 交的 peak 块（CONTRACT §10.1/10.3）：
    - None/非 dict → None（无峰谷概念，气泡整行不渲染）；
    - rule/windows/holidays 原样透传（worker 跨边界推进用同一份规则）；
    - 优先级 next_boundary_ts > windows > rule；binding 只交规则没交现值时
      宿主用 peak_rules 算好 next_boundary_ts 再写 view。"""
    if not isinstance(peak, dict):
        return None
    out = {}
    rule = peak.get("rule")
    windows = peak.get("windows")
    holidays = peak.get("holidays")
    if isinstance(rule, str) and rule:
        out["rule"] = rule
    if isinstance(windows, list) and windows:
        out["windows"] = windows
    if isinstance(holidays, list) and holidays:
        out["holidays"] = [str(h) for h in holidays]
    pr = _peak_rules()
    is_peak = peak.get("is_peak")
    if isinstance(is_peak, bool):
        out["is_peak"] = is_peak
    else:
        out["is_peak"] = bool(pr.is_peak_at(now, rule=rule, windows=windows,
                                            holidays=holidays))
    nb = peak.get("next_boundary_ts")
    try:
        nb = float(nb)
    except (TypeError, ValueError):
        nb = None
    if nb is None or nb <= now:
        nb = float(pr.next_boundary(now, rule, windows, holidays))
    out["next_boundary_ts"] = round(nb, 3)
    lp, lv = peak.get("label_peak"), peak.get("label_valley")
    out["label_peak"] = lp if isinstance(lp, str) and lp else "峰"
    out["label_valley"] = lv if isinstance(lv, str) and lv else "谷"
    out["server_authoritative"] = bool(peak.get("server_authoritative", False))
    return out


def normalize(binding_result, meta, now=None) -> dict:
    """binding fetch() 结果 → view provider 条目（CONTRACT §10.1 字段）。
    绝不造数据：接口没有的字段（value/extra）缺就缺。"""
    now = float(time.time() if now is None else now)
    r = binding_result if isinstance(binding_result, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    ok = bool(r.get("ok", False))
    value = r.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = None
    else:
        value = float(value)
    display = r.get("display")
    display = mask_secrets(display) if isinstance(display, str) else ""
    extra = r.get("extra")
    if isinstance(extra, dict):
        extra = {str(k): mask_secrets(v) for k, v in extra.items()
                 if isinstance(v, str)}
    else:
        extra = {}
    entry = {
        "id": str(meta.get("id") or ""),
        "name": str(meta.get("name") or meta.get("id") or ""),
        "unit": str(meta.get("unit") or ""),
        "ok": ok,
        "error": _short(r.get("error") or "") if not ok else "",
        "value": value,
        "display": display,
        "peak": _peak_view(r.get("peak"), now) if ok else None,
        "fetched_ts": round(now, 3),
    }
    if extra:
        entry["extra"] = extra
    # 余额气泡修订：结构化多行渲染（lines=[{text,style}]，style: title|amount|info|bar）。
    # 只透传 dict 且 text 为串的条目（打码），其余丢弃；气泡见 lines 走专用排版。
    # bar 行额外透传 fraction（0~1 钳位），供气泡画进度条。
    lines = r.get("lines")
    if isinstance(lines, list):
        clean = []
        for ln in lines:
            if isinstance(ln, dict) and isinstance(ln.get("text"), str):
                item = {"text": mask_secrets(ln["text"]),
                        "style": str(ln.get("style") or "info")}
                if item["style"] == "bar":
                    try:
                        frac = float(ln.get("fraction"))
                    except (TypeError, ValueError):
                        frac = 0.0
                    item["fraction"] = max(0.0, min(1.0, frac))
                clean.append(item)
        if clean:
            entry["lines"] = clean
    return entry


# ---------------------------------------------------------------- 原子写 ----
def _tmp_sibling(path: Path) -> Path:
    """同目录唯一临时名（pid + 线程 id）：write_view 会被宿主消费线程与 watcher
    线程并发调用（N18），固定 ".tmp" 名会互相踩（一方 rename 走另一方正在写的
    文件 → FileNotFoundError 或半文件）。"""
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _write_json_atomic(path: Path, obj) -> None:
    """tmp+rename 原子写（临时名带 pid+线程 id，N18）。
    Windows 上若目标文件正被读侧打开（Python open 不带 FILE_SHARE_DELETE），
    MoveFileEx 报 WinError 5——读侧只开几微秒，短退避重试即可，别丢整拍；
    重试用尽仍失败才抛，失败路径必删 tmp（绝不攒 .tmp 垃圾）。"""
    tmp = _tmp_sibling(path)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    try:
        for attempt in range(WRITE_RETRIES):
            try:
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt + 1 >= WRITE_RETRIES:
                    raise
                time.sleep(0.005 * (attempt + 1))
    finally:
        with contextlib.suppress(Exception):
            if tmp.exists():
                tmp.unlink()


# ---------------------------------------------------------------- watcher ----
class BalanceWatcher:
    """周期拉全部 provider + 消费手动刷新请求 + 写 balance_view.json。

    构造（全注入，便于测试）：
      bus_dir       总线目录（balance_view.json / balance_refresh_req.json 落这里）
      providers_dir 投放目录（""=不解析，get_cfg 回调可动态改）
      get_cfg       读 balance 配置回调 → {"enabled","refresh_s","providers_dir"}
      python_exe    跑 binding 的宿主 python（缺省 sys.executable）
      logger        可选，.info(msg)
      now_fn        可选时钟（测试假时钟；缺省 time.time）
      run_fn        可选执行器（测试 monkeypatch；缺省 run_provider）
      discover_fn   可选发现器（测试注入；缺省 discover_providers）

    线程体 run(stop: threading.Event)：
      enabled=false 或目录空 → 空转 sleep，不写 view 不拉取；
      快源每周期都拉；SLOW 源单独计时、后台线程慢拉不阻塞快源；
      手动 req 0.25s 轮询 mtime+签名，幂等最近 64 id，同 provider <10s 节流；
      view 内容无变不空写（签名不含 ts，ts 只在落盘时打）。
    """

    def __init__(self, bus_dir, providers_dir="", get_cfg=None, python_exe=None,
                 logger=None, now_fn=None, run_fn=None, discover_fn=None,
                 sleep_fn=None):
        self.bus_dir = Path(bus_dir)
        self.providers_dir = str(providers_dir or "")
        self._get_cfg = get_cfg or (lambda: {})
        self.python_exe = str(python_exe or sys.executable)
        self._logger = logger
        self._now = now_fn or time.time
        self._run = run_fn or run_provider
        self._discover = discover_fn or discover_providers
        self._sleep = sleep_fn
        self._stop = None
        self.view_path = self.bus_dir / "balance_view.json"
        self.req_path = self.bus_dir / "balance_refresh_req.json"
        self._entries = {}          # id -> view 条目（最近一次结果）
        self._last_fetch = {}       # id -> 最近 fetch 完成时刻（节流基准）
        self._slow_busy = set()     # 在跑的 SLOW id
        self._slow_threads = {}     # id -> Thread（join 用）
        self._req_done = []         # 幂等：最近 64 个已处理请求 id
        self._view_sig = None
        self._lock = threading.Lock()
        # N18：write_view 的 sig 判定+写盘+更新 sig 必须独占——本方法同时被宿主
        # 消费线程（__init__._write_view）和 watcher 线程调用。独立锁，**绝不在
        # _lock 内取它、也不在它内取 _lock 之外的东西**（_build_view 会取 _lock，
        # 锁序固定 view_lock -> lock，反向嵌套=死锁）。
        self._view_lock = threading.Lock()
        self._last_cfg = {"enabled": False, "refresh_s": 300, "providers_dir": ""}

    # ---------------- 配置/日志 ----------------
    def _cfg(self) -> dict:
        try:
            raw = self._get_cfg() or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        cfg = {
            "enabled": bool(raw.get("enabled", False)),
            "refresh_s": _clamp_refresh(raw.get("refresh_s", 300)),
            "providers_dir": str(raw.get("providers_dir") or ""),
            "token_plan_edition": str(raw.get("token_plan_edition") or "individual"),
        }
        self._last_cfg = cfg
        return cfg

    def _resolve_dir(self, cfg) -> Path:
        d = cfg.get("providers_dir") or self.providers_dir
        return Path(d) if d else None

    def _log(self, msg) -> None:
        msg = mask_secrets(msg)
        try:
            if self._logger is not None:
                self._logger.info("gateway-ui balance: %s", msg)
        except Exception:
            pass
        try:
            self.bus_dir.mkdir(parents=True, exist_ok=True)
            f = self.bus_dir / "balance_debug.log"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} BAL {msg}\n")
            if f.stat().st_size > LOG_MAX_BYTES:
                keep = f.read_text(encoding="utf-8").splitlines()[-LOG_KEEP_LINES:]
                tmp = _tmp_sibling(f)
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                os.replace(tmp, f)
        except Exception:
            pass

    # ---------------- 拉取 ----------------
    def _fetch_one(self, meta) -> None:
        pid = meta["id"]
        cfg = self._last_cfg
        extra_env = {"HERMES_BALANCE_TOKEN_PLAN_EDITION":
                     cfg.get("token_plan_edition", "individual")}
        try:
            res = self._run(meta["path"], slow=bool(meta.get("slow")),
                            python_exe=self.python_exe, log=self._log,
                            extra_env=extra_env)
        except Exception as exc:
            res = {"ok": False, "error": _short(f"runner crashed: {exc}")}
        entry = normalize(res, meta, now=self._now())
        with self._lock:
            self._entries[pid] = entry
            self._last_fetch[pid] = self._now()
        if not entry["ok"]:
            self._log(f"{pid} fetch failed: {entry['error']}")

    def _spawn_slow(self, meta) -> None:
        pid = meta["id"]
        with self._lock:
            if pid in self._slow_busy:
                return
            self._slow_busy.add(pid)

        def body():
            try:
                self._fetch_one(meta)
            finally:
                with self._lock:
                    self._slow_busy.discard(pid)
                    self._slow_threads.pop(pid, None)

        t = threading.Thread(target=body, name=f"gui-balance-slow-{pid}", daemon=True)
        with self._lock:
            self._slow_threads[pid] = t
        t.start()

    def _cycle(self, metas, force_ids=None) -> None:
        """一轮拉取：快源同步串行（每个 ≤10s），SLOW 源后台线程（≤45s 不阻塞）。
        force_ids=None 全量；给定集合则只拉其中（手动单刷）。"""
        for meta in metas:
            pid = meta["id"]
            if force_ids is not None and pid not in force_ids:
                continue
            if meta.get("slow"):
                self._spawn_slow(meta)
            else:
                self._fetch_one(meta)

    # ---------------- view ----------------
    def _build_view(self, metas, cfg) -> dict:
        with self._lock:
            entries = [self._entries[m["id"]] for m in metas
                       if m["id"] in self._entries]
        return {
            "refresh_s": int(cfg["refresh_s"]),
            "providers": entries,
            "default_id": metas[0]["id"] if metas else "",
        }

    def write_view(self, metas=None, cfg=None) -> bool:
        """写 balance_view.json（原子 tmp+rename）；内容无变不空写。
        consumer 同拍接线入口：metas/cfg 缺省时自查。返回是否真落盘。
        N18：本方法被消费线程与 watcher 线程并发调用——sig 判定、写盘、更新 sig
        必须在同一把 _view_lock 内完成，否则两线程交替写同一路径 + 同一个 .tmp，
        读侧可能撞上半文件，或旧签名把新内容挡掉。"""
        with self._view_lock:
            cfg = cfg or self._cfg()
            if not cfg["enabled"]:
                return False
            if metas is None:
                d = self._resolve_dir(cfg)
                metas = self._discover(d) if d is not None else []
            if not metas:
                return False
            view = self._build_view(metas, cfg)
            sig = json.dumps(view, ensure_ascii=False, sort_keys=True)
            if sig == self._view_sig:
                return False
            self._view_sig = sig
            view["ts"] = round(self._now(), 3)
            try:
                self.bus_dir.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(self.view_path, view)
                return True
            except Exception as exc:
                self._log(f"view write failed: {_short(exc)}")
                return False

    # ---------------- 手动刷新请求 ----------------
    def _poll_req(self, metas, state) -> None:
        """0.25s 拍消费 balance_refresh_req.json（mtime+签名双判）。
        启动基线：register 前遗留文件视为陈旧不回放（同 settings watcher）。"""
        try:
            if not self.req_path.exists():
                return
            mtime = self.req_path.stat().st_mtime
            raw = self.req_path.read_text(encoding="utf-8")
            if state["first"]:
                state["first"] = False
                state["mtime"], state["sig"] = mtime, raw
                return
            if mtime == state["mtime"] and raw == state["sig"]:
                return
            state["mtime"], state["sig"] = mtime, raw
            try:
                req = json.loads(raw)
            except Exception:
                return
            if not isinstance(req, dict):
                return
            rid = str(req.get("id") or "")
            if rid and rid in self._req_done:
                return  # 幂等
            if rid:
                self._req_done.append(rid)
                if len(self._req_done) > ID_KEEP:
                    del self._req_done[:len(self._req_done) - ID_KEEP]
            pid = str(req.get("provider_id") or "")
            now = self._now()
            if pid:
                targets = [m for m in metas if m["id"] == pid]
            else:
                targets = list(metas)
            # 节流：同 provider 距上次 fetch <10s 忽略（防连点烧钱）
            due = []
            for m in targets:
                with self._lock:
                    last = self._last_fetch.get(m["id"])
                if last is None or now - last >= MANUAL_THROTTLE_S:
                    due.append(m)
            if not due:
                return
            self._log(f"manual refresh: {pid or 'ALL'} ({len(due)} due)")
            self._cycle(metas, force_ids={m["id"] for m in due})
            self.write_view(metas)
        except Exception:
            pass

    # ---------------- 线程体 ----------------
    def _tick(self, s) -> bool:
        """睡 s 秒；stop 置位返回 True（该退出）。可注入 sleep_fn 供假时钟测试。"""
        if self._sleep is not None:
            self._sleep(s)
            return self._stop.is_set()
        return self._stop.wait(s)

    def run(self, stop) -> None:
        """stop: threading.Event。enabled=false/目录空 → 空转 sleep 不拉不写。"""
        self._stop = stop
        req_state = {"first": True, "mtime": None, "sig": None}
        next_fast = 0.0
        next_slow = {}
        known_ids = set()
        while not stop.is_set():
            try:
                cfg = self._cfg()
                if not cfg["enabled"]:
                    if self._tick(REQ_POLL_S):
                        break
                    continue
                d = self._resolve_dir(cfg)
                metas = self._discover(d) if d is not None else []
                if not metas:
                    if self._tick(REQ_POLL_S):
                        break
                    continue
                ids = {m["id"] for m in metas}
                if ids != known_ids:
                    known_ids = ids
                    with self._lock:
                        for gone in list(self._entries):
                            if gone not in ids:
                                del self._entries[gone]
                                self._last_fetch.pop(gone, None)
                    next_slow = {k: v for k, v in next_slow.items() if k in ids}
                    with self._view_lock:      # N18：sig 归零也要与 write_view 同锁
                        self._view_sig = None  # provider 集合变了，view 必须重写
                now = self._now()
                refresh = float(cfg["refresh_s"])
                # 快源：每周期都拉（新 provider 立即拉）
                fast_due = [m for m in metas if not m.get("slow")
                            and (now >= next_fast
                                 or m["id"] not in self._entries)]
                if fast_due:
                    self._cycle(metas, force_ids={m["id"] for m in fast_due})
                    next_fast = self._now() + refresh
                # SLOW 源：单独计时，后台慢拉不阻塞快源。
                # 首轮（无缓存）立即拉；之后按 refresh_s 周期（_spawn_slow 对在途去重）
                for m in metas:
                    if not m.get("slow"):
                        continue
                    pid = m["id"]
                    if pid not in self._entries or now >= next_slow.get(pid, 0.0):
                        self._spawn_slow(m)
                        next_slow[pid] = self._now() + refresh
                self._poll_req(metas, req_state)
                self.write_view(metas, cfg)
            except Exception as exc:
                self._log(f"watcher loop error: {_short(exc)}")
            if self._tick(REQ_POLL_S):
                break
        # 收尾：等在跑的 SLOW 线程（最多一个超时钳），不留野线程
        with self._lock:
            threads = list(self._slow_threads.values())
        for t in threads:
            t.join(timeout=TIMEOUT_SLOW_S + 5)