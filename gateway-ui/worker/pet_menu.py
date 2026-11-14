# -*- coding: utf-8 -*-
"""gateway-ui worker 桌宠菜单 —— PetMenu + _StayOpenFilter（CONTRACT §7）。

菜单所有权所有权纪律：托盘"桌宠"槽位与桌宠右键=**同一个 PetMenu 实例**；
QAction 生于父菜单永不跨菜单搬移（搬移=原 owner 析构连坐销毁→空菜单/崩溃）；
aboutToShow 只刷 checked；库存变化只重建自己的叶子。
菜单驻留 BUG4 驻留：过滤器装 view.viewport()（不是 menu 对象——装 menu 上
收不到子控件事件=死代码），view.itemAt().data(UserRole) 取项（QMenu.actionAt
在 Fluent 自绘下不可靠）；勾选项 trigger() 后消费 release（禁止手动
setChecked 双翻转）；子菜单入口放行。CHECK/RADIO 分型（CheckableMenu）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QObject, QEvent
from PySide6.QtGui import QAction

from qfluentwidgets import RoundMenu, CheckableMenu, MenuIndicatorType

import sprites

# 知名桌宠平台预留位（菜单所有权指令5）。出真实解析器前=禁用占位。
FUTURE_PLATFORMS = ("Shimeji", "DaoCute", "eSheep", "DesktopMascot")


class _StayOpenFilter(QObject):
    """菜单驻留 BUG4：点击可勾选项后菜单树不该整个消失。

    Fluent RoundMenu 点击链路（源码实锤 menu.py）：鼠标落在 view.viewport()
    → view.itemClicked → RoundMenu._onItemClicked → _hideMenu + trigger()。
    过滤器必须装在 view.viewport()。

    取项走 Fluent 原生方式 view.itemAt(pos).data(Qt.UserRole)：
    UserRole=QAction 是普通项，=RoundMenu 是子菜单入口。
    - 勾选项（CHECK/RADIO）：trigger()（checkable 项自带 toggle，禁止再手动
      setChecked——双翻转会回到原状态，实测教训）+ 返回 True 消费 release →
      itemClicked 不发射 → 菜单驻留，鼠标移开由 Fluent hover 逻辑自然折叠；
    - 子菜单入口：放行。Fluent _onItemClicked 对 itemWidget 项在 _hideMenu
      之前就 return（源码实锤），点击入口本来就不关菜单，无需拦截。"""

    def eventFilter(self, obj, ev):
        try:
            if ev.type() == QEvent.MouseButtonRelease \
                    and ev.button() == Qt.LeftButton:
                menu, view = self.targets
                item = view.itemAt(ev.pos())
                if item is None:
                    return False
                data = item.data(Qt.UserRole)
                if isinstance(data, RoundMenu):
                    return False  # 子菜单入口：放行默认
                act = data
                if not isinstance(act, QAction) or not act.isEnabled():
                    return False
                if act.menu() is not None:
                    return False
                if act.isCheckable() or isinstance(menu, CheckableMenu):
                    act.trigger()
                    return True  # 消费：不 hide，菜单驻留
        except Exception:
            pass
        return super().eventFilter(obj, ev)


class PetMenu:
    """桌宠配置菜单——托盘"桌宠"槽位与桌宠本体右键共享的**唯一实例**。

    一级 CHECK 对勾：显示桌宠/游走（含爬窗）/置顶；
    二级 切换宠物▸{Petdex▸{库存 RADIO 圆点} | 四平台禁用位}、
    大小▸{小0.5/中0.7/大1.0 RADIO}。
    """

    def __init__(self, get_cfg, set_cfg, home, on_change=None):
        self.get_cfg = get_cfg
        self.set_cfg = set_cfg
        self.home = home
        self.on_change = on_change
        self.menu = CheckableMenu("桌宠", parent=None,
                                  indicatorType=MenuIndicatorType.CHECK)
        self.a_show = QAction("显示桌宠", self.menu)
        self.a_show.triggered.connect(lambda c: self._w("enable-pet", bool(c)))
        self.a_roam = QAction("游走（含爬窗）", self.menu)
        self.a_roam.triggered.connect(lambda c: self._w("roam", bool(c)))
        self.a_top = QAction("置顶", self.menu)
        self.a_top.triggered.connect(lambda c: self._w("topmost", bool(c)))
        self.menu.addActions([self.a_show, self.a_roam, self.a_top])
        self.menu.addSeparator()
        self.sub_switch = RoundMenu("切换宠物", self.menu)
        self.sub_pets = CheckableMenu("Petdex", self.sub_switch,
                                      indicatorType=MenuIndicatorType.RADIO)
        self.sub_switch.addMenu(self.sub_pets)
        for label in FUTURE_PLATFORMS:
            a = QAction(f"{label}（未支持）", self.sub_switch)
            a.setEnabled(False)
            self.sub_switch.addAction(a)
        self.menu.addMenu(self.sub_switch)
        self.sub_size = CheckableMenu("大小", self.menu,
                                      indicatorType=MenuIndicatorType.RADIO)
        self.size_acts = {}
        for label, val in (("小 0.5", 0.5), ("中 0.7", 0.7), ("大 1.0", 1.0)):
            a = QAction(label, self.sub_size)
            a.setCheckable(True)  # 菜单驻留补：RADIO 指示器需要 checkable（此前漏）
            a.triggered.connect(lambda _=False, v=val: self._w("scale", v))
            self.size_acts[val] = a
            self.sub_size.addAction(a)
        self.menu.addMenu(self.sub_size)
        self._pet_slugs = None
        self.refresh()
        self.menu.aboutToShow.connect(self.refresh)
        # 菜单驻留：可勾选项点击后菜单驻留（含二级子菜单）。
        # 每个菜单一个 filter（targets=(menu, view)），装到 view.viewport()。
        self._stays = []
        for m in (self.menu, self.sub_switch, self.sub_pets, self.sub_size):
            f = _StayOpenFilter(m)
            f.targets = (m, m.view)
            m.view.viewport().installEventFilter(f)
            self._stays.append(f)

    def _w(self, key, value):
        self.set_cfg(key, value)
        self.refresh()
        if self.on_change:
            self.on_change()

    def current_slug(self):
        cfg = self.get_cfg()
        pets = sprites.installed_pets(self.home)
        return (cfg.get("slug") or "") or (pets[0] if pets else ""), pets

    def _rebuild_pets(self):
        cur, pets = self.current_slug()
        self.sub_pets.clear()
        for slug in pets:
            act = QAction(slug, self.sub_pets)
            act.setCheckable(True)
            act.setChecked(slug == cur)
            act.triggered.connect(lambda _=False, s=slug: self._w("slug", s))
            self.sub_pets.addAction(act)
        if not pets:
            a = QAction("（未安装 petdex 宠物）", self.sub_pets)
            a.setEnabled(False)
            self.sub_pets.addAction(a)
        self._pet_slugs = tuple(pets)

    def refresh(self):
        cfg = self.get_cfg()
        for act, key, dflt in ((self.a_show, "enable-pet", False),
                               (self.a_roam, "roam", True),
                               (self.a_top, "topmost", True)):
            act.blockSignals(True)
            act.setCheckable(True)
            act.setChecked(bool(cfg.get(key, dflt)))
            act.blockSignals(False)
        cur_scale = float(cfg.get("scale", 0.7) or 0.7)
        for val, act in self.size_acts.items():
            act.blockSignals(True)
            act.setChecked(abs(val - cur_scale) < 1e-6)
            act.blockSignals(False)
        if tuple(sprites.installed_pets(self.home)) != self._pet_slugs:
            self._rebuild_pets()
        else:
            cur, _ = self.current_slug()
            for act in self.sub_pets.actions():
                if act.isCheckable():
                    act.blockSignals(True)
                    act.setChecked(act.text() == cur)
                    act.blockSignals(False)

    def exec_at(self, pos):
        self.refresh()
        self.menu.exec(pos)
