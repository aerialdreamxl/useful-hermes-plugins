"""gateway-ui 配置层 —— A1 根除：三态解析 + 默认补齐不回写。

作者 DIY 规范②（逐键自动补齐）与 A1 死循环的调和裁定（整体重写）：
- 显示类键（slug/scale/roam/topmost + 顶层 tray_enabled 等）缺键照补，走官方 writer。
- **enable-pet 是修改类开关：缺键 ≠ false，缺失时绝不把默认 false 写进 config.yaml。**
  运行时真值解析顺序：config 显式值 > pet_cfg.json 运行时值 > False。
  watcher 回写只认「文件真值 vs config 显式值」的真实差异；
  「config 缺键 + 文件 false」不写（false 可能只是默认值，写回去就固化成 A1 死循环）。
  菜单勾选 true 时文件 true vs 缺键 → 写 true，开关权只属于菜单与作者手改。

本模块纯函数与 ctx 薄封装分离，test_config.py 直接测纯函数。
"""
from __future__ import annotations

import contextlib
import os

MISSING = object()

TOP_DEFAULTS = {
    "tray_enabled": True,
    "bus_enabled": True,
    "degrade_window_s": 300,
    "verbose_log": False,
    "package_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
    # R27 子代理监听（修改类，默认 false）：subagent_busy 开=会话全空闲但
    # delegate_task 子代理在跑时，忙线判 acting/灯橙（主人 2026-11-16 点名）。
    "subagent_busy": False,
}

PET_DEFAULTS = {
    "enable-pet": False,   # 修改类：三态处理，缺失不落盘
    "slug": "",
    "scale": 0.7,
    "roam": True,
    "topmost": True,
    # 地面物理增强 R26（PROMPT §11）：显示类行为细节键（与 roam/topmost 同族，
    # 缺键自动补齐走官方 writer）。follow_window=站立线段跟随来源窗口运动；
    # vertical_roam=主动上下爬+跨线段跳转（关掉仍保留确认计时器被动上爬）。
    "follow_window": True,
    "vertical_roam": True,
    # 事件反应：会话事件反应（托盘闪动+桌宠 play_reaction）。作者本轮点名要此功能，
    # 默认 true（DIY 规范默认 false 的偏离已向作者申报）。修改类开关：
    # 缺键按默认 true 读，绝不自动落盘（A1 铁律），只有用户显式操作才写。
    "event_reactions": True,
}

# 余额气泡 BalanceBubble（CONTRACT §10.4）：全部修改类语义——缺键按默认读、
# 绝不自动落盘，不进任何 seed/fill 路径（A1 铁律）。enabled 默认 false：
# 作者显式打开才起 watcher 拉余额（额度纪律）。
BALANCE_DEFAULTS = {
    "enabled": False,
    "refresh_s": 300,       # 钳 60-3600
    "providers_dir": "",    # ""=用 <home>/balance-providers；非空必须绝对路径
    # 余额气泡修订：Token Plan 版本选择（修改类，缺键按 individual 读不落盘）。
    # 经 HERMES_BALANCE_TOKEN_PLAN_EDITION 环境变量注入 binding 子进程。
    "token_plan_edition": "individual",   # individual | team
}

# 显示类 pet 键：缺键自动补齐进 config（规范②）；enable-pet/event_reactions 不在其列。
PET_DISPLAY_KEYS = tuple(k for k in PET_DEFAULTS if k not in ("enable-pet", "event_reactions"))


def read_top(ctx, key):
    """顶层键读取 + 缺键补齐（官方 writer）。"""
    default = TOP_DEFAULTS.get(key)
    try:
        v = ctx.get_config(key, MISSING)
        if v is MISSING:
            v = default
            with contextlib.suppress(Exception):
                ctx.set_config(key, v)
        return v
    except Exception:
        return default


def read_pet_explicit(ctx) -> dict:
    """config.yaml pet 块的显式值（不补 enable-pet，不做任何写入）。"""
    try:
        cfg = ctx.get_config("pet", MISSING)
    except Exception:
        cfg = MISSING
    if not isinstance(cfg, dict):
        return {}
    return {k: v for k, v in cfg.items() if k in PET_DEFAULTS}


def fill_pet_display(ctx, explicit: dict) -> dict:
    """显示类 pet 键缺键补齐（官方 writer）；enable-pet 缺失保持缺失。
    返回补齐后的显式块（供播种/对账用）。"""
    out = dict(explicit)
    changed = False
    for k in PET_DISPLAY_KEYS:
        if k not in out:
            out[k] = PET_DEFAULTS[k]
            changed = True
    if changed:
        with contextlib.suppress(Exception):
            ctx.set_config("pet", out)
    return out


def resolve_pet(explicit: dict, runtime: dict | None) -> dict:
    """播种真值：显式块为基础，enable-pet 三态 + 播种对账 OR 对账。
    explicit 有 enable-pet → 用它；但 runtime 文件为 true 时 OR 保留 true
    （菜单刚开就重启、watcher 没来得及回写的竞态不丢开关）。
    explicit 缺 enable-pet → 取 runtime 文件值；文件也没有 → False（只进运行时，不落 config）。
    """
    out = dict(PET_DEFAULTS)
    out.update({k: v for k, v in explicit.items() if k in PET_DEFAULTS})
    rt = runtime if isinstance(runtime, dict) else {}
    if "enable-pet" in explicit:
        out["enable-pet"] = bool(explicit["enable-pet"]) or bool(rt.get("enable-pet"))
    else:
        out["enable-pet"] = bool(rt.get("enable-pet", False))
    return out


def watcher_merge(file_cfg: dict, explicit: dict) -> dict | None:
    """pet_cfg.json → config.yaml 回写判定（纯函数）。
    返回应写入的完整 pet 块；无真实差异返回 None（不空转 writer）。
    A1 铁律：enable-pet 缺键时只写 true，绝不写默认 false。
    【设置界面起 pet_cfg.json 退役，本函数仅留作历史测试基准，生产不再调用。】"""
    if not isinstance(file_cfg, dict):
        return None
    out = dict(explicit)
    changed = False
    for k in PET_DEFAULTS:
        if k not in file_cfg:
            continue
        fv = file_cfg[k]
        if k == "enable-pet":
            fv = bool(fv)
            if k in explicit:
                if bool(explicit[k]) != fv:
                    out[k] = fv
                    changed = True
            elif fv:  # 缺键 + 文件 true = 菜单真值，写；缺键 + false 不写
                out[k] = True
                changed = True
        else:
            if k not in explicit or explicit[k] != fv:
                out[k] = fv
                changed = True
    return out if changed else None


# ================================================================ 设置界面 F7 ====
# settings 总线协议（CONTRACT §6）：worker 发请求 → 宿主校验 → 官方 writer 落盘。
# config.yaml 只有官方 ctx.set_config 碰；本模块只做纯函数校验/快照，不碰文件。

# 请求白名单：顶层 5 键 + pet 块 6 键（前缀 pet.）+ balance 块 3 键（前缀 balance.，
# 余额气泡 §10.4）。越界 key 一律拒绝。
REQ_TOP_KEYS = tuple(TOP_DEFAULTS)
REQ_PET_KEYS = tuple("pet." + k for k in PET_DEFAULTS)
REQ_BALANCE_KEYS = tuple("balance." + k for k in BALANCE_DEFAULTS)
REQ_KEYS = REQ_TOP_KEYS + REQ_PET_KEYS + REQ_BALANCE_KEYS

SCALE_MIN, SCALE_MAX = 0.1, 3.0
WINDOW_MIN, WINDOW_MAX = 60, 1800
REFRESH_MIN, REFRESH_MAX = 60, 3600


def explicit_top(ctx) -> list:
    """config 里真实存在的顶层键名（缺键不在列；不补默认、不写入）。"""
    out = []
    for k in TOP_DEFAULTS:
        try:
            if ctx.get_config(k, MISSING) is not MISSING:
                out.append(k)
        except Exception:
            pass
    return out


def explicit_pet_keys(ctx) -> list:
    """config pet 块里真实存在的键名（含 enable-pet 若显式存在）。"""
    return list(read_pet_explicit(ctx))


def read_balance_explicit(ctx) -> dict:
    """余额气泡：config.yaml balance 块的显式值（纯读，绝不写入、不补默认）。
    全部修改类语义（§10.4）：缺键按 BALANCE_DEFAULTS 读只在快照层做。"""
    try:
        cfg = ctx.get_config("balance", MISSING)
    except Exception:
        cfg = MISSING
    if not isinstance(cfg, dict):
        return {}
    return {k: v for k, v in cfg.items() if k in BALANCE_DEFAULTS}


def explicit_balance_keys(ctx) -> list:
    """config balance 块里真实存在的键名（缺键不在列）。"""
    return list(read_balance_explicit(ctx))


def validate_request(req, installed_slugs=()) -> tuple:
    """settings_req 校验（纯函数）。返回 (ok, key, value, error)。
    白名单 key + 类型/范围钳制；非法返回 ok=False 带 error（宿主据此拒写）。
    不抛异常——任何畸形输入都收敛成一条拒绝回执。"""
    if not isinstance(req, dict):
        return (False, "", None, "request not a dict")
    key = req.get("key")
    if not isinstance(key, str) or key not in REQ_KEYS:
        return (False, str(key), None, f"key not allowed: {key!r}")
    if "value" not in req:
        return (False, key, None, "missing value")
    val = req["value"]
    # 余额气泡 §10.4：balance 三键（全修改类；providers_dir 空串=用默认目录）
    if key.startswith("balance."):
        bleaf = key[len("balance."):]
        try:
            if bleaf == "enabled":
                return (True, key, bool(val), "")
            if bleaf == "refresh_s":
                n = int(float(val))
                return (True, key, max(REFRESH_MIN, min(REFRESH_MAX, n)), "")
            if bleaf == "providers_dir":
                s = str(val or "").strip()
                if s:
                    if ".." in s:
                        return (False, key, s, "providers_dir must not contain '..'")
                    if not os.path.isabs(s):
                        return (False, key, s, "providers_dir must be an absolute path")
                return (True, key, s, "")
            if bleaf == "token_plan_edition":
                s = str(val or "").strip().lower()
                if s not in ("individual", "team"):
                    return (False, key, val, "edition must be individual or team")
                return (True, key, s, "")
        except (TypeError, ValueError) as exc:
            return (False, key, val, f"bad value: {exc}")
        return (False, key, val, f"unhandled key: {key!r}")
    leaf = key[4:] if key.startswith("pet.") else key
    try:
        if leaf in ("tray_enabled", "bus_enabled", "verbose_log",
                    "enable-pet", "roam", "topmost", "event_reactions",
                    "follow_window", "vertical_roam", "subagent_busy"):
            return (True, key, bool(val), "")
        if leaf == "scale":
            f = float(val)
            return (True, key, max(SCALE_MIN, min(SCALE_MAX, f)), "")
        if leaf == "degrade_window_s":
            n = int(float(val))
            return (True, key, max(WINDOW_MIN, min(WINDOW_MAX, n)), "")
        if leaf == "slug":
            s = str(val or "")
            if s and s not in tuple(installed_slugs):
                return (False, key, s, f"slug not installed: {s!r}")
            return (True, key, s, "")
        if leaf == "package_index_url":
            s = str(val or "").strip()
            if s and not (s.startswith("http://") or s.startswith("https://")):
                return (False, key, s, "url must be http(s)")
            return (True, key, s, "")
    except (TypeError, ValueError) as exc:
        return (False, key, val, f"bad value: {exc}")
    return (False, key, val, f"unhandled key: {key!r}")


def build_view(ctx, installed_slugs=()) -> dict:
    """settings_view 快照（官方 get_config 读取，纯读不写）。
    top/pet = 运行时真值（缺键补默认，仅进快照不落 config）；
    explicit = config 真实存在的键表（设置页据此显示"默认值/未设置"角标）。"""
    top = {}
    for k in TOP_DEFAULTS:
        try:
            v = ctx.get_config(k, MISSING)
        except Exception:
            v = MISSING
        top[k] = TOP_DEFAULTS[k] if v is MISSING else v
    pet_explicit = read_pet_explicit(ctx)
    pet = dict(PET_DEFAULTS)
    pet.update(pet_explicit)
    # enable-pet 三态：缺键运行时 false（不落 config）；显式值照用。
    pet["enable-pet"] = bool(pet_explicit.get("enable-pet", False))
    # event_reactions 同款修改类：缺键运行时 true（作者点名默认开，不落 config）；显式值照用。
    pet["event_reactions"] = bool(pet_explicit.get("event_reactions", True))
    # 余额气泡 §10.4：balance 块——缺键补默认仅进快照（全修改类，绝不落 config）。
    balance_explicit = read_balance_explicit(ctx)
    balance = dict(BALANCE_DEFAULTS)
    balance.update(balance_explicit)
    balance["enabled"] = bool(balance_explicit.get("enabled", False))
    try:
        balance["refresh_s"] = int(balance_explicit.get("refresh_s", 300))
    except (TypeError, ValueError):
        balance["refresh_s"] = BALANCE_DEFAULTS["refresh_s"]
    balance["refresh_s"] = max(REFRESH_MIN, min(REFRESH_MAX, balance["refresh_s"]))
    balance["providers_dir"] = str(balance_explicit.get("providers_dir", "") or "")
    ed = str(balance_explicit.get("token_plan_edition", "individual") or "").strip().lower()
    balance["token_plan_edition"] = ed if ed in ("individual", "team") else "individual"
    return {
        "top": top,
        "pet": pet,
        "balance": balance,
        "explicit": {"top": explicit_top(ctx), "pet": explicit_pet_keys(ctx),
                     "balance": explicit_balance_keys(ctx)},
    }