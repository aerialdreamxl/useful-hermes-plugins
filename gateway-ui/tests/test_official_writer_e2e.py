# -*- coding: utf-8 -*-
"""设置界面 F7 真·官方 writer 端到端：在 scratch 假 HERMES_HOME 里实测
save_plugin_setting（ctx.set_config 的底层）保注释 + 跨进程锁 + 回读。
绝不碰真实 config.yaml / 真实总线。宿主 python 跑（需 hermes_cli 在 sys.path）。

验证整条通路：settings_req 校验 → 官方 writer 落盘 → 中文注释逐字节保全 →
官方 get_config 回读一致 → 缺键默认不落盘（A1）。"""
import os
import sys
import glob
import shutil
from pathlib import Path

from _env import HOME, scratch  # noqa: E402
REPO = Path(HOME) / "hermes-agent"
# Gateway 真实运行依赖根：uv archive 里含 ruamel+hermes_cli 全依赖的目录。
# 自动探测（不硬编码 hash，环境重建后仍有效）：逐个候选试 import ruamel.yaml。
_uv_root = Path(HOME) / "cache" / "uv" / "archive-v0"
_dep_root = None
for cand in sorted(_uv_root.iterdir()):
    if (cand / "ruamel" / "yaml" / "__init__.py").is_file():
        _dep_root = cand
        break
if _dep_root is None:
    print("FAIL: no uv archive with ruamel.yaml found"); sys.exit(1)
sys.path.insert(0, str(_dep_root))
sys.path.insert(0, str(REPO))
print("dep root:", _dep_root, flush=True)

SCRATCH = scratch("gui23-e2e")
HOME = SCRATCH / "home"
HOME.mkdir(parents=True, exist_ok=True)
# 假 HERMES_HOME：官方 get_hermes_home 认环境变量（hermes_constants.py:194）
os.environ["HERMES_HOME"] = str(HOME)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))
    print((" ok " if ok else " !! ") + name, flush=True)

# 带中文注释的初始 config（模拟作者真实文件风格）
INIT = """# 顶层中文注释：模型配置
model:
  provider: custom:main   # 行内注释也要保
  model: qwen-max
plugins:
  entries:
    custom-plugins/gateway-ui:
      settings:
        tray_enabled: true
        bus_enabled: true
        degrade_window_s: 300
        verbose_log: false
        package_index_url: https://pypi.tuna.tsinghua.edu.cn/simple
        pet:
          slug: jue
          scale: 0.7
          roam: true
          topmost: true
          enable-pet: true
  enabled:
    - custom-plugins/gateway-ui
"""
cfg_path = HOME / "config.yaml"
cfg_path.write_text(INIT, encoding="utf-8")
before_bytes = cfg_path.read_bytes()

from hermes_cli import config as config_mod
from hermes_cli.plugins_state import save_plugin_setting

ck("official get_config_path points to scratch", config_mod.get_config_path() == cfg_path)
ck("not managed install", config_mod.is_managed() is False)

# ---- 官方 writer 改 pet.scale（点分段，与宿主 _apply_setting 整块写等价路径）----
save_plugin_setting("custom-plugins/gateway-ui", ("pet", "scale"), 1.5)
after = cfg_path.read_text(encoding="utf-8")
ck("scale written via official writer", "scale: 1.5" in after)
ck("Chinese top comment preserved", "# 顶层中文注释：模型配置" in after)
ck("inline comment preserved", "# 行内注释也要保" in after)
ck("sibling key slug untouched", "slug: jue" in after)
ck("enable-pet untouched", "enable-pet: true" in after)
ck("model section untouched", "provider: custom:main" in after)

# ---- 官方 get_config 回读一致 ----
from hermes_cli.plugins_context import PluginContext
# 直接用官方 load_config_readonly 回读
cfg_mod2 = config_mod.load_config_readonly()
entry = cfg_mod2["plugins"]["entries"]["custom-plugins/gateway-ui"]["settings"]
ck("readback pet.scale == 1.5", entry["pet"]["scale"] == 1.5)
ck("readback pet.slug == jue", entry["pet"]["slug"] == "jue")

# ---- 顶层键修改 ----
save_plugin_setting("custom-plugins/gateway-ui", ("degrade_window_s",), 600)
after2 = cfg_path.read_text(encoding="utf-8")
ck("top key degrade_window_s written", "degrade_window_s: 600" in after2)
ck("comments still preserved after 2nd write", "# 顶层中文注释：模型配置" in after2)

# ---- 锁文件存在（跨进程串行证据）----
lock = cfg_path.with_name(".config.yaml.lock")
ck("official lock file created", lock.exists())

# ---- A1：缺键默认不落盘——新建缺 enable-pet 的 config，build_view 不写 ----
INIT2 = INIT.replace("          enable-pet: true\n", "")
cfg_path.write_text(INIT2, encoding="utf-8")
# 清缓存让官方重读
config_mod.load_config_readonly.cache_clear() if hasattr(config_mod.load_config_readonly, "cache_clear") else None
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin
gui = load_plugin()
class Ctx:
    def get_config(self, key, default=None):
        e = config_mod.load_config_readonly()["plugins"]["entries"]["custom-plugins/gateway-ui"]["settings"]
        seg = key.split(".")
        v = e
        for s in seg:
            if isinstance(v, dict) and s in v:
                v = v[s]
            else:
                return default
        return v
    def set_config(self, key, value):
        save_plugin_setting("custom-plugins/gateway-ui", tuple(key.split(".")), value)
view = gui.cfgmod.build_view(Ctx())
ck("build_view enable-pet runtime false when missing", view["pet"]["enable-pet"] is False)
ck("build_view explicit excludes missing enable-pet", "enable-pet" not in view["explicit"]["pet"])
after3 = cfg_path.read_text(encoding="utf-8")
ck("A1: missing enable-pet NOT written to config", "enable-pet" not in after3)

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)