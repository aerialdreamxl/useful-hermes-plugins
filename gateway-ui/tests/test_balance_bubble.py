# -*- coding: utf-8 -*-
"""test_balance_bubble.py — 余额气泡 BalanceBubble offscreen 断言（CONTRACT §10）。

side-env python 跑（QT_QPA_PLATFORM=offscreen）。假 view 文件驱动，测试总线
只用 scratch/gui25-bub；绝不碰真实总线/config。判据：四段渲染、peak=null
行隐藏、ok=false 截断 24、空 providers、箭头循环 N=3/N=1、切换零 settings_req、
refresh_req 格式+原子性、follow 锚定/翻转/clamp（spike 同款）、新鲜度变灰、
mtime+签名双判、右键菜单两项。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtCore import Qt, QPoint, QRect, QPointF, QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

import peak_rules as pr
from balance_bubble import BalanceBubble, bubble_pos, BUBBLE_W, GAP_PX

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui25-bub/bubble")
BUS.mkdir(parents=True, exist_ok=True)
VIEW = BUS / "balance_view.json"
for f in BUS.glob("*.json*"):
    f.unlink()

R = []


def ck(name, cond):
    R.append((name, bool(cond)))


def write_view(view, mtime=None):
    VIEW.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    # 显式错开 mtime：NTFS mtime 精度有限，同 tick 连续写不同内容会被
    # 双判去重正确跳过（该路径由专门断言覆盖）；常规用例保证 mtime 可分辨。
    if mtime is None:
        mtime = time.time()
        prev = getattr(write_view, "_last_mt", 0.0)
        if mtime <= prev:
            mtime = prev + 0.01
        write_view._last_mt = mtime
    os.utime(VIEW, (mtime, mtime))


def mk_provider(pid, name, ok=True, peak=None, extra=None, display=None):
    return {"id": pid, "name": name, "unit": "CNY", "ok": ok,
            "error": "" if ok else "E" * 40,
            "value": 12.34, "display": display or "¥ 12.34",
            "peak": peak, "extra": extra or {}, "fetched_ts": time.time()}


NOW = time.time()
PEAK_DS = {"is_peak": False, "label_peak": "峰", "label_valley": "谷",
           "next_boundary_ts": NOW + 3600, "rule": "ds_cn",
           "server_authoritative": True}
SCREEN = QRect(0, 0, 1920, 1040)
meta = {"bus_dir": str(BUS), "hermes_home": HOME}
bub = BalanceBubble(meta, BUS, get_screen_rect=lambda: SCREEN)

# ---- 窗口契约 ----
ck("bubble width fixed 220", bub.width() == BUBBLE_W == 220)
ck("bubble no parent (no ownership)", bub.parent() is None)
flags = bub.windowFlags()
ck("frameless flag", bool(flags & Qt.FramelessWindowHint))
ck("stays-on-top flag", bool(flags & Qt.WindowStaysOnTopHint))
ck("tool flag", bool(flags & Qt.Tool))
ck("translucent background", bub.testAttribute(Qt.WA_TranslucentBackground))
ck("countdown timer 1s", bub._timer.interval() == 1000)

# ---- 无文件 → 等待宿主数据 ----
bub.refresh_view()
ck("missing view -> waiting mode", bub._mode == "waiting")
ck("waiting msg text", bub.lbl_msg.text() == "等待宿主数据")
ck("waiting msg visible", not bub.lbl_msg.isHidden())
ck("waiting hides title", bub.lbl_title.isHidden())
ck("waiting hides amount", bub.lbl_amount.isHidden())
ck("dedup: second refresh no re-render", bub.refresh_view() is False)

# ---- 损坏 JSON → 等待态 ----
VIEW.write_text("{broken json!!", encoding="utf-8")
ck("corrupt view -> re-render", bub.refresh_view() is True)
ck("corrupt view waiting mode", bub._mode == "waiting")
ck("corrupt view msg", bub.lbl_msg.text() == "等待宿主数据")

# ---- 空 providers → 未配置余额源 ----
write_view({"ts": NOW, "refresh_s": 300, "providers": [], "default_id": ""})
ck("empty providers re-render", bub.refresh_view() is True)
ck("empty mode", bub._mode == "empty")
ck("empty msg text", bub.lbl_msg.text() == "未配置余额源")

# ---- 正常渲染四段（3 providers，default=b）----
v3 = {"ts": NOW - 10, "refresh_s": 300,
      "providers": [mk_provider("a", "Alpha"),
                    mk_provider("b", "Beta", peak=dict(PEAK_DS),
                                extra={"today": "今日已用 ¥ 0.50"}),
                    mk_provider("c", "Gamma", peak=None)],
      "default_id": "b"}
write_view(v3)
ck("data view re-render", bub.refresh_view() is True)
ck("data mode", bub._mode == "data")
ck("default_id selected", bub.current_provider_id() == "b")
ck("title shows provider name", bub.lbl_title.text() == "Beta")
ck("amount shows display", bub.lbl_amount.text() == "¥ 12.34")
ck("amount full text kept", bub.amount_text_full == "¥ 12.34")
ck("peak tag shown valley", bub.lbl_peak_tag.text() == "[谷]")
ck("peak tag green", pr is not None and "#2fa24c" in bub.lbl_peak_tag.styleSheet())
ck("peak row visible", not bub.lbl_peak_tag.isHidden() and not bub.lbl_peak_cd.isHidden())
ck("countdown HH:MM:SS format", re.fullmatch(r"\d{2,}:\d{2}:\d{2}", bub.lbl_peak_cd.text()) is not None)
ck("bottom has extra.today", "今日已用 ¥ 0.50" in bub.bottom_text_full)
ck("bottom cache age 0 min", "缓存于 0 分前" in bub.bottom_text_full)
ck("fresh not stale", bub._stale is False)
ck("fresh amount white", "#f2f3f5" in bub.lbl_amount.styleSheet())
ck("arrows visible N=3", not bub.arrow_l.isHidden() and not bub.arrow_r.isHidden())
ck("height in 110-130 band", 100 <= bub.height() <= 140)

# ---- 箭头循环 N=3 回原点（真实鼠标事件路由）----
def click(w):
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(12, 12),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(w, ev)

click(bub.arrow_r)
ck("arrow right -> c", bub.current_provider_id() == "c")
click(bub.arrow_r)
ck("arrow right -> a (wrap)", bub.current_provider_id() == "a")
click(bub.arrow_r)
ck("arrow right -> b (full cycle N=3)", bub.current_provider_id() == "b")
click(bub.arrow_l)
ck("arrow left -> a", bub.current_provider_id() == "a")
ck("switch writes no settings_req (A2)", not (BUS / "settings_req.json").exists())
ck("switch writes no settings_req.tmp", not (BUS / "settings_req.json.tmp").exists())

# ---- peak=null 整行隐藏（provider c）----
click(bub.arrow_r)  # a -> b
click(bub.arrow_r)  # b -> c
ck("now at c", bub.current_provider_id() == "c")
ck("peak=null hides tag", bub.lbl_peak_tag.isHidden())
ck("peak=null hides countdown", bub.lbl_peak_cd.isHidden())
ck("peak=null internal state None", bub._peak is None)
ck("c still shows amount", bub.lbl_amount.text() == "¥ 12.34")

# ---- ok=false 错误截断 24 字符 ----
v_err = {"ts": NOW, "refresh_s": 300,
         "providers": [mk_provider("a", "Alpha", ok=False)], "default_id": "a"}
write_view(v_err)
bub.refresh_view()
ck("error amount truncated to 24", bub.amount_text_full == "E" * 24)
ck("error truncation length", len(bub.amount_text_full) == 24)

# ---- N=1 箭头隐藏 ----
ck("N=1 left arrow hidden", bub.arrow_l.isHidden())
ck("N=1 right arrow hidden", bub.arrow_r.isHidden())
bub.step_provider(1)
ck("N=1 step no-op", bub.current_provider_id() == "a")

# ---- 新鲜度：age > 2*refresh_s → 灰 + 过期角标 ----
v_stale = {"ts": NOW - 1000, "refresh_s": 300,
           "providers": [mk_provider("a", "Alpha")], "default_id": "a"}
write_view(v_stale)
bub.refresh_view()
ck("stale flag set (1000 > 600)", bub._stale is True)
ck("stale badge in bottom", "数据可能过期" in bub.bottom_text_full)
ck("stale amount gray", "#8a8f98" in bub.lbl_amount.styleSheet())
ck("stale cache minutes", "缓存于 16 分前" in bub.bottom_text_full)
v_fresh = dict(v_stale, ts=time.time() - 5)
write_view(v_fresh)
bub.refresh_view()
ck("fresh again not stale", bub._stale is False)
ck("fresh badge gone", "数据可能过期" not in bub.bottom_text_full)
ck("fresh amount white again", "#f2f3f5" in bub.lbl_amount.styleSheet())

# ---- mtime+签名双判：同 mtime 改内容仍刷新 ----
write_view(v_fresh)
bub.refresh_view()
mt = VIEW.stat().st_mtime
ck("same content dedup", bub.refresh_view() is False)
v_same_mt = dict(v_fresh, ts=time.time() - 130)
write_view(v_same_mt, mtime=mt)
ck("same mtime new content re-renders", bub.refresh_view() is True)
ck("same mtime content applied", "缓存于 2 分前" in bub.bottom_text_full)

# ---- view 更新保住选择 / 消失回 default ----
write_view(v3)
bub.refresh_view()
ck("reselect keeps active id (a from cycle)", bub.current_provider_id() == "a")
bub.step_provider(1)  # a -> b
ck("stepped to b", bub.current_provider_id() == "b")
v3b = dict(v3, ts=time.time())
write_view(v3b)
bub.refresh_view()
ck("view update keeps selection by id (b)", bub.current_provider_id() == "b")
bub.step_provider(1)  # b -> c
v3c = dict(v3, ts=time.time(), providers=[mk_provider("a", "Alpha"), mk_provider("b", "Beta")])
write_view(v3c)
bub.refresh_view()
ck("selection gone -> default_id", bub.current_provider_id() == "b")

# ---- 倒计时跨边界推进（规则透传）----
bub.show()  # _on_tick 仅在可见时走（生产语义）；直接调用测试也须先 show
app.processEvents()
now2 = time.time()
v_bnd = {"ts": now2, "refresh_s": 300,
         "providers": [mk_provider("a", "Alpha",
                                   peak={"is_peak": True, "label_peak": "峰",
                                         "label_valley": "谷",
                                         "next_boundary_ts": time.time() + 1.2,
                                         "rule": "ds_cn"})],
         "default_id": "a"}
write_view(v_bnd)
bub.refresh_view()
ck("boundary state built", bub._peak is not None and bub._peak["boundary"] > now2)
time.sleep(1.5)
bub._on_tick()
now3 = time.time()
ck("crossed boundary advanced", bub._peak["boundary"] > now3)
ck("advanced is_peak matches rule", bub._peak["is_peak"] == pr.is_peak_at(now3, "ds_cn"))
ck("advanced boundary matches rule",
   abs(bub._peak["boundary"] - pr.next_boundary(now3, "ds_cn", None, None)) < 2.0)
ck("countdown still formatted", re.fullmatch(r"\d{2,}:\d{2}:\d{2}", bub.lbl_peak_cd.text()) is not None)
# 精确倒计时串（+0.5s 余量防秒针抖动）
bub._peak["boundary"] = time.time() + 3661.5
bub._render_countdown()
ck("countdown 3661s -> 01:01:01", bub.lbl_peak_cd.text() == "01:01:01")
bub._peak["boundary"] = time.time() + 90000.5
bub._render_countdown()
ck("countdown >24h hours accumulate no Nd", bub.lbl_peak_cd.text() == "25:00:00")
# 峰态红色
bub._peak["is_peak"] = True
bub._on_tick()
ck("peak tag red", "#e0433f" in bub.lbl_peak_tag.styleSheet())
ck("peak tag text", bub.lbl_peak_tag.text() == "[峰]")

# ---- refresh_req：格式 + 原子性（tmp+rename 实锤）----
import balance_bubble as bb
real_replace = os.replace
replace_calls = []
def spy_replace(src, dst):
    replace_calls.append((str(src), str(dst)))
    return real_replace(src, dst)
bb.os.replace = spy_replace
rid = bub.request_refresh()
bb.os.replace = real_replace
req_p = BUS / "balance_refresh_req.json"
ck("refresh_req file exists", req_p.is_file())
req = json.loads(req_p.read_text(encoding="utf-8"))
ck("refresh_req id hex8", re.fullmatch(r"[0-9a-f]{8}", req["id"]) is not None)
ck("refresh_req id returned", rid == req["id"] == bub.last_refresh_req_id)
ck("refresh_req ts float fresh", abs(req["ts"] - time.time()) < 5)
ck("refresh_req provider_id current", req["provider_id"] == bub.current_provider_id())
ck("refresh_req exact key set", set(req) == {"id", "ts", "provider_id"})
ck("atomic via os.replace", len(replace_calls) == 1
   and replace_calls[0][0].endswith("balance_refresh_req.json.tmp")
   and replace_calls[0][1].endswith("balance_refresh_req.json"))
ck("no tmp residue", not (BUS / "balance_refresh_req.json.tmp").exists())

# ---- 右键菜单：扁平两项 + trigger 接线 ----
acts = [a for a in bub._menu.actions() if not a.isSeparator()]
ck("menu exactly 2 items", len(acts) == 2)
ck("menu item 1 refresh", acts[0].text() == "立即刷新")
ck("menu item 2 close", acts[1].text() == "关闭气泡")
bub.show()
app.processEvents()
acts[1].trigger()
ck("close action hides bubble", not bub.isVisible())
ck("close stops timer", not bub._timer.isActive())
n_before = req_p.stat().st_mtime
time.sleep(0.02)
acts[0].trigger()
ck("refresh action writes new req", req_p.stat().st_mtime >= n_before)
req2 = json.loads(req_p.read_text(encoding="utf-8"))
ck("refresh action new id", req2["id"] != req["id"])

# ---- follow 锚定/翻转/clamp（spike 同款断言）----
PET = QRect(500, 400, 100, 100)
p, f = bubble_pos(PET, 220, 120, SCREEN)
ck("anchor above-center", not f and p.y() == 400 - 120 - GAP_PX)
ck("anchor centered x", p.x() == PET.center().x() - 110)
p, f = bubble_pos(QRect(500, SCREEN.top() + 50, 100, 100), 220, 120, SCREEN)
ck("flip below when no headroom", f and p.y() == SCREEN.top() + 50 + 100 + GAP_PX)
p, f = bubble_pos(QRect(SCREEN.right() - 10, 400, 100, 100), 220, 120, SCREEN)
ck("clamp right edge", p.x() + 220 <= SCREEN.right() + 1)
p, f = bubble_pos(QRect(SCREEN.left() - 50, 400, 100, 100), 220, 120, SCREEN)
ck("clamp left edge", p.x() >= SCREEN.left())
p, f = bubble_pos(QRect(SCREEN.right() - 10, SCREEN.top() + 5, 100, 100), 220, 120, SCREEN)
ck("corner flip+clamp", f and p.x() + 220 <= SCREEN.right() + 1)
p, f = bubble_pos(QRect(500, SCREEN.top() + SCREEN.height() - 40, 100, 100), 220, 120, SCREEN)
ck("bottom pet clamps into screen", p.y() + 120 <= SCREEN.top() + SCREEN.height())

# ---- widget 级 follow：show_for + 移动后重锚（processEvents 再断言）----
bub.show_for(PET)
app.processEvents()
ck("show_for visible", bub.isVisible())
ck("show_for starts timer", bub._timer.isActive())
expect, _ = bubble_pos(PET, bub.width(), bub.height(), SCREEN)
ck("show_for anchored", bub.pos() == expect)
PET2 = QRect(700, 450, 100, 100)
bub.follow(PET2)
app.processEvents()
expect2, _ = bubble_pos(PET2, bub.width(), bub.height(), SCREEN)
ck("follows pet move", bub.pos() == expect2)
bub.hide_bubble()
app.processEvents()
ck("hide_bubble hidden", not bub.isVisible())
ck("hide_bubble timer stopped", not bub._timer.isActive())

# ---- 视图删除 → 回等待态 ----
VIEW.unlink()
ck("view deleted re-render", bub.refresh_view() is True)
ck("deleted -> waiting", bub._mode == "waiting" and bub.lbl_msg.text() == "等待宿主数据")

# ---- 余额气泡修订：lines 结构化多行（作者原型图排版）----
def mk_lines_provider(pid="bailian_token_plan"):
    p = mk_provider(pid, "百炼 Token Plan", display="59503 / 80000 Credits")
    p["lines"] = [
        {"text": "[TestUser] 剩余月额度", "style": "title"},
        {"style": "bar", "fraction": 0.74, "text": "74%"},
        {"text": "Standard  距到期84天", "style": "info"},
    ]
    p["peak"] = {"is_peak": False, "label_peak": "峰", "label_valley": "谷",
                 "next_boundary_ts": time.time() + 3600,
                 "windows": [{"days": [1, 2, 3, 4, 5, 6, 7],
                              "ranges": [[8, 22]], "tz_offset_h": 8}]}
    return p

write_view({"ts": time.time(), "refresh_s": 300,
            "providers": [mk_lines_provider()], "default_id": "bailian_token_plan"})
ck("lines refresh re-render", bub.refresh_view() is True)
ck("lines title shown", bub.lbl_line_title.text() == "[TestUser] 剩余月额度")
ck("lines bar shown", not bub.bar_line.isHidden())
ck("lines bar fraction", abs(bub.bar_line._fraction - 0.74) < 1e-6)
ck("lines bar pct text", bub.bar_line.lbl_pct.text() == "74%")
ck("lines info shown", bub.lbl_line_info.text() == "Standard  距到期84天")
ck("lines hides single amount", bub.lbl_amount.isHidden())
ck("lines hides text amount slot", bub.lbl_line_amount.isHidden())
ck("lines title visible", not bub.lbl_line_title.isHidden())
ck("lines peak valley tag", bub.lbl_peak_tag.text() == "[谷]")
# 过期变灰联动 bar 行（填充+百分比同步）
write_view({"ts": time.time() - 700, "refresh_s": 300,
            "providers": [mk_lines_provider()], "default_id": "bailian_token_plan"})
bub.refresh_view()
bub._render_bottom()
ck("lines stale grey bar", bub.bar_line._stale is True
   and "#8a8f98" in bub.bar_line.lbl_pct.styleSheet())
# fraction 钳位（>1 / <0 / 非法）
write_view({"ts": time.time(), "refresh_s": 300,
            "providers": [mk_lines_provider()], "default_id": "bailian_token_plan"})
bub.refresh_view()
bub.bar_line.set_value(1.5, "150%")
ck("bar fraction clamp high", bub.bar_line._fraction == 1.0)
bub.bar_line.set_value(-0.3, "-30%")
ck("bar fraction clamp low", bub.bar_line._fraction == 0.0)
bub.bar_line.set_value("xyz", "?")
ck("bar fraction bad -> 0", bub.bar_line._fraction == 0.0)
# 无 lines 的 provider → 旧单行排版，lines 槽+bar 隐藏
write_view({"ts": time.time(), "refresh_s": 300,
            "providers": [mk_provider("deepseek", "DeepSeek")], "default_id": "deepseek"})
bub.refresh_view()
ck("no-lines single amount shown", not bub.lbl_amount.isHidden())
ck("no-lines line slots hidden", bub.lbl_line_title.isHidden()
   and bub.lbl_line_amount.isHidden() and bub.lbl_line_info.isHidden()
   and bub.bar_line.isHidden())
# ok=false 即便带 lines 也走 error 单行
bad = mk_lines_provider()
bad["ok"] = False
bad["error"] = "没有生效中的订阅"
write_view({"ts": time.time(), "refresh_s": 300,
            "providers": [bad], "default_id": "bailian_token_plan"})
bub.refresh_view()
ck("lines ok=false error path", bub.amount_text_full == "没有生效中的订阅"
   and bub.lbl_line_amount.isHidden() and bub.bar_line.isHidden())

bub.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R) - len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)