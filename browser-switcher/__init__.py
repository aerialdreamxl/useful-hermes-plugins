"""browser-switcher — 会话作用域的浏览器后端转换插头。

三档后端（全部收敛到 Chromium CDP，刻意不管 Camofox / 扩展控制 / 云厂商）：
  default  不干预，走 Hermes 打包的 Headless Chromium（agent-browser 路径）
  shardx   本机 ShardX 指纹浏览器，经其本地 HTTP API 启动 profile 拿 CDP ws URL
  cdp      任意 Chromium CDP 端点（ws://…/devtools/browser/… 或 http://host:port）

机制（对 v0.22.2 源码取证）：
- tools/browser_use_cli.py 的 _resolve_backend_cdp 优先序：BU_CDP_* env（本进程内注入）
  > BROWSER_CDP_URL env / browser.cdp_url > cloud provider > 本地引擎。
  本插件把解析出的 CDP URL 写进 os.environ["BROWSER_CDP_URL"]，即劫持该优先序第 2 级，
  browser_exec 与 built-in browser_* 两条工具面都逐次读它，无需改核心。
- harness daemon 与 agent-browser daemon 都只在冷启动时快照 env（browser_use_cli.py:669
  注释实锤）。因此每次切换/漂移后必须：
    a) 对 harness：等价 stop_harness_daemons() 的 per-BU_NAME --reload；
    b) 对 agent-browser：_run_browser_command(cache_key,"close") 撕掉旧 daemon。
- 会话作用域：上游没有会话级 provider 槽位（_get_cloud_provider/_get_cdp_override 全是
  进程级），故插件自带作用域层：register_middleware("tool_request") 在每次 browser_* 派发前
  对比「本会话 desired」与「进程 attached」，不一致 → 换端点 + 重启 daemon 再继续。
  状态纯内存、进程生命周期 = 天然「仅本会话生效」。

ShardX 配置全部来自环境（$HERMES_HOME/.env 或进程环境）：
  SHARDX_BASE_URL（默认 http://127.0.0.1:40325）/ SHARDX_API_KEY（Bearer JWT）
  / SHARDX_BROWSER_NAME（默认 profile 名）。密钥经 agent.secret_scope.get_secret 解析，
  绝不打印值；日志只出现 profile 名与 URL（脱敏后）。

已知局限（如实陈述）：
- gateway 多路复用并发多会话时 env 是进程级的 → 两个会话切不同后端会 last-writer-wins
  并互相触发 reload（乒乓）。单会话部署（本机现状）无此问题。
- built-in browser_* 面在 /browser connect 等进程外通道也写同一 env 键时归属本插件管辖，
  切换 default 会清掉它。
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "browser-switcher"
_PROVIDERS = ("default", "shardx", "cdp")

# ── 会话作用域状态（纯内存）─────────────────────────────────────────────
_lock = threading.RLock()
# key(会话别名) -> {provider, extra, headless}
_desired: Dict[str, Dict[str, Any]] = {}
# 当前进程实际挂载的后端（全进程唯一，因为 env 是进程级）：
# None = default；否则 {provider, cdp, shardx_profile_id, resolved_for_keys:{别名:切换时戳}}
_attached: Optional[Dict[str, Any]] = None
# attached 是为哪些会话键解析的（漂移检测只在「本会话的 desired != attached」时触发）
_attached_owner: Dict[str, float] = {}
# 同一 desired 的解析失败冷却，防每次 browser_* 调用都打 ShardX API
_last_fail: Dict[str, Any] = {}


def _now() -> float:
    import time
    return time.monotonic()


# ── 会话键解析阶梯（借鉴 session-hold / smart-terminal-cwd 实证）──────────
def _session_keys(*, task_id: str = "", session_id: str = "") -> list[str]:
    """收集当前可见的全部会话别名（写入时互为别名，middleware 侧查任一即中）。"""
    keys: list[str] = []
    for v in (session_id, task_id):
        if v and v not in keys:
            keys.append(str(v))
    try:
        from gateway.session_context import get_session_env
        for name in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_UI_SESSION_ID"):
            v = get_session_env(name, "")
            if v and v not in keys:
                keys.append(v)
    except Exception:
        pass
    try:
        from tools.approval import get_current_session_key
        v = get_current_session_key(default="")
        if v and v not in keys:
            keys.append(str(v))
    except Exception:
        pass
    return keys


def _lookup(mapping: Dict[str, Any], keys: list[str]) -> Optional[Any]:
    for k in keys:
        if k in mapping:
            return mapping[k]
    return None


# ── ShardX 本地 API ────────────────────────────────────────────────────
def _shardx_base() -> str:
    try:
        from agent.secret_scope import get_secret
        v = get_secret("SHARDX_BASE_URL") or ""
    except Exception:
        v = os.environ.get("SHARDX_BASE_URL", "")
    return (v.strip() or "http://127.0.0.1:40325").rstrip("/")


def _shardx_token() -> str:
    try:
        from agent.secret_scope import get_secret
        v = get_secret("SHARDX_API_KEY") or ""
    except Exception:
        v = os.environ.get("SHARDX_API_KEY", "")
    return v.strip()


def _shardx_default_name() -> str:
    try:
        from agent.secret_scope import get_secret
        v = get_secret("SHARDX_BROWSER_NAME") or ""
    except Exception:
        v = os.environ.get("SHARDX_BROWSER_NAME", "")
    return v.strip()


def _shardx_call(method: str, path: str, body: Any = None, timeout: float = 35.0) -> Any:
    """ShardX API 一次调用；HTTP 层错误抛 RuntimeError（401 空 body = 令牌错/已轮换）。"""
    url = _shardx_base() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    token = _shardx_token()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):
            detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"ShardX API {method} {path} -> HTTP {e.code} {detail}".strip()) from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"ShardX API {method} {path} 不可达: {e}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        raise RuntimeError(f"ShardX API {method} {path} 返回非 JSON") from None


def _shardx_health() -> bool:
    try:
        h = _shardx_call("GET", "/health", timeout=4.0)
        return bool(h.get("ok"))
    except Exception:
        return False


def _shardx_find_profile(name: str) -> Optional[dict]:
    try:
        profiles = _shardx_call("GET", "/profiles", timeout=6.0)
    except Exception as e:
        raise RuntimeError(f"ShardX 列举 profile 失败: {e}") from None
    if isinstance(profiles, dict):
        profiles = profiles.get("data") or profiles.get("profiles") or []
    for p in profiles or []:
        if isinstance(p, dict) and str(p.get("name", "")) == name:
            return p
    return None


def _shardx_start(name: str, headless: bool = True) -> dict:
    """按名称启动 profile，返回 {profile_id, cdp}。已在 UI 打开时先 stop 再 API start
    （UI 启动的窗口 cdp 恒 null，这是 ShardX 防反检测设计）。"""
    prof = _shardx_find_profile(name)
    if prof is None:
        avail = []
        with contextlib.suppress(Exception):
            lst = _shardx_call("GET", "/profiles", timeout=6.0)
            if isinstance(lst, dict):
                lst = lst.get("data") or lst.get("profiles") or []
            avail = [str(p.get("name")) for p in lst if isinstance(p, dict)][:12]
        raise RuntimeError(f"ShardX 没有名为 '{name}' 的 profile。可用: {', '.join(avail) or '(列举失败)'}")
    pid = str(prof.get("id") or prof.get("profileId") or "")
    if not pid:
        raise RuntimeError(f"ShardX profile '{name}' 无 id 字段")
    if prof.get("running"):
        with contextlib.suppress(Exception):
            _shardx_call("POST", f"/profiles/{pid}/stop", {}, timeout=10.0)
    started = _shardx_call("POST", f"/profiles/{pid}/start", {"headless": bool(headless)}, timeout=35.0)
    # 实测（launcher 2.0.3）：start 响应的 cdp 是对象 {port, http_url, web_socket_debugger_url}；
    # 兼容老式字符串形态。优先 ws URL（playwright/browser 面直连），退化用 http 端点让上游发现。
    cdp_obj = (started or {}).get("cdp")
    if isinstance(cdp_obj, dict):
        cdp = str(cdp_obj.get("web_socket_debugger_url") or cdp_obj.get("http_url") or "")
    else:
        cdp = str(cdp_obj or "")
    if not cdp:
        reason = (started or {}).get("cdp_error") or "start 响应无 cdp 字段"
        raise RuntimeError(f"ShardX profile '{name}' 启动后拿不到 CDP: {reason}")
    return {"profile_id": pid, "cdp": cdp, "name": name}


def _shardx_stop(profile_id: str) -> None:
    with contextlib.suppress(Exception):
        _shardx_call("POST", f"/profiles/{profile_id}/stop", {}, timeout=10.0)


# ── CDP 端点发现（http 端点 -> ws URL；ws 原样）─────────────────────────
# F3 白名单只认 RFC1918 + IPv6 ULA；loopback / link-local 用 ipaddress 自带判定
_LOCAL_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_LOCAL_V6 = (ipaddress.ip_network("fc00::/7"),)


def _cdp_host_is_local(host: str) -> bool:
    """只看字面：localhost / loopback / RFC1918 / ULA / link-local。"""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local:
        return True
    for n in (_LOCAL_V4 if ip.version == 4 else _LOCAL_V6):
        if ip in n:
            return True
    return False


def _check_cdp_endpoint(url: str) -> None:
    """F3：先验 scheme，再验 CDP 主机白名单；远程需显式开关。"""
    allow = (os.environ.get("BROWSER_SWITCHER_ALLOW_REMOTE_CDP") or "").strip().lower()
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or ""
        if scheme not in ("ws", "wss", "http", "https") or not host:
            raise RuntimeError("CDP URL 需以 ws://、wss://、http:// 或 https:// 开头并包含主机")
        port = parts.port
    except ValueError as e:
        raise RuntimeError("CDP URL 端口无效") from e
    if allow in ("1", "true", "yes"):
        return
    if _cdp_host_is_local(host):
        return
    raise RuntimeError(
        f"CDP 端点主机 '{host or '?'}' 不在本地白名单（只允许 127.0.0.0/8、::1、localhost、"
        "RFC1918 私网、IPv6 ULA fc00::/7、link-local）。若确需连远程端点，先设 "
        "BROWSER_SWITCHER_ALLOW_REMOTE_CDP=1。"
    )


def _redact_cdp_url(url: str) -> str:
    """status/返回文案只保留 scheme、host、port，不暴露 path 中的 GUID。"""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        host = parts.hostname or "?"
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except Exception:
        return "<invalid-cdp>"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """发现请求禁自动跟随重定向：3xx 直接抛，绝不先访问重定向目标再判。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _discover_cdp_ws(url: str) -> str:
    url = url.strip()
    if not url:
        raise RuntimeError("CDP URL 为空")
    if not re.match(r"^(wss?|https?)://", url):
        raise RuntimeError(f"CDP URL 需以 ws:// wss:// http:// https:// 开头，收到: {url!r}")
    _check_cdp_endpoint(url)
    if url.startswith(("ws://", "wss://")):
        return url
    version_url = url.rstrip("/") + "/json/version"
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(version_url, timeout=6.0) as resp:
            if str(resp.geturl()) != version_url:
                raise RuntimeError("CDP 发现请求被重定向（已拒绝）")
            info = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            raise RuntimeError(f"CDP 端点 {url} /json/version 返回重定向（已拒绝跟随）") from e
        raise RuntimeError(f"CDP 端点 {url} /json/version 发现失败: {e}") from e
    except Exception as e:
        raise RuntimeError(f"CDP 端点 {url} /json/version 发现失败: {e}") from e
    ws = str(info.get("webSocketDebuggerUrl") or "")
    if not ws:
        raise RuntimeError(f"CDP 端点 {url} 未返回 webSocketDebuggerUrl（Chromium 136+ 需带 --remote-allow-origins 重启浏览器）")
    _check_cdp_endpoint(ws)
    if urllib.parse.urlsplit(ws).scheme.lower() not in ("ws", "wss"):
        raise RuntimeError("CDP discovery returned a non-websocket URL")
    return ws


# ── env 装载 / daemon 撕除 ──────────────────────────────────────────────
def _set_process_cdp(cdp: Optional[str]) -> None:
    """写/清进程级 BROWSER_CDP_URL —— 两条工具面逐次读取的唯一生效通道。"""
    if cdp:
        os.environ["BROWSER_CDP_URL"] = cdp
    else:
        os.environ.pop("BROWSER_CDP_URL", None)


def _stop_harness_daemons_best_effort() -> None:
    """上游同款：对已驱动过的 BU_NAME 逐个 --reload（daemon 只在 spawn 读一次 env）。"""
    try:
        from tools.browser_use_cli import stop_harness_daemons
        stop_harness_daemons()
    except Exception as e:
        logger.debug("browser-switcher: harness daemon stop failed (non-fatal): %s", e)


def _close_agent_browser_daemons_best_effort() -> None:
    """撕掉 agent-browser Node daemon（冷启动快照 env，不撕则新后端被静默忽略——2026-10-21 实测）。"""
    try:
        from tools.browser_tool_lifecycle import cleanup_all_browsers
        cleanup_all_browsers()
    except Exception as e:
        logger.debug("browser-switcher: agent-browser cleanup failed (non-fatal): %s", e)


# ── 核心：把 desired 落到进程实际状态 ──────────────────────────────────
def _apply(desired: Dict[str, Any]) -> str:
    """解析并挂载 desired；成功返回描述行，失败抛 RuntimeError（不改进程状态）。

    N1：解析端点（ShardX start 最长 35s、CDP 发现 6s）与停旧 profile（10s）全部在 _lock
    外做 —— _middleware 每次 browser_* 派发前抢同一把锁，持锁等网络会把其它线程全排住。
    """
    provider = desired["provider"]
    global _attached
    # 1) 先解析新端点（解析失败 = 进程状态原样，调用方回滚 desired）
    if provider == "default":
        new_cdp, shard_pid = "", ""
    elif provider == "shardx":
        started = _shardx_start(desired["extra"] or _shardx_default_name() or "", headless=desired.get("headless", True))
        _check_cdp_endpoint(started["cdp"])
        new_cdp, shard_pid = started["cdp"], started["profile_id"]
    elif provider == "cdp":
        _check_cdp_endpoint(desired["extra"] or "")
        new_cdp, shard_pid = _discover_cdp_ws(desired["extra"]), ""
    else:
        raise RuntimeError(f"未知后端 '{provider}'")
    to_stop = ""
    with _lock:
        old = _attached
        # 并发：解析期间别的线程已把同一 ShardX profile 挂上 → 复用它解析出的 cdp
        if provider == "shardx" and old and old.get("provider") == "shardx" \
                and old.get("shardx_profile_id") == shard_pid and old.get("cdp"):
            new_cdp = old["cdp"]
        # 2) 拆旧挂载的 ShardX profile（换 default / 换 shardx profile 时）—— 网络部分出锁再做
        if old and old.get("provider") == "shardx" and old.get("shardx_profile_id") and old["shardx_profile_id"] != shard_pid:
            to_stop = old["shardx_profile_id"]
        # 3) 装载新 env + 撕两类 daemon + 清 CDP supervisor 缓存判定（下条工具调用自然重建）
        _set_process_cdp(new_cdp or None)
        _stop_harness_daemons_best_effort()
        _close_agent_browser_daemons_best_effort()
        if provider == "default":
            _attached = None
        else:
            _attached = {"provider": provider, "cdp": new_cdp, "shardx_profile_id": shard_pid,
                         "extra": desired.get("extra", "")}
        # owner 表刷新：当前所有已知别名 + 之前登记过的别名
        for k in list(_attached_owner) :
            _attached_owner[k] = _now()
        logger.info("browser-switcher: attached provider=%s", provider)
    if to_stop:
        # 锁外停工；期间别的线程若又挂回同一 profile 就跳过（别掐活着的浏览器）
        with _lock:
            reused = ((_attached or {}).get("shardx_profile_id") or "") == to_stop
        if not reused:
            _shardx_stop(to_stop)
    if provider == "default":
        return "已切回内建 Headless Chromium（default）。"
    if provider == "shardx":
        return f"已挂载 ShardX 指纹浏览器（profile: {desired['extra'] or _shardx_default_name()}，headless={desired.get('headless', True)}），CDP 握手正常。"
    return f"已挂载外部 CDP 端点（{_redact_cdp_url(new_cdp)}）。"


def _status_line() -> str:
    with _lock:
        provider = (_attached or {}).get("provider", "default")
        extra = ""
        if provider == "shardx":
            extra = f" profile={_shardx_default_name() or '?'}"
        elif provider == "cdp":
            cdp = (_attached or {}).get("cdp", "")
            extra = f" url={_redact_cdp_url(cdp)}"
    # 健康探测是 4s 的网络调用，放锁外（否则 /browserswitch 与 middleware 互堵）
    health = ""
    if provider == "shardx":
        health = "（launcher 在线）" if _shardx_health() else "（⚠ launcher 不在线）"
    return f"当前会话后端: {provider or 'default'}{extra}{health}；作用域=仅本会话，重启进程后自动回到 default。"


# ── 切换入口共享引擎（slash 与 tool 共用一条路，报告式返回）────────────
def _switch(provider: str, extra: str = "", headless: bool = True) -> str:
    provider = (provider or "").strip().lower()
    extra = (extra or "").strip()
    if provider not in _PROVIDERS:
        return f"未知后端 '{provider}'。可选: default / shardx <profile名> / cdp <ws或http URL>。"
    if provider == "shardx" and not extra:
        extra = _shardx_default_name()
        if not extra:
            return "shardx 需要 profile 名：SHARDX_BROWSER_NAME 未设置，命令行也没给。"
    if provider == "cdp" and not extra:
        return "cdp 需要端点 URL：/browserswitch cdp ws://127.0.0.1:9222/devtools/browser/<id> 或 http://host:port"
    if provider == "shardx":
        if not _shardx_token():
            return "SHARDX_API_KEY 未设置（应写在 $HERMES_HOME/.env，程序化读取）。"
        if not _shardx_health():
            return f"ShardX launcher 不在线（{_shardx_base()}/health 无响应）。请先启动 ShardX Launcher。"
    keys = _session_keys()
    desired = {"provider": provider, "extra": extra, "headless": headless}
    try:
        line = _apply(desired)
    except Exception as e:
        _last_fail["err"] = (str(e), _now())
        logger.warning("browser-switcher: switch to %s failed: %s", provider, e)
        return f"切换失败，保持原后端：{e}\n{_status_line()}"
    with _lock:
        for k in keys:
            _desired[k] = desired
            _attached_owner[k] = _now()
    _last_fail.clear()
    return f"{line}\n{_status_line()}"


def _parse_raw_args(raw: str) -> tuple[str, str]:
    parts = (raw or "").split(None, 1)
    provider = parts[0] if parts else ""
    extra = parts[1].strip() if len(parts) > 1 else ""
    return provider, extra


# ── tool_request middleware：漂移检测（用户的 1 号裁定机制）──────────────
def _middleware(payload: Any = None, **ctx: Any) -> None:
    """每次 browser_* 派发前：本会话 desired ≠ 进程 attached → 重装再放行。

    回调内联执行、无 30s 超时（区别于 pre_tool_call hook），ShardX start 最长等 30s 合法。
    只做 side effect，永不改写 args —— 返回 None 即透传。
    """
    global _attached
    tool_name = str(ctx.get("tool_name") or "")
    if not tool_name.startswith("browser_"):
        return None
    keys = _session_keys(task_id=str(ctx.get("task_id") or ""), session_id=str(ctx.get("session_id") or ""))
    if not keys:
        return None
    with _lock:
        desired = _lookup(_desired, keys)
        if desired is None:
            return None  # 本会话从未切换过 —— default 语义
        attached = _attached or {}
        provider = attached.get("provider", "default")
        same_provider = desired["provider"] == provider
        # default 无 extra；shardx/cdp 还要比名称/URL —— 同 provider 换 profile 也算漂移
        matches = same_provider and (provider == "default" or attached.get("extra") == desired["extra"])
        if matches:
            return None
        # attached 不是本会话装的 → 本会话的 desired 生效（last-writer-wins，见 docstring 局限）
    err = _last_fail.get("err")
    if err and _now() - err[1] < 30.0:
        logger.debug("browser-switcher: drift re-apply cooling down (last error: %s)", err[0])
        return None
    try:
        _apply(desired)
        with _lock:
            for k in keys:
                _attached_owner[k] = _now()
        _last_fail.clear()
        logger.info("browser-switcher: drift re-applied %s for session keys %s", desired["provider"], keys[:2])
    except Exception as e:
        _last_fail["err"] = (str(e), _now())
        logger.warning("browser-switcher: drift re-apply failed: %s", e)
    return None


# ── 工具注册 ────────────────────────────────────────────────────────────
_TOOL_SCHEMA = {
    "name": "browser_switch",
    "description": (
        "Switch THIS session's browser backend (converter plug). Providers: 'default' = Hermes "
        "built-in headless Chromium; 'shardx' = local ShardX fingerprint browser started via its "
        "loopback API (arg = profile name; omit to use the SHARDX_BROWSER_NAME default); "
        "'cdp' = any Chromium remote-debugging endpoint (arg = ws://…/devtools/browser/<id> or "
        "http://host:port). The switch takes effect on the NEXT browser tool call in this session; "
        "browser harness daemons are restarted automatically. Session-scoped: restart clears it "
        "back to default. Returns the new backend status line."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": list(_PROVIDERS)},
            "arg": {"type": "string", "description": "Profile name (shardx) or CDP URL (cdp); omit for default."},
            "headless": {"type": "boolean", "description": "shardx only: launch profile headless (default true)."},
        },
        "required": ["provider"],
    },
}


def _tool_handler(args: dict, **kwargs) -> str:
    provider = str(args.get("provider") or "")
    arg = str(args.get("arg") or "")
    headless = bool(args.get("headless", True))
    if provider == "" and not arg:
        return json.dumps({"status": _status_line()}, ensure_ascii=False)
    return json.dumps({"result": _switch(provider, arg, headless=headless)}, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_command(
        "browserswitch",
        _command_handler,
        description="切换本会话浏览器后端：/browserswitch default|shardx [profile名]|cdp [URL]",
        args_hint="[default|shardx <profile>|cdp <url>]",
    )
    ctx.register_tool(
        "browser_switch", "browser", _TOOL_SCHEMA, _tool_handler,
        description="Switch this session's browser backend (default / shardx / cdp)",
        emoji="🔌",
    )
    ctx.register_middleware("tool_request", _middleware)
    logger.info("browser-switcher: registered (session-scoped converter plug: default|shardx|cdp)")


def _command_handler(raw: str) -> str:
    provider, extra = _parse_raw_args(raw)
    if not provider:
        return _status_line() + "\n用法: /browserswitch default | shardx [profile名] | cdp <ws/http URL>"
    if provider == "status":
        return _status_line()
    return _switch(provider, extra)
