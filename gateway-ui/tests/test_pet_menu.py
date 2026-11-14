# -*- coding: utf-8 -*-
"""test_pet_menu.py — 菜单驻留 BUG4 二级菜单驻留断言全量移植（旧 menu_stay19_test.py
13 断言，真实事件路由：release 投递到 view.viewport() → 已装过滤器消费 → 菜单树不 hide）。
side-env python 跑（offscreen Qt）。另加 CHECK/RADIO 分型与四平台禁用位核对。"""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF, Qt, QEvent
from PySide6.QtGui import QMouseEvent

app = QApplication([])

from pet_menu import PetMenu, FUTURE_PLATFORMS

from _env import HOME  # noqa: E402

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

CFG = {"enable-pet": True, "slug": "fairy", "scale": 0.7, "roam": True, "topmost": True}
writes = []

def get_cfg():
    return dict(CFG)

def set_cfg(key, value):
    writes.append((key, value))
    CFG[key] = value

pm = PetMenu(get_cfg, set_cfg, HOME)
menu = pm.menu


def viewport_point_of(m, action):
    """action 对应 item 在 view.viewport() 坐标系下的中心点。"""
    item = action.property("item")
    rect = m.view.visualItemRect(item)
    return QPointF(rect.center())


def real_release(m, pos):
    """把左键 release 真实投递到 viewport（经过已安装的 event filter）。"""
    vp = m.view.viewport()
    ev = QMouseEvent(QEvent.MouseButtonRelease, pos,
                     vp.mapToGlobal(pos.toPoint()),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    return QApplication.sendEvent(vp, ev)


def find_act(m, frag):
    for a in m.menuActions():
        if frag in a.text():
            return a
    return None


# ---- 一级 CHECK 项：点击后驻留（旧 13 断言）----
menu.popup(QPointF(100, 100).toPoint())
app.processEvents()
ck("menu visible after popup", menu.isVisible())

act_show = find_act(menu, "显示桌宠")
ck("found checkable action", act_show is not None and act_show.isCheckable())

real_release(menu, viewport_point_of(menu, act_show))
app.processEvents()
ck("level-1 menu STILL visible after click", menu.isVisible())
ck("enable-pet toggled to False", ("enable-pet", False) in writes)
ck("check state flipped", act_show.isChecked() is False)

writes.clear()
real_release(menu, viewport_point_of(menu, act_show))
app.processEvents()
ck("2nd click stays open", menu.isVisible())
ck("enable-pet toggled back True", ("enable-pet", True) in writes)

# ---- 二级 RADIO 子菜单（大小）：点击后驻留 ----
pm.sub_size.popup(QPointF(320, 100).toPoint())
app.processEvents()
act_big = find_act(pm.sub_size, "大 1.0")
ck("found 大 1.0 action", act_big is not None)
real_release(pm.sub_size, viewport_point_of(pm.sub_size, act_big))
app.processEvents()
ck("level-2 submenu STILL visible", pm.sub_size.isVisible())
ck("level-2 radio checked after click", act_big.isChecked())
ck("scale written 1.0", CFG.get("scale") == 1.0)

# ---- 子菜单入口（"切换宠物" itemWidget 项）：filter 放行 ----
sw_item = None
for i in range(menu.view.count()):
    it = menu.view.item(i)
    if it is not None and "切换宠物" in (it.text() or ""):
        sw_item = it
        break
ck("submenu entry item located", sw_item is not None)
if sw_item is not None:
    r = menu.view.visualItemRect(sw_item)
    vp = menu.view.viewport()
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(r.center()),
                     vp.mapToGlobal(r.center()),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(vp, ev)
    app.processEvents()
    ck("submenu-entry click keeps menu open", menu.isVisible())

menu.close()
pm.sub_size.close()

# ---- 新增：CHECK/RADIO 分型 + 四平台禁用位 + 大小三档 RADIO ----
# CheckableMenu 把 indicatorType 落成 view 的 itemDelegate 类型（源码实锤）。
def delegate_kind(m):
    return type(m.view.itemDelegate()).__name__
ck("level-1 menu is CHECK indicator", delegate_kind(pm.menu) == "CheckIndicatorMenuItemDelegate")
ck("petdex submenu is RADIO indicator", delegate_kind(pm.sub_pets) == "RadioIndicatorMenuItemDelegate")
ck("size submenu is RADIO indicator", delegate_kind(pm.sub_size) == "RadioIndicatorMenuItemDelegate")
ck("four future platforms present", len(FUTURE_PLATFORMS) == 4)
disabled_labels = [a.text() for a in pm.sub_switch.menuActions() if not a.isEnabled()]
ck("future platforms all disabled", all(any(p in t for p in FUTURE_PLATFORMS) for t in disabled_labels) and len(disabled_labels) >= 4)
ck("size has three RADIO tiers", len(pm.size_acts) == 3 and all(a.isCheckable() for a in pm.size_acts.values()))

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
