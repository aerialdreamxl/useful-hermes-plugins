# -*- coding: utf-8 -*-
"""gateway-ui worker 精灵层 —— petdex 几何契约镜像（CONTRACT.md §7）。

常量逐条镜像官方 agent/pet/constants.py（整体重写修正：FRAMES_PER_STATE=6，
官方 steps(6)；旧代码取 8 帧是错的）。tests/test_sprites.py 在宿主 python 里
import 官方 agent.pet.constants 断言镜像一致，防漂移。

本模块顶层**不 import Qt / PIL**（宿主 python 无 Qt 也要能 import 做常量对齐）；
QPixmap 转换与 PIL 解码都在函数/方法内惰性加载。素材只读消费 <home>/pets/。
"""
from __future__ import annotations

from pathlib import Path

# ---- petdex 几何契约（mirror agent/pet/constants.py）----
FRAME_W = 192
FRAME_H = 208
# 每状态步进的帧数（petdex CSS steps(6)）；图集多出的物理列忽略。
FRAMES_PER_STATE = 6
LOOP_MS = 1100  # 单状态整循环时长 ms（petdex 默认）

DEFAULT_SCALE = 0.33
MIN_SCALE = 0.1
MAX_SCALE = 3.0

BLANK_ALPHA = 8  # 空白尾帧裁切阈值：整帧 alpha 最大值 <= 8 视为空白

# 当前 Petdex 行序（上→下），1536x1872 图集（8 列 x 9 行 Codex）。
CODEX_ROWS = ["idle", "running-right", "running-left", "waving", "jumping",
              "failed", "waiting", "running", "review"]
# 旧版 Hermes/petdex 行序（上→下），1728x1664 图集（9 列 x 8 行 legacy）。
LEGACY_ROWS = ["idle", "wave", "run", "failed", "review", "jump", "extra1", "extra2"]

# 规范 Hermes 名 → 可接受行名别名（降序优先）。镜像官方 STATE_ALIASES。
ALIASES = {
    "idle": ("idle",),
    "wave": ("wave", "waving"),
    "jump": ("jump", "jumping"),
    "run": ("run", "running"),
    "failed": ("failed",),
    "review": ("review",),
    "waiting": ("waiting",),
}
STATE_ALIASES = ALIASES  # 官方同名，便于对齐测试

# host 状态 → 精灵姿态行（CONTRACT §7：fall/climb→jumping、walk→方向跑行在窗口侧）
# R28-5：await（等待人类批准持续档）→ waiting 行（与 listening 同姿态，语义独立）。
HOST_MAP = {"idle": "idle", "listening": "waiting", "thinking": "review",
            "acting": "run", "busy": "run", "happy": "wave", "error": "failed",
            "await": "waiting"}


def clamp_scale(scale: float) -> float:
    """Clamp 到 [MIN_SCALE, MAX_SCALE]（镜像官方唯一校验点）。"""
    return max(MIN_SCALE, min(MAX_SCALE, scale))


def rows_for(row_count):
    """按图集行数选行表（镜像官方 state_rows_for_grid：>=9 行=Codex，否则 legacy）。"""
    try:
        rows = int(row_count or 0)
    except (TypeError, ValueError):
        rows = 0
    return CODEX_ROWS if rows >= len(CODEX_ROWS) else LEGACY_ROWS


def state_aliases_for(state):
    value = str(state)
    return ALIASES.get(value) or (value,)


def row_index(state, row_count):
    """状态 → 图集行号（钳制，永不抛错；镜像官方 state_row_index）。"""
    rows = rows_for(row_count)
    for name in state_aliases_for(state):
        if name in rows:
            return rows.index(name)
    return 0  # 0 = idle 行


def pil_to_pixmap(im):
    """PIL RGBA → QPixmap（惰性 import Qt；copy() 防 bytes 生命周期悬空）。"""
    from PySide6.QtGui import QImage, QPixmap
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimg)


class PetSheet:
    """petdex spritesheet → 按行裁帧（每状态 FRAMES_PER_STATE=6 帧，
    空白尾帧裁切 alpha<=8），缓存 QPixmap。webp/png 均可（PIL 解码）。

    事件反应高 DPI 修复：dpr=devicePixelRatio（默认 1.0 行为不变）。帧按物理
    像素渲染（round(w*dpr)×round(h*dpr)）并 setDevicePixelRatio(dpr)，Qt 直出
    物理像素不再二次放大；逻辑尺寸 self.w/h 不变——窗口几何/存档/地面物理
    全部继续用逻辑坐标。放大用 LANCZOS 高质量重采样；缩小保持现状。
    清晰度上限=素材原生 192×208，scale*dpr>1 属放大，不是超分辨率。"""

    def __init__(self, path, scale, dpr=1.0):
        from PIL import Image
        self.path = Path(path)
        sheet = Image.open(self.path).convert("RGBA")
        self.cols = max(1, sheet.width // FRAME_W)
        self.rows = max(1, sheet.height // FRAME_H)
        self.scale = scale
        try:
            dpr = float(dpr)
        except (TypeError, ValueError):
            dpr = 1.0
        self.dpr = dpr if dpr > 0 else 1.0
        self.w = max(1, int(FRAME_W * scale))
        self.h = max(1, int(FRAME_H * scale))
        # 物理像素尺寸（DPR 标记用）；逻辑尺寸 w/h 保持原语义
        self.pw = max(1, round(self.w * self.dpr))
        self.ph = max(1, round(self.h * self.dpr))
        self._sheet = sheet
        # 缓存键含 dpr（实例不可变，键即 row_idx；换 dpr 一律重建实例）
        self._cache = {}

    def frames(self, row_idx):
        if row_idx in self._cache:
            return self._cache[row_idx]
        from PIL import Image
        from PySide6.QtGui import QPixmap
        out = []
        top = min(row_idx * FRAME_H, max(0, self._sheet.height - FRAME_H))
        # 整体重写修正：官方 steps(6)，旧代码 min(cols, 8) 是错的。
        for i in range(min(self.cols, FRAMES_PER_STATE)):
            f = self._sheet.crop((i * FRAME_W, top, (i + 1) * FRAME_W, top + FRAME_H))
            if f.getchannel("A").getextrema()[1] <= BLANK_ALPHA:  # 空白尾帧裁切
                break
            if (self.pw, self.ph) != (FRAME_W, FRAME_H):
                # 放大走 LANCZOS（高质量）；缩小保持现状
                resample = Image.LANCZOS if (self.pw > FRAME_W or self.ph > FRAME_H) \
                    else Image.BICUBIC
                f = f.resize((self.pw, self.ph), resample)
            pm = pil_to_pixmap(f)
            pm.setDevicePixelRatio(self.dpr)  # Qt 按物理像素直出，不再二次放大
            out.append(pm)
        if not out:
            fb = QPixmap(self.pw, self.ph)
            fb.setDevicePixelRatio(self.dpr)
            out = self._cache.get(0) or [fb]
        self._cache[row_idx] = out
        return out

    def walk_row(self, direction):
        """方向跑行 + 镜像：右走优先 running-right，左走优先 running-left；
        legacy 无方向行时回退 run 行 + 镜像。返回 (row_idx, mirror)。"""
        rows = rows_for(self.rows)
        has_l, has_r = "running-left" in rows, "running-right" in rows
        if direction > 0:
            if has_r:
                return rows.index("running-right"), False
            if has_l:
                return rows.index("running-left"), True
            return row_index("run", self.rows), True
        if has_l:
            return rows.index("running-left"), False
        if has_r:
            return rows.index("running-right"), True
        return row_index("run", self.rows), False


def installed_pets(home):
    """只读枚举 <home>/pets/ 下已安装 petdex 库存（字母序）。"""
    d = Path(home) / "pets"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if (p / "spritesheet.webp").is_file() or (p / "spritesheet.png").is_file():
            out.append(p.name)
    return out


def sheet_path(home, slug):
    p = Path(home) / "pets" / slug
    for name in ("spritesheet.webp", "spritesheet.png"):
        if (p / name).is_file():
            return p / name
    return None
