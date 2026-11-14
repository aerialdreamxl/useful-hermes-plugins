# -*- coding: utf-8 -*-
"""test_dpi.py — 事件反应高 DPI 修复断言（物理像素渲染 + DPR 标记 + 小数透传）。

side-env python 跑（QT_QPA_PLATFORM=offscreen）。合成图集/临时总线只进
scratch gui24-dpi-*；真实 pets/ 只读。分两部分：
- Part A：子进程策略探针——QT_SCALE_FACTOR 小数缩放下 screen dpr 精确透传
  （125%/150%/200%），并断言 worker/main.py 在 QApplication 创建前显式
  设置 PassThrough（防漂移回 Round）。
- Part B：进程内——PetSheet 物理像素尺寸/DPR 标记/逻辑尺寸不变/LANCZOS
  放大/缓存含 dpr；PetWindow 构造取屏幕 dpr、dpr 变化重建 sheet、窗口
  逻辑几何不变、镜像路径（旧 Qt.Horizontal 在 PySide6 6.12 直接 TypeError）。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from _env import HOME, scratch  # noqa: E402
SCRATCH = scratch("gui24-dpi-tests")
SCRATCH.mkdir(parents=True, exist_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QTransform, QPainter, QImage
from PySide6.QtWidgets import QApplication

# ============ Part A：子进程策略探针（小数缩放精确透传）============
PROBE = SCRATCH / "probe_policy.py"
PROBE.write_text(
    "import os, sys\n"
    "from PySide6.QtCore import Qt\n"
    "from PySide6.QtGui import QGuiApplication\n"
    "QGuiApplication.setHighDpiScaleFactorRoundingPolicy(\n"
    "    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)\n"
    "from PySide6.QtWidgets import QApplication\n"
    "app = QApplication([])\n"
    "print(app.primaryScreen().devicePixelRatio())\n",
    encoding="utf-8")

for factor in ("1.25", "1.5", "2.0"):
    env = dict(os.environ, QT_SCALE_FACTOR=factor, QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, str(PROBE)], env=env,
                       capture_output=True, text=True, timeout=120)
    dpr = None
    try:
        dpr = float(p.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    ck(f"fractional scale {factor} -> screen dpr exact {factor} (PassThrough)",
       p.returncode == 0 and dpr is not None and abs(dpr - float(factor)) < 1e-6)

# main.py 启动处显式 PassThrough 且位于 QApplication 创建之前（源码契约）
main_src = (WORKER / "main.py").read_text(encoding="utf-8")
pol_pos = main_src.find("Qt.HighDpiScaleFactorRoundingPolicy.PassThrough")
app_pos = main_src.find("app = QApplication(sys.argv)")
ck("main.py sets PassThrough policy", pol_pos != -1)
ck("main.py policy set BEFORE QApplication creation",
   pol_pos != -1 and app_pos != -1 and pol_pos < app_pos)
ck("policy call is the only HighDpi site in main.py",
   main_src.count("HighDpiScaleFactorRoundingPolicy") == 2)  # setter + enum 各一

# ============ Part B：进程内 PetSheet / PetWindow ============
app = QApplication([])
from PIL import Image, ImageDraw
import sprites
from sprites import PetSheet
from pet_window import PetWindow

def make_sheet(path, cols, rows, opaque):
    """合成图集：每行前 opaque[r] 帧不透明（红块），其余全透明。"""
    im = Image.new("RGBA", (cols * sprites.FRAME_W, rows * sprites.FRAME_H),
                   (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for r in range(rows):
        for c in range(opaque[r] if r < len(opaque) else 0):
            x0, y0 = c * sprites.FRAME_W, r * sprites.FRAME_H
            d.rectangle([x0 + 20, y0 + 20, x0 + 100, y0 + 150],
                        fill=(200, 60, 60, 255))
    im.save(path)
    return path

codex_path = make_sheet(SCRATCH / "codex.png", 8, 9, [6] * 9)

# ---- B1：dpr=1.0 默认行为不变（逻辑=物理）----
s1 = PetSheet(codex_path, 0.7)
f1 = s1.frames(0)
ck("dpr default 1.0", s1.dpr == 1.0)
ck("logical size unchanged at dpr=1 (134x145)", (s1.w, s1.h) == (134, 145))
ck("dpr=1 physical==logical", all(p.width() == s1.w and p.height() == s1.h
                                  for p in f1))
ck("dpr=1 pixmap DPR==1.0", all(p.devicePixelRatio() == 1.0 for p in f1))

# ---- B2：高 dpr 物理像素渲染 + DPR 标记，逻辑尺寸不变 ----
for dpr in (1.25, 1.5, 2.0):
    s = PetSheet(codex_path, 0.7, dpr=dpr)
    fr = s.frames(0)
    pw, ph = round(s1.w * dpr), round(s1.h * dpr)
    ck(f"dpr={dpr}: logical size unchanged (134x145)", (s.w, s.h) == (134, 145))
    ck(f"dpr={dpr}: physical size == round(logical*dpr) ({pw}x{ph})",
       (s.pw, s.ph) == (pw, ph))
    ck(f"dpr={dpr}: pixmap pixels are physical {pw}x{ph}",
       all(p.width() == pw and p.height() == ph for p in fr))
    ck(f"dpr={dpr}: pixmap devicePixelRatio == {dpr}",
       all(abs(p.devicePixelRatio() - dpr) < 1e-9 for p in fr))
    ck(f"dpr={dpr}: still 6 frames (blank-tail trim intact)", len(fr) == 6)
    ck(f"dpr={dpr}: drawPixmap logical size == window logical size",
       all(round(p.width() / p.devicePixelRatio()) == s.w for p in fr))

# ---- B3：放大走 LANCZOS（逐像素对齐参考实现）----
from PIL import Image as PILImage
src = PILImage.open(codex_path).convert("RGBA")
crop0 = src.crop((0, 0, sprites.FRAME_W, sprites.FRAME_H))
s_up = PetSheet(codex_path, 0.7, dpr=1.5)  # 134*1.5=201 > 192 原生 → 放大
pm_up = s_up.frames(0)[0]
ref = crop0.resize((s_up.pw, s_up.ph), PILImage.LANCZOS)
got = pm_up.toImage().convertToFormat(QImage.Format_RGBA8888)
ref_bytes = ref.tobytes("raw", "RGBA")
got_bytes = b"".join(bytes(got.constBits())[y * got.bytesPerLine():
                                           y * got.bytesPerLine() + got.width() * 4]
                     for y in range(got.height()))
ck("upscale case really upscales (201>192 native)", s_up.pw > sprites.FRAME_W)
# Qt 内部 ARGB32_Premultiplied 存储，转回 straight RGBA 有 ±1~2 量化误差
# （探针实测 912/175272 字节差 ≤1）——逐通道容差 2 内即认定同一 LANCZOS 结果；
# 若误用 NEAREST/BILINEAR 差异会成百上千，容差不会放行。
max_diff = max(abs(a - b) for a, b in zip(got_bytes, ref_bytes))
ck("upscale uses LANCZOS (pixel-match vs PIL reference, premul tolerance<=2)",
   len(got_bytes) == len(ref_bytes) and max_diff <= 2)

# ---- B4：缓存键含 dpr（不同 dpr 实例互不串缓存）----
sa = PetSheet(codex_path, 0.7, dpr=1.5)
sb = PetSheet(codex_path, 0.7, dpr=2.0)
fa, fb = sa.frames(0), sb.frames(0)
ck("cache identity within instance", sa.frames(0) is fa)
ck("different dpr -> different physical frames",
   fa[0].width() != fb[0].width()
   and abs(fa[0].devicePixelRatio() - fb[0].devicePixelRatio()) > 0.4)
ck("dpr change rebuilds (new instance, new cache)", sa._cache is not sb._cache)

# ---- B5：全空白行兜底帧也带 DPR 标记 ----
blank_path = make_sheet(SCRATCH / "blank.png", 8, 9, [0] * 9)
sblank = PetSheet(blank_path, 0.7, dpr=1.5)
fblank = sblank.frames(0)
ck("fallback frame non-empty", len(fblank) >= 1)
ck("fallback frame carries dpr", abs(fblank[0].devicePixelRatio() - 1.5) < 1e-9)
ck("fallback frame physical size", (fblank[0].width(), fblank[0].height())
   == (sblank.pw, sblank.ph))

# ---- B6：PetWindow 构造取屏幕 dpr，逻辑几何不变 ----
BUS = SCRATCH / "bus"
BUS.mkdir(parents=True, exist_ok=True)
meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg = {"enable-pet": True, "slug": "fairy", "scale": 0.7,
       "roam": False, "topmost": False}
win = PetWindow(meta, BUS, lambda: dict(cfg), None, pet_menu=None)
win.show()
app.processEvents()
scr_dpr = win.screen().devicePixelRatio()
ck("window sheet built", win.sheet is not None)
ck("window sheet dpr == screen dpr", abs(win.sheet.dpr - scr_dpr) < 1e-9)
ck("window logical geometry == sheet logical size",
   (win.width(), win.height()) == (win.sheet.w, win.sheet.h))
ck("window _dpr tracked", abs(win._dpr - scr_dpr) < 1e-9)

# ---- B7：dpr 变化 → reload 重建 sheet（逻辑尺寸/窗口几何不变）----
old_sheet = win.sheet
old_geo = (win.width(), win.height())
old_pos = (win.x(), win.y())
win._cur_dpr = lambda: 2.5  # 模拟拖到 250% 缩放屏
win.reload()
ck("dpr change rebuilds sheet instance", win.sheet is not old_sheet)
ck("rebuilt sheet dpr == 2.5", abs(win.sheet.dpr - 2.5) < 1e-9)
ck("rebuilt sheet physical == round(logical*2.5)",
   (win.sheet.pw, win.sheet.ph) == (round(old_geo[0] * 2.5),
                                    round(old_geo[1] * 2.5)))
ck("window logical geometry unchanged after dpr rebuild",
   (win.width(), win.height()) == old_geo)
win._step()
app.processEvents()
ck("frames after rebuild carry new dpr",
   all(abs(p.devicePixelRatio() - 2.5) < 1e-9 for p in win._frames))

# ---- B8：screenChanged 钩子触发重建 ----
old_sheet2 = win.sheet
win._cur_dpr = lambda: 1.0
win.screenChanged(win.screen())
ck("screenChanged rebuilds on dpr delta", win.sheet is not old_sheet2
   and abs(win.sheet.dpr - 1.0) < 1e-9)
sheet_before = win.sheet
win.screenChanged(win.screen())  # dpr 未变 → 不重建
ck("screenChanged no-op when dpr unchanged", win.sheet is sheet_before)

# ---- B9：镜像路径（旧 Qt.Horizontal 在 6.12 TypeError；QTransform 保 DPR）----
pm = win.sheet.frames(0)[0]
try:
    pm.transformed(Qt.Horizontal)
    legacy_ok = True
except TypeError:
    legacy_ok = False
ck("legacy Qt.Horizontal rejected by PySide6 6.12 (bug evidence)", not legacy_ok)
mir = pm.transformed(QTransform().scale(-1, 1))
ck("QTransform mirror preserves devicePixelRatio",
   abs(mir.devicePixelRatio() - pm.devicePixelRatio()) < 1e-9)
ck("QTransform mirror preserves physical size",
   (mir.width(), mir.height()) == (pm.width(), pm.height()))

# ---- B10：paintEvent 实跑不炸（含镜像分支，旧代码此处 TypeError）----
win._cur_dpr = lambda: 1.5
win.reload()
win._advance_frame(time.time())
win.mirror = False
win.repaint()
ck("paintEvent normal path runs", True)
win.mirror = True
win.repaint()  # 旧代码：pm.transformed(Qt.Horizontal) → TypeError
ck("paintEvent mirror path runs (no TypeError)", True)

# ---- B11：真实 fairy 素材高 dpr 渲染冒烟（只读）----
sp = sprites.sheet_path(HOME, "fairy")
real = PetSheet(sp, 0.7, dpr=1.5)
rfr = real.frames(0)
ck("real fairy @1.5dpr decodes 1..6 frames", 1 <= len(rfr) <= 6)
ck("real fairy @1.5dpr physical size",
   all(p.width() == real.pw and p.height() == real.ph for p in rfr))
ck("real fairy @1.5dpr DPR marked",
   all(abs(p.devicePixelRatio() - 1.5) < 1e-9 for p in rfr))

win.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
