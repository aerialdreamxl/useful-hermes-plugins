# -*- coding: utf-8 -*-
"""DeepSeek 官方余额 binding（gateway-ui 出厂余额适配器，CONTRACT §10.3）。

可删、可改、可加：本目录（<home>/balance-providers/）每个 *.py 是一个余额源，
宿主周期在隔离子进程里跑 fetch()。删掉本文件=气泡不再显示 DeepSeek。
密钥环境变量：DEEPSEEK_API_KEY（进程环境优先，缺则读 <home>/.env）。
宿主配置面永不过明文；view/日志的 sk- 样式串由宿主统一打码。

数据源：GET https://api.deepseek.com/user/balance（usage-bridge 实测同款）。
峰谷：接口若带 is_peak/peak 字段 → 透传 server_authoritative=True；
否则交内置规则 ds_cn（北京时间工作日 9-12/14-18 峰，周末/节假日谷），
边界由宿主/worker 用 peak_rules 推算。接口无今日用量字段 → extra 省略（不造数据）。
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

NAME = "DeepSeek"
UNIT = "CNY"
ID = "deepseek"

_URL = "https://api.deepseek.com/user/balance"
_HTTP_TIMEOUT_S = 6.0


def _home() -> Path:
    env = os.environ.get("HERMES_HOME", "")
    return Path(env) if env else Path.home() / ".hermes"


def _dotenv() -> dict:
    values = {}
    try:
        for line in (_home() / ".env").read_text(
                encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, _, value = text.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _api_key() -> str:
    v = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    return v or str(_dotenv().get("DEEPSEEK_API_KEY") or "").strip()


def fetch() -> dict:
    key = _api_key()
    if not key:
        return {"ok": False, "error": "缺凭证：DEEPSEEK_API_KEY 未设置"}
    req = urllib.request.Request(
        _URL, headers={"Authorization": f"Bearer {key}",
                       "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": f"余额接口调用失败：{exc}"[:120]}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "余额接口返回非对象"}
    infos = payload.get("balance_infos") or []
    if not infos or not isinstance(infos[0], dict):
        return {"ok": False, "error": "接口未返回 balance_infos"}
    info = infos[0]
    total = info.get("total_balance")
    if total in (None, ""):
        return {"ok": False, "error": "接口未返回 total_balance"}
    try:
        value = float(total)
    except (TypeError, ValueError):
        return {"ok": False, "error": "total_balance 非数值"}
    unit = str(info.get("currency") or UNIT).strip() or UNIT
    symbol = "¥" if unit == "CNY" else ("$" if unit == "USD" else unit + " ")
    result = {
        "ok": True, "error": "",
        "value": value,
        "display": f"{symbol} {value:.2f}",
    }
    # 峰谷：服务端字段优先（is_peak 顶层或 peak 块），否则交 ds_cn 规则
    peak = None
    server_peak = payload.get("peak")
    if isinstance(server_peak, dict) and isinstance(server_peak.get("is_peak"), bool):
        peak = dict(server_peak)
        peak["server_authoritative"] = True
    elif isinstance(payload.get("is_peak"), bool):
        peak = {"is_peak": payload["is_peak"], "server_authoritative": True}
    if peak is None:
        peak = {"rule": "ds_cn"}
    holidays = payload.get("peak_holidays") or payload.get("holidays")
    if isinstance(holidays, list) and holidays:
        peak["holidays"] = [str(h) for h in holidays]
    result["peak"] = peak
    # extra.today：接口没有今日用量字段就不造（不许自造数据）
    today = payload.get("today_usage") or payload.get("today_cost")
    if today not in (None, ""):
        result["extra"] = {"today": f"今日已用 {symbol} {today}"}
    return result


if __name__ == "__main__":
    print(json.dumps(fetch(), ensure_ascii=False))