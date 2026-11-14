# -*- coding: utf-8 -*-
"""test_spawn_pos.py — 屏内出生断言全量移植（旧 spawn_pos20_test.py 8 断言）。
新建窗口一律屏内右下角地面（不读 pet_pos.json）；resize 路径（old_h>0）
沿用存档+高度差微调，双向 clamp。side-env python 跑（offscreen Qt）。"""
import json
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication
app = QApplication([])

from pet_window import PetWindow

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui22-spawn")
BUS.mkdir(parents=True, exist_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg = {"enable-pet": True, "slug": "fairy", "scale": 0.7, "roam": False, "topmost": False}

# 罪证存档：作者磁盘上真实的出逃位置 (335, -145)——y 为负，屏幕外
(BUS / "pet_pos.json").write_text(json.dumps({"x": 335, "y": -145}), encoding="utf-8")

# --- 场景1：fresh spawn（old_h=0）必须无视存档，落在屏内右下角 ---
win = PetWindow(meta, BUS, lambda: dict(cfg), None, pet_menu=None)
ck("window built", win.sheet is not None)
g = win._screen_geo()
x, y = win.x(), win.y()
ck(f"spawn y in-screen (y={y} >= top={g.top()})", y >= g.top())
ck(f"spawn y on ground (y={y})", abs(y - (g.bottom() - win.height())) <= 2)
ck(f"spawn x in-screen (x={x})", g.left() <= x <= g.right() - win.width())
ck("spawn ignored escaped save (x!=335 or y!=-145)", (x, y) != (335, -145))

# --- 场景2：resize 路径（old_h>0）仍沿用存档位置（体验不变）---
(BUS / "pet_pos.json").write_text(json.dumps({"x": 335, "y": 400}), encoding="utf-8")
win._place_initial(old_h=win.height())  # 模拟换宠物/改大小的 reload 调用
ck("resize path honors saved pos", (win.x(), win.y()) == (335, 400))

# --- 场景3：resize 路径的越界存档也被双向 clamp（负 y 不再救不回）---
(BUS / "pet_pos.json").write_text(json.dumps({"x": 99999, "y": -500}), encoding="utf-8")
win._place_initial(old_h=win.height())
ck("resize clamps escaped save x", win.x() <= g.right() - win.width())
ck("resize clamps escaped save y>=top", win.y() >= g.top())

win.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
