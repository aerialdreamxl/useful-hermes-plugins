"""better-pricing 回归探针：N26（缺价模型被按 0 元计入有价）+ F16（全局 monkey-patch
无卸载 / Path.cwd() 价格表兜底）。

用法: python probe_n26_f16.py     （退出码 0 = 全过）
只读探针：不写配置、不碰 state.db、不改插件文件。
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(r"D:\01-AI\Hermes-Agent\Data\hermes-agent")
PLUGIN_INIT = Path(r"D:\01-AI\Hermes-Agent\Data\plugins\custom-plugins\better-pricing\__init__.py")
sys.path.insert(0, str(HERMES_ROOT))

FAILS = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print("{} {}{}".format("PASS" if cond else "FAIL", name, (" — " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def load_plugin(tag="bp_under_test"):
    """按路径加载插件模块（每次都是全新模块对象，用于模拟热重载）。"""
    spec = importlib.util.spec_from_file_location(tag, str(PLUGIN_INIT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[tag] = mod
    spec.loader.exec_module(mod)
    return mod


CFG = {
    "native_report": "USD",
    "price_list": {
        # 有 models 但缺目标模型（N26 案发现场）
        "custom:alpha": {
            "calculates": "token", "currency": "CNY", "multiply": 7.2,
            "models": {"alpha-model": {"input": 2.0, "output": 8.0}},
        },
        # free 提供商：不要求列模型
        "freehouse": {"calculates": "free", "currency": "", "multiply": 0, "models": {}},
    },
}


def probe_find_entry(mod):
    print("\n== find_entry 三态 ==")
    hit = mod.find_entry(CFG, "custom:alpha", "alpha-missing")
    check("provider 键命中但缺该模型 → None（不再 price=None）", hit is None, repr(hit))

    hit = mod.find_entry(CFG, "freehouse", "whatever-model")
    check("free 提供商 → 命中且 price=None",
          hit is not None and hit["price"] is None and hit["key"] == "freehouse", repr(hit))

    hit = mod.find_entry(CFG, "ghost", "ghost-model")
    check("完全未命中 → None", hit is None, repr(hit))

    # 回归：正常有价目 / 大小写 / custom: 后缀兜底 都不能坏
    hit = mod.find_entry(CFG, "custom:alpha", "alpha-model")
    check("正常有价目仍命中且 price 非空",
          hit is not None and isinstance(hit["price"], dict) and hit["price"]["input"] == 2.0,
          repr(hit and hit["price"]))
    hit = mod.find_entry(CFG, "custom:alpha", "ALPHA-MODEL")
    check("大小写不敏感仍生效", hit is not None and hit["price"]["output"] == 8.0, repr(hit and hit["price"]))
    hit = mod.find_entry(CFG, "alpha", "alpha-model")   # 裸键 → custom:alpha
    check("custom: 后缀兜底仍生效", hit is not None and hit["key"] == "custom:alpha", repr(hit and hit["key"]))

    # 全表按模型名搜：唯一命中要有价目；显式空值不算命中
    cfg2 = {"price_list": {"p1": {"calculates": "token", "models": {"only-model": {"input": 1}}},
                           "p2": {"calculates": "token", "models": {"null-model": None}}}}
    hit = mod.find_entry(cfg2, "unrelated", "only-model")
    check("全表唯一命中 → 命中", hit is not None and hit["key"] == "p1", repr(hit))
    hit = mod.find_entry(cfg2, "unrelated", "null-model")
    check("yaml 里价为空（None）不算命中", hit is None, repr(hit))
    cfg3 = {"price_list": {"p1": {"calculates": "token", "models": {"dup": {"input": 1}}},
                           "p2": {"calculates": "token", "models": {"dup": {"input": 2}}}}}
    check("全表重价 → 不猜，未命中", mod.find_entry(cfg3, "unrelated", "dup") is None)

    # 提供商条目存在但缺该模型 → 不许从别家借价（实测 glm-5.2 曾被 token-plan 顶价）
    cfg4 = {"price_list": {
        "custom:common": {"calculates": "token", "currency": "CNY", "models": {"a": {"input": 1}}},
        "custom:token-plan": {"calculates": "token", "currency": "CNY",
                              "models": {"borrowed": {"input": 8, "output": 28}}},
    }}
    hit = mod.find_entry(cfg4, "custom:common", "borrowed")
    check("本家有价目条但缺该模型 → 不跨提供商借价（None）", hit is None, repr(hit))
    hit = mod.find_entry(cfg4, "unknown-provider", "borrowed")
    check("提供商键完全对不上时全局兜底仍生效", hit is not None and hit["key"] == "custom:token-plan",
          repr(hit and hit["key"]))


def probe_report(mod):
    print("\n== build_report 计数口径 ==")
    rows = [
        {"billing_provider": "custom:alpha", "model": "alpha-model", "task": "main",
         "api_call_count": 10, "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0},
        {"billing_provider": "custom:alpha", "model": "alpha-missing", "task": "main",
         "api_call_count": 5, "input_tokens": 1000, "output_tokens": 100,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0},
        {"billing_provider": "freehouse", "model": "any-model", "task": "main",
         "api_call_count": 3, "input_tokens": 500, "output_tokens": 50,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0},
        {"billing_provider": "ghost", "model": "ghost-model", "task": "main",
         "api_call_count": 1, "input_tokens": 1, "output_tokens": 1,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0},
    ]
    mod.load_config = lambda force=False: CFG
    mod.fetch_usage = lambda days=0.0: rows
    out = mod.build_report("")
    print(out)
    check("无价目计数 = 2（缺价模型 + 完全未命中）", "无价目 2" in out,
          [ln for ln in out.splitlines() if "有价" in ln])
    check("有价计数 = 2（有价目 + free）", "有价 2" in out)
    free_line = [ln for ln in out.splitlines() if "freehouse" in ln]
    check("free 行仍显示 free", bool(free_line) and "free" in free_line[0],
          free_line[0] if free_line else "缺行")
    miss_line = [ln for ln in out.splitlines() if "alpha-missing" in ln]
    check("缺价行显示 无价目", bool(miss_line) and "无价目" in miss_line[0],
          miss_line[0] if miss_line else "缺行")
    check("缺价行不再出现 0 CNY", not (miss_line and "0 CNY" in miss_line[0]))


def probe_config_path(mod):
    print("\n== config_path 不读 Path.cwd() ==")
    tmp = Path(tempfile.mkdtemp())
    bait = tmp / "better-pricing.yaml"
    bait.write_text("native_report: DISABLE\nprice_list: {}\n", encoding="utf-8")
    old = os.getcwd()
    os.chdir(str(tmp))
    try:
        got = mod.config_path()
    finally:
        os.chdir(old)
    check("cwd 里的 better-pricing.yaml 不会被采信",
          got is None or Path(got).resolve() != bait.resolve(), repr(got))
    src = [ln for ln in PLUGIN_INIT.read_text(encoding="utf-8").splitlines()
           if not ln.lstrip().startswith("#")]
    check("源码里已无 Path.cwd() 调用（注释不算）",
          not any("Path.cwd()" in ln for ln in src))


def probe_patch_lifecycle(mod):
    print("\n== monkey-patch 安装 / 还原 ==")
    import agent.usage_pricing as up
    orig = up.estimate_usage_cost
    orig_id = id(orig)
    print("原函数 id={:#x}".format(orig_id))

    check("install 成功", mod.install_report_hook() is True)
    wrapper = up.estimate_usage_cost
    check("装上后已替换", wrapper is not orig, "id={:#x}".format(id(wrapper)))
    check("wrapper 记着原函数", getattr(wrapper, "__bp_orig__", None) is orig)
    check("重复 install 幂等（不叠加）",
          mod.install_report_hook() is True and up.estimate_usage_cost is wrapper)

    check("uninstall 返回 True", mod.uninstall_report_hook() is True)
    check("还原后 is 原引用", up.estimate_usage_cost is orig,
          "now id={:#x}".format(id(up.estimate_usage_cost)))
    check("还原后 id() 与原引用一致", id(up.estimate_usage_cost) == orig_id)
    check("重复 uninstall 幂等且不误伤", mod.uninstall_report_hook() is False
          and up.estimate_usage_cost is orig)

    # 第二轮：install → uninstall 后仍等价（不残留代数）
    mod.install_report_hook()
    mod.uninstall_report_hook()
    check("二轮安装/还原后 id 仍一致", id(up.estimate_usage_cost) == orig_id)

    # 热重载：核心 force re-discovery 会清 sys.modules 再 import 本模块（新 _INJECT_STATE）
    mod.install_report_hook()
    gen2 = load_plugin("bp_under_test_gen2")
    gen2.install_report_hook()
    check("热重载后重装不 wrapper 套 wrapper",
          getattr(up.estimate_usage_cost, "__bp_orig__", None) is orig,
          "orig 链指向 0x%x" % id(getattr(up.estimate_usage_cost, "__bp_orig__", None)))
    gen2.uninstall_report_hook()
    check("热重载代 uninstall 也还原到原引用", id(up.estimate_usage_cost) == orig_id)
    check("旧代 uninstall 幂等返回 False", mod.uninstall_report_hook() is False
          and up.estimate_usage_cost is orig)

    # atexit 已登记（进程退出兜底）
    check("已登记 atexit 兜底还原", mod._ATEXIT_STATE["registered"] is True)


def probe_wrapper_behavior(mod):
    print("\n== wrapper 行为（fail-open / 回报） ==")
    import agent.usage_pricing as up
    mod.load_config = lambda force=False: dict(CFG, native_report="USD")
    orig = up.estimate_usage_cost
    mod.install_report_hook()
    try:
        CU = up.CanonicalUsage
        usage = CU(input_tokens=1_000_000, output_tokens=0,
                   cache_read_tokens=0, cache_write_tokens=0)
        res = up.estimate_usage_cost("alpha-model", usage, provider="custom:alpha")
        print("命中价目 →", res.status, res.source, res.amount_usd)
        check("命中价目走 estimated/user_override",
              res.source == "user_override" and str(res.amount_usd).startswith("0.2777"),
              "amount={}".format(res.amount_usd))
        res2 = up.estimate_usage_cost("alpha-missing", usage, provider="custom:alpha")
        print("缺价模型 →", res2.status, res2.source, res2.amount_usd)
        check("缺价模型不回报 0 元（走核心原逻辑）",
              res2.source != "user_override", "source={}".format(res2.source))
    finally:
        mod.uninstall_report_hook()
    check("探针收尾后核心未残留补丁", up.estimate_usage_cost is orig)


class _FakeCtx:
    """最小 PluginContext 替身：记录 register 的注册项，按 ledger 语义反序 dispose。"""

    def __init__(self):
        self.cli, self.slash, self.hooks, self.unloads = [], [], [], []

    def register_cli_command(self, **kw):
        self.cli.append(kw["name"])

    def register_command(self, **kw):
        self.slash.append(kw["name"])

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def on_unload(self, callback):
        self.unloads.append(callback)

    def dispose_all(self):
        for cb in reversed(self.unloads):
            cb()
        self.unloads = []


def probe_register_unload(mod):
    print("\n== register → on_unload 真卸载路径 ==")
    import agent.usage_pricing as up
    orig = up.estimate_usage_cost
    orig_id = id(orig)
    ctx = _FakeCtx()
    mod.register(ctx)
    print("register 注册项:", ctx.cli, ctx.slash, ctx.hooks, [cb.__name__ for cb in ctx.unloads])
    check("register 挂上了 on_unload 回调", [cb.__name__ for cb in ctx.unloads] == ["uninstall_report_hook"])
    check("register 后核心已打补丁", up.estimate_usage_cost is not orig)
    ctx.dispose_all()      # 等价于禁用插件/强制重发现的 ledger dispose
    check("on_unload 回调还原核心 → is 原引用", up.estimate_usage_cost is orig,
          "now id={:#x}".format(id(up.estimate_usage_cost)))
    check("on_unload 还原后 id() 一致", id(up.estimate_usage_cost) == orig_id)
    check("再触发一次也不炸（幂等）", (ctx.unloads and all(cb() is False for cb in ctx.unloads)) or True)


def main():
    mod = load_plugin()
    probe_find_entry(mod)
    probe_report(mod)
    probe_config_path(mod)
    probe_patch_lifecycle(mod)
    probe_wrapper_behavior(mod)
    probe_register_unload(mod)
    print("\n== 汇总: {} 项失败 ==".format(len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
