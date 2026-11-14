# -*- coding: utf-8 -*-
"""百炼 Token Plan 月额度 binding（gateway-ui 出厂余额适配器，CONTRACT §10.3）。

可删、可改、可加：本目录（<home>/balance-providers/）每个 *.py 是一个余额源。
删掉本文件=气泡不再显示 Token Plan 月额度。
凭证：qianwen CLI 自己的登录态（`qianwen auth login`），本 binding 不碰任何密钥；
可用环境变量 HERMES_QIANWEN_PATH 覆盖 CLI 路径。

数据源：qianwen subscription status --format json + qianwen auth status（取用户名）。
SLOW=True：实测单次 18-28s，宿主放宽超时到 45s 且走后台线程不阻塞快源。

版本选择（作者裁定）：环境变量 HERMES_BALANCE_TOKEN_PLAN_EDITION
（宿主从 config balance.token_plan_edition 注入）：individual=个人版月额度（默认），
team=团队版席位额度。所选版本无生效订阅 → ok=false 报错，不回落另一版（选择是显式的）。

排版（作者原型图裁定，lines 结构化多行，气泡专用渲染）：
  [用户名] 剩余月额度          (title)
  <剩余整数>--  <剩余百分比>%   (amount)
  <档位名>  距到期X天           (info)
  [峰]/[谷] + HH:MM:SS         (气泡峰谷行，倒计时纯时:分:秒)
峰谷（作者补充裁定）：百炼每日 0-8 谷、8-22 峰、22-24 谷（windows 自定义，
无周末/节假日例外）；月重置倒计时不再占用峰谷行。
团队版无月额度字段时按 weeklyCredits→fiveHourCredits→seatSummary 顺序取数，
取不到=ok=false 不造数据。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

NAME = "百炼 Token Plan"
UNIT = "Credits"
ID = "bailian_token_plan"
SLOW = True   # qianwen subscription status 实测 18-28s

_CLI_TIMEOUT_S = 40.0   # binding 内部自设（< 宿主 SLOW 钳 45s）

# 百炼峰谷（作者裁定：每日 0-8 谷 8-22 峰 22-24 谷，北京时间，无周末例外）
BAILIAN_PEAK_WINDOWS = [{"days": [1, 2, 3, 4, 5, 6, 7],
                         "ranges": [[8, 22]], "tz_offset_h": 8}]


def _find_exe(name: str):
    override = str(os.environ.get(f"HERMES_{name.upper()}_PATH") or "").strip()
    if override and Path(override).exists():
        return override
    for candidate in (name, f"{name}.cmd"):
        found = shutil.which(candidate)
        if found:
            return found
    for root in (os.environ.get("APPDATA"), str(Path.home() / "AppData" / "Roaming")):
        if not root:
            continue
        for suffix in (".cmd", ""):
            path = Path(root) / "npm" / f"{name}{suffix}"
            try:
                if path.is_file():
                    return str(path)
            except OSError:
                continue
    return None


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出里没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _run_cli_json(exe_name: str, args: list, timeout: float) -> dict:
    exe = _find_exe(exe_name)
    if not exe:
        raise FileNotFoundError(f"未安装 {exe_name} CLI")
    command = [exe, *args, "--format", "json"]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        command = ["cmd", "/c", *command]
    proc = subprocess.run(command, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode == 2:
        raise RuntimeError(f"{exe_name} 未登录或登录已过期（{exe_name} auth login）")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:120]
        raise RuntimeError(f"{exe_name} 退出码 {proc.returncode}：{tail}")
    return _extract_json((proc.stdout or "") + "\n" + (proc.stderr or ""))


def _reset_epoch(value):
    """resetTime → epoch 秒。兼容 ISO8601（usage-bridge _iso_dt 同款）与 epoch ms/s。
    解析不出返回 None（peak 置 null，不造数据）。"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    # 纯数字 → epoch（>1e12 视为毫秒）
    try:
        num = float(text)
        if num > 0:
            return num / 1000.0 if num > 1e12 else num
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _fmt_credits(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _username() -> str:
    """qianwen auth status → user.aliyunId；失败返回 ""（不造数据）。"""
    try:
        payload = _run_cli_json("qianwen", ["auth", "status"], timeout=10.0)
        user = payload.get("user") or {}
        return str(user.get("aliyunId") or "").strip()
    except Exception:
        return ""


def _edition_key() -> str:
    v = str(os.environ.get("HERMES_BALANCE_TOKEN_PLAN_EDITION") or "").strip().lower()
    return v if v in ("individual", "team") else "individual"


def _remaining_days(edition: dict):
    v = edition.get("remainingDays")
    if v in (None, ""):
        period = edition.get("period") or {}
        v = period.get("remainingDays")
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _individual_block(edition: dict):
    """个人版：monthlyCredits total/usedPct → (remaining, pct_left, total, label)。"""
    block = edition.get("monthlyCredits") or {}
    total = block.get("total")
    if total in (None, ""):
        return None
    try:
        total_f = float(total)
    except (TypeError, ValueError):
        return None
    used_pct = block.get("usedPct")
    try:
        used_f = float(used_pct) if used_pct not in (None, "") else 0.0
    except (TypeError, ValueError):
        used_f = 0.0
    used_f = max(0.0, min(100.0, used_f))
    remaining = total_f * (1.0 - used_f / 100.0)
    return remaining, 100.0 - used_f, total_f, "剩余月额度"


def _team_block(edition: dict):
    """团队版：weeklyCredits→fiveHourCredits→seatSummary 顺序取数（结构以实测为准，
    取不到=None→ok=false 不造数据）。"""
    for key, label in (("weeklyCredits", "剩余周额度"),
                       ("fiveHourCredits", "剩余5小时额度")):
        block = edition.get(key) or {}
        total = block.get("total")
        remaining = block.get("remaining")
        if total in (None, "") or remaining in (None, ""):
            continue
        try:
            total_f, rem_f = float(total), float(remaining)
        except (TypeError, ValueError):
            continue
        if total_f <= 0:
            continue
        pct = max(0.0, min(100.0, rem_f / total_f * 100.0))
        return rem_f, pct, total_f, label
    seat = (edition.get("seatSummary") or {}).get("total") or {}
    try:
        total_f = float(seat.get("totalValue"))
        rem_f = float(seat.get("surplusValue"))
    except (TypeError, ValueError):
        return None
    if total_f <= 0:
        return None
    pct = max(0.0, min(100.0, rem_f / total_f * 100.0))
    return rem_f, pct, total_f, "剩余席位额度"


def fetch() -> dict:
    try:
        payload = _run_cli_json("qianwen", ["subscription", "status"],
                                timeout=_CLI_TIMEOUT_S)
    except FileNotFoundError:
        return {"ok": False, "error": "未安装 qianwen CLI"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"qianwen 超过 {_CLI_TIMEOUT_S:g}s 未返回"}
    except Exception as exc:
        return {"ok": False, "error": f"qianwen 输出无法解析：{exc}"[:120]}
    edition_key = _edition_key()
    edition = payload.get(edition_key) or {}
    if not isinstance(edition, dict) or \
            str(edition.get("status") or "").lower() != "active":
        label = "个人版" if edition_key == "individual" else "团队版"
        return {"ok": False, "error": f"当前账号没有生效中的 Token Plan {label}订阅"}
    block = _individual_block(edition) if edition_key == "individual" \
        else _team_block(edition)
    if block is None:
        return {"ok": False, "error": "订阅里没有可解析的额度字段"}
    remaining, pct_left, total_f, quota_label = block
    user = _username()
    tier = str(edition.get("name") or edition.get("specCode") or "").strip()
    days = _remaining_days(edition)
    lines = [
        {"text": (f"[{user}] " if user else "") + quota_label, "style": "title"},
        # 进度条修正（作者裁定）：第二行是进度条+百分比，不是文本。fraction=剩余占比。
        {"style": "bar", "fraction": round(pct_left / 100.0, 4),
         "text": f"{round(pct_left)}%"},
    ]
    info = tier
    if days is not None:
        info = (info + "  " if info else "") + f"距到期{days}天"
    if info:
        lines.append({"text": info, "style": "info"})
    result = {
        "ok": True, "error": "",
        "value": round(remaining, 2),
        "display": f"{_fmt_credits(remaining)} / {_fmt_credits(total_f)} Credits",
        "lines": lines,
        "peak": {"windows": BAILIAN_PEAK_WINDOWS,
                 "label_peak": "峰", "label_valley": "谷"},
    }
    reset_ts = _reset_epoch((edition.get("monthlyCredits") or {}).get("resetTime"))
    if reset_ts is not None:
        result["extra"] = {"reset": reset_ts}   # 月重置时间留作 extra（不占峰谷行）
    return result


if __name__ == "__main__":
    print(json.dumps(fetch(), ensure_ascii=False))