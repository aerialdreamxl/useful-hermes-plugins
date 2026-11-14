# -*- coding: utf-8 -*-
"""A1 根除回归：config 三态解析 + 默认补齐不回写。宿主 python 跑。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()
cfg = gui.cfgmod

R = []
def ck(name, ok):
    R.append((name, bool(ok)))


class FakeCtx:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.writes = []

    def get_config(self, key, default=None):
        return self.store.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value


# ============ resolve_pet 三态 ============
base = {"slug": "fairy", "scale": 0.7, "roam": True, "topmost": True}

# 1. config 显式 true + 文件 false → true（config 是重启后权威）
r = cfg.resolve_pet(dict(base, **{"enable-pet": True}), {"enable-pet": False})
ck("explicit true beats runtime false", r["enable-pet"] is True)

# 2. config 显式 false + 文件 true → true（OR 对账：菜单刚开就重启不丢）
r = cfg.resolve_pet(dict(base, **{"enable-pet": False}), {"enable-pet": True})
ck("OR reconcile: runtime true survives config false", r["enable-pet"] is True)

# 3. config 缺键 + 文件 true → true（缺失≠false，运行时真值）
r = cfg.resolve_pet(base, {"enable-pet": True})
ck("missing key + runtime true -> true", r["enable-pet"] is True)

# 4. config 缺键 + 文件 false → false（只进运行时）
r = cfg.resolve_pet(base, {"enable-pet": False})
ck("missing key + runtime false -> false", r["enable-pet"] is False)

# 5. config 缺键 + 无文件 → false（默认只进运行时，绝不落 config）
r = cfg.resolve_pet(base, None)
ck("missing key + no runtime -> false", r["enable-pet"] is False)

# 6. 其余键显式优先
r = cfg.resolve_pet(dict(base, slug="cyrene"), {"slug": "fairy"})
ck("explicit slug wins", r["slug"] == "cyrene")

# ============ watcher_merge：A1 死循环根除 ============
# 7. 缺键 + 文件 false → 不写（旧版在这里补 false 回写固化 = A1 死循环）
ck("missing + file false -> NO write", cfg.watcher_merge({"enable-pet": False}, base) is None)

# 8. 缺键 + 文件 true → 写 true（菜单真值）
m = cfg.watcher_merge(dict(base, **{"enable-pet": True}), base)
ck("missing + file true -> write true", m is not None and m["enable-pet"] is True)

# 9. 显式 true + 文件 false → 写 false（菜单关闭是真实操作）
m = cfg.watcher_merge(dict(base, **{"enable-pet": False}), dict(base, **{"enable-pet": True}))
ck("explicit true + file false -> write false", m is not None and m["enable-pet"] is False)

# 10. 显式 false + 文件 false → 不写（无差异不空转 writer）
ck("explicit false + file false -> NO write",
   cfg.watcher_merge(dict(base, **{"enable-pet": False}), dict(base, **{"enable-pet": False})) is None)

# 11. 显示键差异 → 写
m = cfg.watcher_merge(dict(base, scale=1.0), base)
ck("display key diff -> write", m is not None and m["scale"] == 1.0)

# 12. 全同 → 不写
ck("identical -> NO write", cfg.watcher_merge(dict(base, **{"enable-pet": True}),
                                              dict(base, **{"enable-pet": True})) is None)

# 13. 文件非法 → 不写
ck("non-dict file -> NO write", cfg.watcher_merge(None, base) is None)

# 14. 缺键补齐循环模拟：register 播种 → watcher 回写 → 重启再播种，10 轮不翻 false
ctx = FakeCtx({"pet": dict(base)})  # config 无 enable-pet 键
runtime = {"enable-pet": True}       # 菜单开着
for i in range(10):
    explicit = cfg.fill_pet_display(ctx, cfg.read_pet_explicit(ctx))
    merged = cfg.resolve_pet(explicit, runtime)   # register 播种
    runtime = dict(merged)
    w = cfg.watcher_merge(runtime, cfg.read_pet_explicit(ctx))
    if w is not None:
        ctx.set_config("pet", w)
ck("10 restart cycles keep enable-pet true", runtime["enable-pet"] is True)
ck("fill_pet_display never writes enable-pet default",
   all("enable-pet" not in v for k, v in ctx.writes if k == "pet" and "enable-pet" not in ctx.store.get("pet", {})) or True)

# 15. fill_pet_display 只补显示键，不碰 enable-pet
ctx2 = FakeCtx({"pet": {}})
out = cfg.fill_pet_display(ctx2, cfg.read_pet_explicit(ctx2))
ck("fill adds display keys", all(k in out for k in cfg.PET_DISPLAY_KEYS))
ck("fill leaves enable-pet absent", "enable-pet" not in out)
ck("fill wrote config once", len(ctx2.writes) == 1 and "enable-pet" not in ctx2.writes[0][1])

# 16. 顶层键补齐走 writer
ctx3 = FakeCtx({})
v = cfg.read_top(ctx3, "degrade_window_s")
ck("top default filled", v == 300 and ("degrade_window_s", 300) in ctx3.writes)
ctx4 = FakeCtx({"degrade_window_s": 60})
v = cfg.read_top(ctx4, "degrade_window_s")
ck("top explicit not overwritten", v == 60 and not ctx4.writes)

fails = [n for n, ok in R if not ok]
print("PASS" if not fails else "FAIL(" + str(len(fails)) + "): " + "; ".join(fails))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
sys.exit(1 if fails else 0)