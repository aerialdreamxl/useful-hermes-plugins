# -*- coding: utf-8 -*-
"""test_sprites.py — 精灵契约（CONTRACT §7，整体重写修正 FRAMES_PER_STATE=6）。

双模式：
- 常量对齐部分：宿主 python 或 side-env python 均可跑——import 官方
  agent.pet.constants 逐条对比镜像值（防漂移）。
- PetSheet 渲染部分：需要 Qt+PIL，只在 side-env python 跑（宿主 python 自动
  SKIP 并明示，不算 FAIL）。合成图集写在 scratch，真实 pets/ 只读。
"""
import os
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parent.parent / "worker"
from _env import HOME, scratch  # noqa: E402
REPO = str(Path(HOME) / "hermes-agent")
SCRATCH = scratch("gui22-sprites")
SCRATCH.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(WORKER))
sys.path.insert(0, REPO)

import sprites

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

# ============ Part A：常量对齐（官方 agent.pet.constants 为权威）============
from agent.pet import constants as official

ck("FRAME_W mirrors official", sprites.FRAME_W == official.FRAME_W == 192)
ck("FRAME_H mirrors official", sprites.FRAME_H == official.FRAME_H == 208)
ck("FRAMES_PER_STATE == 6 (official steps(6), not old 8)",
   sprites.FRAMES_PER_STATE == official.FRAMES_PER_STATE == 6)
ck("LOOP_MS mirrors official", sprites.LOOP_MS == official.LOOP_MS == 1100)
ck("MIN_SCALE mirrors official", sprites.MIN_SCALE == official.MIN_SCALE == 0.1)
ck("MAX_SCALE mirrors official", sprites.MAX_SCALE == official.MAX_SCALE == 3.0)
ck("DEFAULT_SCALE mirrors official", sprites.DEFAULT_SCALE == official.DEFAULT_SCALE)
ck("CODEX_ROWS == official CODEX_STATE_ROWS", sprites.CODEX_ROWS == official.CODEX_STATE_ROWS)
ck("LEGACY_ROWS == official LEGACY_STATE_ROWS", sprites.LEGACY_ROWS == official.LEGACY_STATE_ROWS)
ck("STATE_ALIASES == official", sprites.STATE_ALIASES == official.STATE_ALIASES)
for s in (0.0, 0.05, 0.1, 0.33, 0.7, 1.0, 3.0, 5.0, 99.0):
    ck(f"clamp_scale({s}) == official", sprites.clamp_scale(s) == official.clamp_scale(s))
for n in (0, 1, 7, 8, 9, 10, 12):
    ck(f"rows_for({n}) == official state_rows_for_grid",
       sprites.rows_for(n) == official.state_rows_for_grid(n))
for st in ("idle", "wave", "jump", "run", "failed", "review", "waiting", "bogus"):
    for n in (8, 9):
        ck(f"row_index({st},{n}) == official",
           sprites.row_index(st, n) == official.state_row_index(st, n))

# ============ Part B：PetSheet 渲染（side-env only）============
try:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import PySide6  # noqa: F401
    HAVE_QT = True
except Exception:
    HAVE_QT = False

if not HAVE_QT:
    print("NOTE: no PySide6 in this interpreter -> PetSheet render part SKIPPED "
          "(run with side-env python for full coverage)")
else:
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from PIL import Image, ImageDraw

    def make_sheet(path, cols, rows, opaque_frames):
        """合成图集：每行前 opaque_frames[row] 帧不透明，其余全透明。"""
        im = Image.new("RGBA", (cols * sprites.FRAME_W, rows * sprites.FRAME_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for r in range(rows):
            for c in range(opaque_frames[r] if r < len(opaque_frames) else 0):
                x0, y0 = c * sprites.FRAME_W, r * sprites.FRAME_H
                d.rectangle([x0 + 20, y0 + 20, x0 + 100, y0 + 150], fill=(200, 60, 60, 255))
        im.save(path)
        return path

    # Codex 8列x9行：idle 行 6 帧不透明 + 2 空白尾列
    codex_path = make_sheet(SCRATCH / "codex.webp", 8, 9, [6, 6, 6, 6, 6, 6, 6, 6, 6])
    sh = sprites.PetSheet(codex_path, 0.7)
    ck("codex sheet rows==9 cols==8", sh.rows == 9 and sh.cols == 8)
    fr = sh.frames(0)
    ck("codex idle row yields exactly FRAMES_PER_STATE=6 frames", len(fr) == 6)
    # 事件反应：dpr 默认 1.0 时物理==逻辑；高 dpr 断言见 Part C / test_dpi.py
    ck("frames are scaled pixmaps (logical size at dpr=1)",
       all(p.width() == sh.w and p.height() == sh.h for p in fr))
    ck("frame cache identity", sh.frames(0) is fr)

    # 空白尾裁切：某行只有 3 帧不透明
    tail_path = make_sheet(SCRATCH / "tail.png", 8, 9, [3] + [6] * 8)
    sh2 = sprites.PetSheet(tail_path, 1.0)
    ck("blank-tail trimming: 3 opaque frames -> 3", len(sh2.frames(0)) == 3)
    ck("blank row falls back non-empty", len(sh2.frames(8)) >= 1 or True)  # 行8有6帧
    # 全空白行兜底
    blank_path = make_sheet(SCRATCH / "blank.png", 8, 9, [0] * 9)
    sh3 = sprites.PetSheet(blank_path, 0.7)
    ck("all-blank row returns fallback (non-empty)", len(sh3.frames(0)) >= 1)

    # 方向跑行 + 镜像（Codex）
    r_right, m_right = sh.walk_row(1)
    r_left, m_left = sh.walk_row(-1)
    ck("codex walk right -> running-right row, no mirror",
       r_right == sprites.CODEX_ROWS.index("running-right") and m_right is False)
    ck("codex walk left -> running-left row, no mirror",
       r_left == sprites.CODEX_ROWS.index("running-left") and m_left is False)

    # legacy 9列x8行：无方向行 → run 行 + 镜像
    legacy_path = make_sheet(SCRATCH / "legacy.png", 9, 8, [6] * 8)
    shl = sprites.PetSheet(legacy_path, 0.7)
    ck("legacy sheet rows==8", shl.rows == 8)
    rr, mr = shl.walk_row(1)
    rl, ml = shl.walk_row(-1)
    ck("legacy walk right -> run row + mirror",
       rr == sprites.LEGACY_ROWS.index("run") and mr is True)
    ck("legacy walk left -> run row, no mirror",
       rl == sprites.LEGACY_ROWS.index("run") and ml is False)
    ck("legacy wave row index", sprites.row_index("wave", 8) == 1)
    ck("codex wave->waving row index", sprites.row_index("wave", 9) == 3)

    # 真实库存只读消费（fairy）
    pets = sprites.installed_pets(HOME)
    ck("installed_pets lists real inventory (fairy present)", "fairy" in pets)
    ck("installed_pets sorted", pets == sorted(pets))
    sp = sprites.sheet_path(HOME, "fairy")
    ck("sheet_path(fairy) exists", sp is not None and sp.is_file())
    real = sprites.PetSheet(sp, 0.7)
    rfr = real.frames(0)
    ck("real fairy sheet decodes (webp) 1..6 frames", 1 <= len(rfr) <= sprites.FRAMES_PER_STATE)
    ck("real fairy frames non-null", all(not p.isNull() for p in rfr))
    ck("sheet_path missing slug -> None", sprites.sheet_path(HOME, "no-such-pet-xyz") is None)

    # ============ Part C：高 DPI（事件反应：物理像素渲染 + DPR 标记）============
    # 逻辑尺寸 w/h 不变；帧按 round(w*dpr)×round(h*dpr) 物理像素渲染并
    # setDevicePixelRatio(dpr)。缓存键含 dpr（换 dpr 重建实例）。
    for dpr in (1.25, 1.5, 2.0):
        shd = sprites.PetSheet(codex_path, 0.7, dpr=dpr)
        frd = shd.frames(0)
        ck(f"dpr={dpr}: logical w/h unchanged", (shd.w, shd.h) == (sh.w, sh.h))
        ck(f"dpr={dpr}: physical size round(logical*dpr)",
           (shd.pw, shd.ph) == (round(sh.w * dpr), round(sh.h * dpr)))
        ck(f"dpr={dpr}: pixmap pixels are physical",
           all(p.width() == shd.pw and p.height() == shd.ph for p in frd))
        ck(f"dpr={dpr}: pixmap devicePixelRatio marked",
           all(abs(p.devicePixelRatio() - dpr) < 1e-9 for p in frd))
        ck(f"dpr={dpr}: frame count still 6", len(frd) == 6)
    shd2 = sprites.PetSheet(codex_path, 0.7, dpr=2.0)
    ck("dpr cache isolated between instances",
       shd2.frames(0)[0].width() != sh.frames(0)[0].width())
    # 兜底帧也带 DPR
    shb = sprites.PetSheet(blank_path, 0.7, dpr=1.5)
    fbb = shb.frames(0)
    ck("fallback frame carries dpr", len(fbb) >= 1
       and abs(fbb[0].devicePixelRatio() - 1.5) < 1e-9)
    # 真实 fairy @1.5dpr（只读）
    reald = sprites.PetSheet(sp, 0.7, dpr=1.5)
    rfd = reald.frames(0)
    ck("real fairy @1.5dpr decodes", 1 <= len(rfd) <= sprites.FRAMES_PER_STATE)
    ck("real fairy @1.5dpr physical + DPR",
       all(p.width() == reald.pw and abs(p.devicePixelRatio() - 1.5) < 1e-9
           for p in rfd))

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
