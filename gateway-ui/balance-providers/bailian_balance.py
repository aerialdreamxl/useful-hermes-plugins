# -*- coding: utf-8 -*-
"""百炼账户余额 binding（gateway-ui 出厂余额适配器，CONTRACT §10.3）。

可删、可改、可加：本目录（<home>/balance-providers/）每个 *.py 是一个余额源。
删掉本文件=气泡不再显示百炼账户余额。
凭证：qianwen CLI 自己的登录态（`qianwen auth login`），本 binding 不碰任何密钥；
可用环境变量 HERMES_QIANWEN_PATH 覆盖 CLI 路径。

数据源：qianwen billing balance summary --format json（照抄 usage-bridge
_run_cli_json 实测命令行）。快源（3s 级），无峰谷概念 → peak=None。
CLI 未安装 → ok=False error="未安装 qianwen CLI"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

NAME = "百炼账户余额"
UNIT = "CNY"
ID = "bailian_balance"

_CLI_TIMEOUT_S = 8.0


def _find_exe(name: str):
    """定位 CLI：环境变量覆盖 → PATH → npm 全局目录兜底（照抄 usage-bridge）。"""
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


def fetch() -> dict:
    try:
        payload = _run_cli_json("qianwen", ["billing", "balance", "summary"],
                                timeout=_CLI_TIMEOUT_S)
    except FileNotFoundError:
        return {"ok": False, "error": "未安装 qianwen CLI"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"qianwen 超过 {_CLI_TIMEOUT_S:g}s 未返回"}
    except Exception as exc:
        return {"ok": False, "error": f"qianwen 输出无法解析：{exc}"[:120]}
    amount = payload.get("availableAmount")
    if amount in (None, ""):
        return {"ok": False, "error": "余额接口未返回 availableAmount"}
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "availableAmount 非数值"}
    currency = str(payload.get("currency") or UNIT).strip() or UNIT
    symbol = "¥" if currency == "CNY" else ("$" if currency == "USD" else currency + " ")
    return {
        "ok": True, "error": "",
        "value": value,
        "display": f"{symbol} {value:.2f}",
        "peak": None,   # 账户余额无峰谷概念
    }


if __name__ == "__main__":
    print(json.dumps(fetch(), ensure_ascii=False))