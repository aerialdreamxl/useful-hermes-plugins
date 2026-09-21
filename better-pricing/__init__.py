"""better-pricing — 独立计费查询台 + 可选向 Hermes 计费管线回报价格。

设计契约（主人 2026-09-21 定，示例配置 plugins/better-pricing.yaml）：
* 价目表来自 better-pricing.yaml（$HERMES_HOME/ 优先，插件目录兜底），不落任何
  衍生状态；查询时直读 state.db（mode=ro + query_only）现算。
* 注册 `hermes pricing-plus`（CLI 子命令）与 `/pricing-plus`（会话内 slash）。
* native_report: USD--本币价 ÷ multiply 换算成美元回报 Hermes；
                 raw--原始币种数值直接回报（列名仍 *_cost_usd，语义自便）；
                 disable--不回报，只做独立查询。
* multiply: 几个该币种计 1 美金；0 = 独立体系（如魔粒），永不回报、不入美元总额。
* calculates: token（input/output/cache 每百万token价）| calls（每次调用价）| free。
* 回报走 estimate_usage_cost 包装：命中价目表 → CostResult(status=estimated,
  source=user_override)；未命中 → 原逻辑。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
_cfg_cache: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": None}

# ---------------------------------------------------------------------------
# 配置加载（mtime 热重载：改 yaml 即时生效）
# ---------------------------------------------------------------------------

def config_path() -> Optional[Path]:
    env = os.environ.get("BETTER_PRICING_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    candidates: List[Path] = []
    try:
        from hermes_constants import get_hermes_home
        candidates.append(get_hermes_home() / "better-pricing.yaml")
    except Exception:
        pass
    candidates.append(PLUGIN_DIR / "better-pricing.yaml")
    candidates.append(PLUGIN_DIR.parent / "better-pricing.yaml")  # 开发态布局
    candidates.append(Path.cwd() / "better-pricing.yaml")
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(force: bool = False) -> Optional[dict]:
    path = config_path()
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if not force and _cfg_cache["data"] is not None and _cfg_cache["mtime"] == mtime \
            and _cfg_cache["path"] == str(path):
        return _cfg_cache["data"]
    try:
        import yaml
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("top-level YAML is not a mapping")
    except Exception as exc:
        logger.warning("better-pricing: config read failed %s: %s", path, exc)
        return _cfg_cache["data"]
    _cfg_cache.update(path=str(path), mtime=mtime, data=data)
    return data


def _provider_keys(billing_provider: str) -> Tuple[str, ...]:
    """custom:xxx 与裸 xxx 互为候选，宽松匹配价目表键。"""
    bp = (billing_provider or "").strip()
    out = [bp]
    if bp.startswith("custom:"):
        out.append(bp.split(":", 1)[1])
    elif bp:
        out.append("custom:" + bp)
    return tuple(k for k in out if k)


def find_entry(cfg: dict, billing_provider: str, model: str) -> Optional[dict]:
    """返回 {entry, key, price}（price 可为 None：free 提供商不要求列模型）。
    提供商键未命中时退一级：模型价目全局唯一 → 也算命中（billing_provider 的
    `custom:标签` 后缀与价目表键经常对不上，模型名是唯一可靠的兜底）。"""
    plist = cfg.get("price_list") or {}
    for key in _provider_keys(billing_provider):
        entry = plist.get(key)
        if not isinstance(entry, dict):
            continue
        models = entry.get("models") or {}
        price = models.get(model)
        if price is None:
            price = {str(k).lower(): v for k, v in models.items()}.get((model or "").lower())
        return {"entry": entry, "key": key, "price": price}
    # 兜底：按模型名全表搜（多提供商重价 → 不猜，视为未命中）
    found = []
    for key, entry in plist.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models") or {}
        lower = {str(k).lower(): v for k, v in models.items()}
        if (model or "").lower() in lower:
            found.append((key, entry, lower[(model or "").lower()]))
    if len(found) == 1:
        key, entry, price = found[0]
        return {"entry": entry, "key": key, "price": price}
    return None

# ---------------------------------------------------------------------------
# 单价计算（该提供商体系内的本币原价）
# ---------------------------------------------------------------------------

def native_cost(entry: dict, price: Any, counts: Dict[str, int]) -> float:
    """token: {input,output,cache[,cache_write]} 元/百万token；
       calls: 数字 = 每次调用价；free / 缺价: 0。"""
    calc = (entry.get("calculates") or "token").lower()
    if calc == "free":
        return 0.0
    if calc == "calls":
        per_call = float(price) if isinstance(price, (int, float)) else \
            float(price.get("call", price.get("input", 0)) or 0) if isinstance(price, dict) else 0.0
        return per_call * int(counts.get("api_call_count", 0) or 0)
    if not isinstance(price, dict):
        return 0.0
    inp = float(price.get("input", 0) or 0)
    out = float(price.get("output", 0) or 0)
    cache_r = float(price.get("cache", price.get("cache_read", 0)) or 0)
    cache_w = float(price.get("cache_write", cache_r) or 0)
    return (inp * int(counts.get("input_tokens", 0) or 0)
            + out * int(counts.get("output_tokens", 0) or 0)
            + cache_r * int(counts.get("cache_read_tokens", 0) or 0)
            + cache_w * int(counts.get("cache_write_tokens", 0) or 0)) / 1_000_000.0

# ---------------------------------------------------------------------------
# 用量查询（state.db 只读）
# ---------------------------------------------------------------------------

def state_db_path() -> Optional[Path]:
    env = os.environ.get("BETTER_PRICING_STATE_DB")
    if env and Path(env).is_file():
        return Path(env)
    try:
        from hermes_constants import get_hermes_home
        p = get_hermes_home() / "state.db"
        if p.is_file():
            return p
    except Exception:
        pass
    return None


_COUNT_KEYS = ("api_call_count", "input_tokens", "output_tokens",
               "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")

_USAGE_SQL = """
SELECT smu.billing_provider, smu.model, smu.task,
       SUM(smu.api_call_count) AS api_call_count,
       SUM(smu.input_tokens) AS input_tokens,
       SUM(smu.output_tokens) AS output_tokens,
       SUM(smu.cache_read_tokens) AS cache_read_tokens,
       SUM(smu.cache_write_tokens) AS cache_write_tokens,
       SUM(smu.reasoning_tokens) AS reasoning_tokens
FROM session_model_usage smu
JOIN sessions s ON s.id = smu.session_id
WHERE (? <= 0 OR s.started_at >= ?)
GROUP BY smu.billing_provider, smu.model, smu.task
ORDER BY smu.billing_provider, smu.model
"""


def fetch_usage(days: float = 0.0) -> List[sqlite3.Row]:
    db = state_db_path()
    if db is None:
        return []
    # sqlite URI 在 Windows 下要求正斜杠（D:/x），反斜杠直接打不开
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        cutoff = (time.time() - days * 86400) if days > 0 else 0
        return conn.execute(_USAGE_SQL, (cutoff, cutoff)).fetchall()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# 报表引擎（CLI 与 slash 共用；never raises）
# ---------------------------------------------------------------------------

def _compact(v: int) -> str:
    v = int(v or 0)
    if v >= 1_000_000:
        return "{:.1f}M".format(v / 1_000_000)
    if v >= 1_000:
        return "{:.0f}K".format(v / 1_000)
    return str(v)


def _fmt_money(amount: float) -> str:
    if amount == 0:
        return "0"
    if abs(amount) < 0.01:
        return "{:,.4f}".format(amount)
    return "{:,.2f}".format(amount)


HELP_TEXT = (
    "用法: hermes pricing-plus [--days N] [--provider KEY] [--aux] [--config-check]\n"
    "  独立计费报表：读 better-pricing.yaml 价目表 × state.db 实际用量，现算现出。\n"
    "  --days N        只看最近 N 天（默认全部时间）\n"
    "  --provider -p   按 billing_provider 子串过滤（如 dashscope）\n"
    "  --aux           包含辅助模型调用（压缩/vision 等）\n"
    "  --config-check  校验价目表可读并列出提供商\n"
    "会话内同名命令: /pricing-plus [相同参数]\n"
    "配置: better-pricing.yaml（$HERMES_HOME/ 或插件目录；改动即时生效，无需重启）"
)


def build_report(raw_args: str = "") -> str:
    days, show_aux, provider_filter = 0.0, False, ""
    toks = (raw_args or "").split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("--days", "-d") and i + 1 < len(toks):
            try:
                days = float(toks[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if t == "--aux":
            show_aux = True
            i += 1
            continue
        if t in ("--provider", "-p") and i + 1 < len(toks):
            provider_filter = toks[i + 1]
            i += 2
            continue
        if t in ("--help", "-h"):
            return HELP_TEXT
        i += 1

    cfg = load_config()
    if cfg is None:
        return ("better-pricing: 未找到 better-pricing.yaml"
                "（$BETTER_PRICING_CONFIG → $HERMES_HOME/ → 插件目录 → 当前目录）")
    rows = [r for r in fetch_usage(days)
            if show_aux or (r["task"] or "main") == "main"]
    if provider_filter:
        rows = [r for r in rows if provider_filter in (r["billing_provider"] or "")]
    if not rows:
        return "better-pricing: 窗口内没有匹配的用量记录（--days N 调窗口，--aux 含辅助调用）"

    report_mode = str(cfg.get("native_report") or "USD").lower()
    window = "全部时间" if days <= 0 else "最近 {} 天".format(
        int(days) if days == int(days) else days)

    # 聚合 (provider, model) → 计数
    agg: Dict[Tuple[str, str], Dict[str, int]] = {}
    for r in rows:
        a = agg.setdefault((r["billing_provider"] or "?", r["model"]),
                           {k: 0 for k in _COUNT_KEYS})
        for k in _COUNT_KEYS:
            a[k] += (r[k] or 0)

    lines: List[str] = [
        "better-pricing · 窗口 {} · 报账模式 native_report={}".format(window, report_mode),
        "  {:<26} {:<24} {:>6} {:>8} {:>8} {:>12}  {:>16}".format(
            "PROVIDER", "MODEL", "CALLS", "IN", "OUT", "CACHE-R/W", "COST"),
        "  " + "-" * 100,
    ]
    usd_equiv = 0.0            # 供参考的美元等值（mult>0 条目）
    currency_subtotals: Dict[str, float] = {}
    iso_rows: List[str] = []
    priced = unpriced = 0

    for (prov, model), a in sorted(agg.items()):
        hit = find_entry(cfg, prov, model)
        if hit is None:
            unpriced += 1
            cost_cell = "无价目"
        else:
            entry, price = hit["entry"], hit["price"]
            calc = (entry.get("calculates") or "token").lower()
            currency = str(entry.get("currency") or "?")
            mult = float(entry.get("multiply") or 0)
            native = native_cost(entry, price, a)
            # 自有界面：永远按配置币种显示；USD 换算只发生在向 Hermes 报账时
            if calc == "free":
                priced += 1
                cost_cell = "free"
            elif mult <= 0:
                priced += 1
                iso_rows.append("{}/{} ≈ {} {}".format(
                    prov, model, _fmt_money(native), currency))
                cost_cell = "{} {} (独立)".format(_fmt_money(native), currency)
            else:
                priced += 1
                currency_subtotals[currency] = currency_subtotals.get(currency, 0.0) + native
                usd_equiv += native / mult
                cost_cell = "{} {}".format(_fmt_money(native), currency)
        lines.append("  {:<26} {:<24} {:>6} {:>8} {:>8} {:>12}  {:>16}".format(
            prov[:26], model[:24], "{:,}".format(a["api_call_count"]),
            _compact(a["input_tokens"]), _compact(a["output_tokens"]),
            "{}/{}".format(_compact(a["cache_read_tokens"]), _compact(a["cache_write_tokens"])),
            cost_cell))

    lines.append("")
    for curcy, amt in sorted(currency_subtotals.items()):
        lines.append("小计 [{}]: {}".format(curcy, _fmt_money(amt)))
    if currency_subtotals:
        lines.append("美元等值（仅用于向 Hermes 报账）: USD {}".format(_fmt_money(usd_equiv)))
    for s in iso_rows:
        lines.append("独立体系不计入总额: " + s)
    lines.append("有价 {} · 无价目 {} · 行 {}".format(priced, unpriced, len(agg)))
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 回报 Hermes 计费管线（estimate_usage_cost 包装；按 native_report 档位）
# ---------------------------------------------------------------------------

_INJECT_STATE = {"patched": False, "orig": None}


def _pricing_from_config(model: str, provider: Optional[str], usage: Any) -> Optional[Any]:
    """命中价目表且允许回报 → CostResult；否则 None（走原逻辑）。"""
    cfg = load_config()
    if cfg is None:
        return None
    mode = str(cfg.get("native_report") or "USD").lower()
    if mode == "disable":
        return None
    hit = find_entry(cfg, provider or "", model)
    if hit is None:
        return None
    entry, price = hit["entry"], hit["price"]
    calc = (entry.get("calculates") or "token").lower()
    mult = float(entry.get("multiply") or 0)
    if calc != "free" and (price is None or mult <= 0):
        return None  # 缺模型价 / 独立体系：不回报，走原逻辑
    counts = {
        "api_call_count": getattr(usage, "request_count", 1),
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_tokens": getattr(usage, "cache_read_tokens", 0),
        "cache_write_tokens": getattr(usage, "cache_write_tokens", 0),
    }
    native = native_cost(entry, price, counts)
    if calc == "free" or mode == "raw":
        amount = Decimal(str(native))
    else:
        amount = Decimal(str(native / mult)) if mult > 0 else None
    if amount is None:
        return None
    from agent.usage_pricing import CostResult, format_cost_label
    currency = str(entry.get("currency") or "")
    return CostResult(
        amount_usd=amount, status="estimated", source="user_override",
        label=format_cost_label(amount),
        pricing_version="better-pricing:{}".format(currency or mode),
        notes=("better-pricing native_report={} currency={} mult={}".format(mode, currency, mult),))


def install_report_hook() -> bool:
    """patch usage_pricing.estimate_usage_cost + 顶层 from-import 绑定点。幂等。"""
    if _INJECT_STATE["patched"]:
        return True
    try:
        import agent.usage_pricing as up
    except Exception:
        return False
    orig = up.estimate_usage_cost
    _INJECT_STATE["orig"] = orig

    def wrapper(model_name, usage, *, provider=None, base_url=None, api_key=None, **kw):
        # insights 等离线消费者传 usage 的形态是 token 计数 dict 而非 CanonicalUsage
        if usage is None and isinstance(kw.get("usage"), dict):
            from agent.usage_pricing import CanonicalUsage as _CU
            usage = _CU(**{k: kw["usage"].get(k, 0) or 0
                           for k in ("input_tokens", "output_tokens",
                                     "cache_read_tokens", "cache_write_tokens")})
        try:
            injected = _pricing_from_config(model_name, provider, usage)
        except Exception as exc:  # fail-open：绝不打断会话回合
            logger.debug("better-pricing inject failed: %s", exc)
            injected = None
        if injected is not None:
            return injected
        return orig(model_name, usage, provider=provider, base_url=base_url, api_key=api_key, **kw)

    up.estimate_usage_cost = wrapper
    for mod_name in ("agent.turn_usage", "agent.insights"):
        try:
            m = __import__(mod_name, fromlist=["estimate_usage_cost"])
            if getattr(m, "estimate_usage_cost", None) is orig:
                setattr(m, "estimate_usage_cost", wrapper)
        except Exception:
            pass
    _INJECT_STATE["patched"] = True
    logger.info("better-pricing: report hook installed (native_report=%s)",
                (load_config() or {}).get("native_report"))
    return True

# ---------------------------------------------------------------------------
# CLI / slash 注册
# ---------------------------------------------------------------------------

def _cli_setup(parser) -> None:
    parser.add_argument("--days", "-d", type=float, default=0.0, help="窗口天数（0=全部）")
    parser.add_argument("--provider", "-p", default="", help="billing_provider 子串过滤")
    parser.add_argument("--aux", action="store_true", help="包含辅助调用")
    parser.add_argument("--config-check", action="store_true", help="校验价目表可读")


def _cli_handler(args) -> int:
    if getattr(args, "config_check", False):
        cfg = load_config(force=True)
        path = config_path()
        if cfg is None:
            print("better-pricing: 未找到配置文件")
            return 1
        plist = cfg.get("price_list") or {}
        print("配置 OK: {}".format(path))
        print("native_report={} · 提供商 {} 个:".format(cfg.get("native_report"), len(plist)))
        for key, entry in plist.items():
            if isinstance(entry, dict):
                print("  {}: calculates={} currency={} multiply={} models={}".format(
                    key, entry.get("calculates"), entry.get("currency"),
                    entry.get("multiply"), len(entry.get("models") or {})))
        return 0
    extra: List[str] = []
    if getattr(args, "days", 0):
        extra += ["--days", str(args.days)]
    if getattr(args, "provider", ""):
        extra += ["--provider", args.provider]
    if getattr(args, "aux", False):
        extra.append("--aux")
    print(build_report(" ".join(extra)))
    return 0


def _slash_handler(raw_args: str = "", **kwargs):
    return build_report(raw_args)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="pricing-plus", help="独立计费报表（better-pricing.yaml × state.db 现算）",
        setup_fn=_cli_setup, handler_fn=_cli_handler,
        description="Read-only cost desk: renders per-provider/model token & call spend from "
                    "your own price list; optional report-back into Hermes billing.")
    ctx.register_command(
        name="pricing-plus", handler=_slash_handler,
        args_hint="[--days N] [--provider KEY] [--aux]",
        description="独立计费报表（同 `hermes pricing-plus`）")
    ok = install_report_hook()
    if not ok:
        try:
            ctx.register_hook("pre_llm_call", _lazy_install_hook)
        except Exception:
            pass


def _lazy_install_hook(**kwargs):
    if not _INJECT_STATE["patched"]:
        install_report_hook()
    return None
