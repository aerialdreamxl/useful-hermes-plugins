# -*- coding: utf-8 -*-
"""gateway-ui worker Fluent 设置窗口 —— SettingsWindow（CONTRACT §6/§7，设置界面 F7）。

FluentWindow 三页导航：常规 / 桌宠 / 关于。与托盘/桌宠共用同一 QApplication，
本模块**绝不创建 QApplication**；单实例由宿主 main.open_settings 管理。

读写协议（CONTRACT §6，config.yaml 唯一事实源）：
- 读：宿主每拍写 <bus_dir>/settings_view.json 快照（top/pet/explicit）。
  构造时读一次作初值；窗口开着时 QTimer 1s 按 mtime+内容签名双判轮询刷新
  （NTFS mtime 精度有限，同 tick 连写内容变了仍须处理；内容没变不空转）。
  explicit 列出 config 真实存在的键；缺键卡片副标题加"默认值/未设置"提示。
- 写：卡片改动 → request_fn(key, value, source="settings") → 宿主官方 writer
  落盘 → settings_ack.json 回执。ok → InfoBar"已保存"；fail → InfoBar 报错
  + 卡片回滚到快照值。pending rid 自己记账；未决键刷新时跳过（保乐观值）。
- **本窗口绝不打开/写 config.yaml，绝不写 pet_cfg.json。**

qfluentwidgets 1.11.3 控件坑（已实测）：
- RangeSettingCard/ComboBoxSettingCard 内部槽会 qconfig.set → 全局 qconfig.save
  写它自己的 config/config.json（不是我们要的）。解法：独立 ConfigItem 不注册
  全局 qconfig，构造后**断开卡片内部槽**（slider.valueChanged→__onValueChanged、
  comboBox.currentIndexChanged→_onCurrentIndexChanged、configItem.valueChanged
  →setValue），纯用控件信号驱动 request_fn。程序化赋值一律 blockSignals 包裹，
  防"刷新→触发→再写"回环。
- QSlider 只认整数：scale 0.1~3.0 浮点用 ×10 整数滑杆（1~30）映射，
  RangeValidator(0.1,3.0) 直接喂会被截成 0~3。
- SwitchSettingCard 不传 configItem 即用，无 qconfig 副作用。
- 关闭即停预览/轮询 QTimer（closeEvent），防泄漏。
- 所有异常收敛留证 <bus_dir>/pet_error.log（带 traceback），不炸托盘。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QDialog)

from qfluentwidgets import (
    FluentWindow, SettingCard, SettingCardGroup, SwitchSettingCard,
    RangeSettingCard, ComboBoxSettingCard, PushSettingCard, InfoBar,
    MessageBox, MessageBoxBase, LineEdit, ScrollArea, FluentIcon,
    RangeConfigItem, OptionsConfigItem, RangeValidator, OptionsValidator,
)
from qfluentwidgets.common.config import qconfig

import sprites

# ---- 缺省值镜像（与宿主 config.py TOP_DEFAULTS/PET_DEFAULTS 对齐，仅展示兜底）----
TOP_DEFAULTS = {
    "tray_enabled": True,
    "bus_enabled": True,
    "degrade_window_s": 300,
    "verbose_log": False,
    "package_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "subagent_busy": False,
}
PET_DEFAULTS = {
    "enable-pet": False,   # 三态：缺键运行时 false，但显示"未设置"
    "slug": "",
    "scale": 0.7,
    "roam": True,
    "topmost": True,
    "follow_window": True,    # R26-2：窗口跟随（显示类，与宿主 config.py 对齐）
    "vertical_roam": True,    # R26-3：垂直游走（显示类）
    "event_reactions": True,  # 事件反应：作者点名默认开（缺键按 true 读，不落盘）
}
# 余额气泡：余额气泡（与宿主 config.py BALANCE_DEFAULTS 对齐，仅展示兜底）
BALANCE_DEFAULTS = {
    "enabled": False,        # 修改类默认关（DIY 规范）
    "refresh_s": 300,
    "providers_dir": "",     # 空=回落 <home>/balance-providers
    "token_plan_edition": "individual",   # 余额气泡修订：individual|team，默认个人版
}
REFRESH_MIN, REFRESH_MAX = 60, 3600   # 余额刷新周期整数秒（与宿主钳位一致）
# 审批弹窗（与宿主 config.py APPROVAL_DEFAULTS 对齐，仅展示兜底）
APPROVAL_DEFAULTS = {
    "popup_enabled": False,    # 修改类默认关（DIY 规范）
    "popup_sounds": True,      # 显示类默认开
    "platform_takeover": False,  # R29 分流：默认不接管消息平台会话审批
}

# 包源预设（label, url）；第四项"自定义…"弹 LineEdit 输入，限 http(s)。
PKG_PRESETS = (
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里", "https://mirrors.aliyun.com/pypi/simple/"),
    ("官方", "https://pypi.org/simple"),
)
CUSTOM_SENTINEL = "__custom__"

SCALE_X10_MIN, SCALE_X10_MAX = 1, 30      # 滑杆整数 = scale×10（0.1~3.0）
WINDOW_MIN, WINDOW_MAX = 60, 1800          # 降级窗口整数秒
# N19：pet_error.log 滚动闸（与 worker/main.pet_error_log 同策略，异常通道不许无界）
PET_ERROR_MAX_BYTES = 1_000_000
PET_ERROR_KEEP_LINES = 2000

# 卡片副标题：改什么 / 会引发什么
CONTENT = {
    "tray_enabled": "托盘图标总开关。关闭会连设置窗口、桌宠一起停掉",
    "bus_enabled": "网关总线通信开关。关闭后不再接收宿主状态、不再发配置请求",
    "degrade_window_s": "自动降级等待窗口（秒，60~1800）。超时无响应按降级处理",
    "verbose_log": "详细日志开关。开启后向总线目录写 debug.log / pet_debug.log",
    "subagent_busy": "子代理忙线：无活跃会话但有子代理在跑时，托盘灯亮橙、桌宠演工作",
    "package_index_url": "pip 包安装源。影响依赖安装/更新时的下载地址",
    "enable-pet": "桌宠显示开关。关闭仅隐藏浮窗，不删除素材",
    "slug": "当前出场的 petdex 角色。切换即时重建预览与浮窗素材",
    "scale": "桌宠大小（0.1~3.0）。拖动即时重建预览与浮窗尺寸",
    "topmost": "桌宠窗口置顶。关闭后可被其他窗口遮挡",
    "roam": "桌宠游走（含爬窗）。关闭后变为静止浮窗",
    "follow_window": "窗口跟随：站在窗口边沿时随窗口移动/缩放贴线跟随；窗口关闭立即落下。关闭=靠定时确认兜底",
    "vertical_roam": "垂直游走：随机主动上爬/下爬（下爬是平滑动画），走到边沿可跳到邻接线段。关闭=只保留被动上爬",
    "event_reactions": "会话完成/失败/等人类输入时桌宠动作+托盘闪动（仅桌面端/TUI/CLI）",
    "balance_enabled": "左键单击桌宠弹出余额气泡。需先把适配器放进投放目录；密钥只走环境变量",
    "balance_refresh_s": "宿主周期查询间隔（秒，60~3600）。气泡只读缓存，点击绝不直连 API",
    "balance_providers_dir": "适配器投放目录。留空=默认 HERMES_HOME\\balance-providers（出厂三适配器已播种）",
    "balance_token_plan_edition": "百炼 Token Plan 查询版本。个人版=月额度；团队版=席位额度；所选版本无生效订阅时报错不回落",
    "approval_popup_enabled": "审批弹窗总开关。需先在 config.yaml 配 security.approval.transport: gateway-ui；只配 security 不开此开关=审批超时 fail-closed 拒绝",
    "approval_popup_sounds": "审批弹窗到达提示音。关闭=静音（无音频设备也静音）",
    "approval_platform_takeover": "消息平台会话审批接管。关（默认）=飞书/QQ 群/微信等远程审批交还原生聊天卡，弹窗只接管本机（Desktop/TUI/CLI）会话——需配 security.approval.transport_fallback: builtin；开=远程审批也弹本机窗，你不在电脑前时远程审批会等到超时",
    "approval_sample": "弹出样例审批窗口预览 UAC 式形态与四按钮。纯演示，点击不注入任何真实信号",
}
MARK_DEFAULT = "｜默认值（未写入 config）"
MARK_UNSET = "｜未设置（运行时关闭）"
_MISSING = object()   # _effective 哨兵：区分"无 pending"与"pending 值恰为 None"


class _UrlDialog(MessageBoxBase):
    """自定义包源输入框：限 https，validate 不过点确定无效。"""

    def __init__(self, current: str, parent=None):
        super().__init__(parent=parent)
        self.titleLabel = QLabel("自定义包源地址", self.widget)
        self.edit = LineEdit(self.widget)
        self.edit.setPlaceholderText("https://…（仅限 https）")
        self.edit.setText(current or "")
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")

    def validate(self) -> bool:
        # N21：只认 https——明文 http 的 pip 源可被中间人换包（供应链投毒），
        # 宿主 validate_request 同款拒收，这里提前拦，避免"填了却被拒"的来回。
        t = self.edit.text().strip().lower()
        return t.startswith("https://")

    def url(self) -> str:
        return self.edit.text().strip()


class SettingsWindow(FluentWindow):
    """Fluent 设置窗口（常规/桌宠/关于）。构造签名宿主已定，不得改。"""

    def __init__(self, meta: dict, state: dict, request_fn, home: str = ""):
        super().__init__()
        self.meta = meta or {}
        self.state = state if state is not None else {"data": None, "ts": 0.0}
        self.request_fn = request_fn
        self.home = home or ""
        self.bus_dir = Path(self.meta.get("bus_dir") or ".")

        # qconfig 全局写盘闸：卡片内部槽已断开，这里再把 save 指到 scratch 总线
        # 下的影子文件，双保险——任何漏网的 qconfig.set 也绝不写 cwd/config.json。
        # 不注册任何 ConfigItem，toDict 只含 QFluentWidgets 主题项，无业务数据。
        try:
            qconfig.file = self.bus_dir / "qfw_shadow_config.json"
        except Exception:
            pass

        # ---- 协议状态 ----
        self._pending = {}            # rid -> {"key","value"}
        self._view = None             # 最近快照（规范化后）
        self._view_mtime = 0.0
        self._view_sig = None
        self._ack_mtime = 0.0
        self._ack_sig = None
        self._default_marks = {}      # key -> 是否缺键（测试/取证可读）
        self.last_info = None         # (kind,title,content) 最近 InfoBar
        self._view_apply_count = 0
        self._ack_processed = 0
        self._slugs = None
        self._preview_key = None
        self._preview_frames = []
        self._preview_idx = 0

        # 可注入钩子（offscreen 测试 monkeypatch 用；生产走真弹窗）
        self.confirm_tray_off = self._confirm_tray_off_dialog
        self.ask_custom_url = self._ask_custom_url_dialog
        self.ask_providers_dir = self._ask_providers_dir_dialog
        # 审批弹窗样例窗口钩子（main.open_settings 注入 _show_sample_approval；
        # 未注入时按钮提示不可用）。纯预览，零配置写（A2）。
        self.show_sample_approval = None

        self._code_cmd = None
        for cand in ("code.cmd", "code.exe", "code"):
            found = shutil.which(cand)
            if found:
                self._code_cmd = found
                break

        try:
            self._build_ui()
            self._poll_view()          # 初值 = 快照（构造时读一次）
            self._update_about()
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(1000)
            self._poll_timer.timeout.connect(self._tick)
            self._poll_timer.start()
        except Exception as exc:
            self._log_error(exc)
            raise

    # ---------------------------------------------------------- 留证 ----
    def _log_error(self, exc) -> None:
        """异常收敛留证：pet_error.log 带 traceback，绝不向外炸。
        N19：与 worker/main.pet_error_log 同一套滚动策略——>1MB 截断保留尾部
        2000 行（tmp+replace）。设置窗口长开时这里才是写入主力，无闸会写爆盘。"""
        f = self.bus_dir / "pet_error.log"
        try:
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%H:%M:%S ") + "SettingsWindow: "
                         + "".join(traceback.format_exception(
                             type(exc), exc, exc.__traceback__)).rstrip() + "\n")
            if f.stat().st_size > PET_ERROR_MAX_BYTES:
                keep = f.read_text(encoding="utf-8", errors="replace").splitlines()[
                    -PET_ERROR_KEEP_LINES:]
                tmp = f.with_name(f"pet_error.log.{os.getpid()}.tmp")
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                os.replace(tmp, f)
        except Exception:
            pass

    def _info(self, kind: str, title: str, content: str) -> None:
        self.last_info = (kind, title, content)
        try:
            fn = InfoBar.success if kind == "success" else InfoBar.error
            fn(title, content, parent=self, duration=1800)
        except Exception as exc:
            self._log_error(exc)

    # ---------------------------------------------------------- UI ----
    def _page(self, object_name: str) -> tuple:
        """ScrollArea 页 + 内层垂直布局（透明底，Fluent 导航页标准做法）。"""
        sa = ScrollArea(self)
        sa.enableTransparentBackground()
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(16)
        sa.setWidget(inner)
        sa.setWidgetResizable(True)
        sa.setObjectName(object_name)
        return sa, lay

    def _build_ui(self) -> None:
        self.setWindowTitle("网关设置")
        self.resize(880, 660)

        # ---------- 常规页 ----------
        page, lay = self._page("pageGeneral")
        g1 = SettingCardGroup("托盘与总线", page)
        self.tray_card = SwitchSettingCard(
            FluentIcon.SETTING, "启用托盘", CONTENT["tray_enabled"])
        self.bus_card = SwitchSettingCard(
            FluentIcon.CONNECT, "启用总线", CONTENT["bus_enabled"])
        self.degrade_card = RangeSettingCard(
            RangeConfigItem("ui", "degrade_window_s", 300,
                            RangeValidator(WINDOW_MIN, WINDOW_MAX)),
            FluentIcon.HISTORY, "降级窗口（秒）", CONTENT["degrade_window_s"])
        self.verbose_card = SwitchSettingCard(
            FluentIcon.DOCUMENT, "详细日志", CONTENT["verbose_log"])
        self.subagent_card = SwitchSettingCard(
            FluentIcon.PEOPLE, "子代理忙线", CONTENT["subagent_busy"])
        g1.addSettingCards([self.tray_card, self.bus_card,
                            self.degrade_card, self.verbose_card,
                            self.subagent_card])
        g2 = SettingCardGroup("包源", page)
        self.pkg_card = ComboBoxSettingCard(
            OptionsConfigItem("ui", "package_index_url", PKG_PRESETS[0][1],
                              OptionsValidator([u for _, u in PKG_PRESETS]
                                               + [CUSTOM_SENTINEL])),
            FluentIcon.GLOBE, "pip 安装源", CONTENT["package_index_url"],
            texts=[t for t, _ in PKG_PRESETS] + ["自定义…"])
        g2.addSettingCard(self.pkg_card)
        # ---------- 常规页：审批弹窗组（R28-7）----------
        g8 = SettingCardGroup("审批弹窗", page)
        self.approval_card = SwitchSettingCard(
            FluentIcon.ACCEPT, "启用审批弹窗", CONTENT["approval_popup_enabled"])
        self.approval_takeover_card = SwitchSettingCard(
            FluentIcon.GLOBE, "接管消息平台会话审批", CONTENT["approval_platform_takeover"])
        self.approval_sounds_card = SwitchSettingCard(
            FluentIcon.VOLUME, "到达提示音", CONTENT["approval_popup_sounds"])
        self.approval_sample_card = PushSettingCard(
            "弹出样例窗口", FluentIcon.PLAY, "预览审批弹窗", CONTENT["approval_sample"])
        g8.addSettingCards([self.approval_card, self.approval_takeover_card,
                            self.approval_sounds_card, self.approval_sample_card])
        lay.addWidget(g1)
        lay.addWidget(g2)
        lay.addWidget(g8)
        lay.addStretch(1)
        self.addSubInterface(page, FluentIcon.SETTING, "常规")

        # ---------- 桌宠页 ----------
        page, lay = self._page("pagePet")
        g3 = SettingCardGroup("桌宠", page)
        self.enable_card = SwitchSettingCard(
            FluentIcon.ROBOT, "显示桌宠", CONTENT["enable-pet"])
        slugs = self._scan_slugs()
        self.slug_card = ComboBoxSettingCard(
            OptionsConfigItem("ui", "slug", slugs[0] if slugs else "",
                              OptionsValidator(slugs or [""])),
            FluentIcon.GAME, "宠物角色", CONTENT["slug"],
            texts=list(slugs) if slugs else ["（无库存）"])
        # 浮点 scale：×10 整数滑杆（QSlider 不认浮点，直接 0.1~3.0 会被截断）
        self.scale_card = RangeSettingCard(
            RangeConfigItem("ui", "scale_x10", 7,
                            RangeValidator(SCALE_X10_MIN, SCALE_X10_MAX)),
            FluentIcon.ZOOM, "大小", CONTENT["scale"])
        self.topmost_card = SwitchSettingCard(
            FluentIcon.PIN, "置顶", CONTENT["topmost"])
        self.roam_card = SwitchSettingCard(
            FluentIcon.MOVE, "游走（含爬窗）", CONTENT["roam"])
        self.follow_card = SwitchSettingCard(
            FluentIcon.LINK, "跟随窗口", CONTENT["follow_window"])
        self.vertical_card = SwitchSettingCard(
            FluentIcon.UP, "垂直游走", CONTENT["vertical_roam"])
        self.reactions_card = SwitchSettingCard(
            FluentIcon.MESSAGE, "事件反应", CONTENT["event_reactions"])
        g3.addSettingCards([self.enable_card, self.slug_card, self.scale_card,
                            self.topmost_card, self.roam_card, self.follow_card,
                            self.vertical_card, self.reactions_card])
        g4 = SettingCardGroup("活体预览", page)
        self.preview_label = QLabel("预览加载中…", g4)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(sprites.FRAME_W, sprites.FRAME_H)
        g4.cardLayout.addWidget(self.preview_label)
        lay.addWidget(g3)
        lay.addWidget(g4)
        lay.addStretch(1)
        self.addSubInterface(page, FluentIcon.GAME, "桌宠")

        # ---------- 余额页（余额气泡）----------
        page, lay = self._page("pageBalance")
        g7 = SettingCardGroup("余额气泡", page)
        self.balance_card = SwitchSettingCard(
            FluentIcon.IOT, "启用余额气泡", CONTENT["balance_enabled"])
        self.balance_refresh_card = RangeSettingCard(
            RangeConfigItem("ui", "balance_refresh_s", 300,
                            RangeValidator(REFRESH_MIN, REFRESH_MAX)),
            FluentIcon.HISTORY, "刷新周期（秒）", CONTENT["balance_refresh_s"])
        self.balance_edition_card = ComboBoxSettingCard(
            OptionsConfigItem("ui", "balance_token_plan_edition", "individual",
                              OptionsValidator(["individual", "team"])),
            FluentIcon.PEOPLE, "Token Plan 版本",
            CONTENT["balance_token_plan_edition"],
            texts=["个人版", "团队版"])
        self.balance_dir_card = PushSettingCard(
            "更改…", FluentIcon.FOLDER, "投放目录", CONTENT["balance_providers_dir"])
        self.balance_open_card = PushSettingCard(
            "打开", FluentIcon.FOLDER, "打开投放目录",
            "资源管理器打开当前生效的适配器目录")
        g7.addSettingCards([self.balance_card, self.balance_refresh_card,
                            self.balance_edition_card, self.balance_dir_card,
                            self.balance_open_card])
        lay.addWidget(g7)
        lay.addStretch(1)
        self.addSubInterface(page, FluentIcon.PIE_SINGLE, "余额")

        # ---------- 关于页 ----------
        page, lay = self._page("pageAbout")
        g5 = SettingCardGroup("状态", page)
        self.version_card = SettingCard(FluentIcon.INFO, "版本", "")
        self.update_card = SettingCard(FluentIcon.UPDATE, "更新状态", "")
        self.uptime_card = SettingCard(FluentIcon.HISTORY, "网关运行时长", "")
        self.state_card = SettingCard(FluentIcon.IOT, "运行状态", "")
        self.approval_channel_card = SettingCard(FluentIcon.ACCEPT, "审批通道", "")
        g5.addSettingCards([self.version_card, self.update_card,
                            self.uptime_card, self.state_card,
                            self.approval_channel_card])
        g6 = SettingCardGroup("快速打开", page)
        self.logdir_card = PushSettingCard(
            "打开", FluentIcon.FOLDER, "日志目录", "资源管理器打开 log_dir")
        self.busdir_card = PushSettingCard(
            "打开", FluentIcon.FOLDER, "总线目录", "资源管理器打开 bus_dir")
        self.vscode_card = PushSettingCard(
            "打开", FluentIcon.CODE, "VSCode 打开 HERMES_HOME",
            "用 VSCode 打开宿主主目录（未检测到 code 命令时禁用）")
        self.vscode_card.button.setEnabled(self._code_cmd is not None)
        g6.addSettingCards([self.logdir_card, self.busdir_card, self.vscode_card])
        lay.addWidget(g5)
        lay.addWidget(g6)
        lay.addStretch(1)
        self.addSubInterface(page, FluentIcon.INFO, "关于")

        # ---- 断开卡片内部 qconfig 槽（防全局 json 写盘），接自己的信号 ----
        self._detach_range(self.degrade_card)
        self._detach_range(self.scale_card)
        self._detach_range(self.balance_refresh_card)
        self._detach_combo(self.pkg_card)
        self._detach_combo(self.slug_card)
        self._detach_combo(self.balance_edition_card)
        # 版本下拉 userData 存 individual/team（ComboBoxSettingCard 默认 text 作 data）
        cb_ed = self.balance_edition_card.comboBox
        cb_ed.setItemData(0, "individual")
        cb_ed.setItemData(1, "team")

        self.tray_card.switchButton.checkedChanged.connect(self._on_tray_toggle)
        self.bus_card.switchButton.checkedChanged.connect(
            lambda v: self._request("bus_enabled", bool(v)))
        self.verbose_card.switchButton.checkedChanged.connect(
            lambda v: self._request("verbose_log", bool(v)))
        self.subagent_card.switchButton.checkedChanged.connect(
            lambda v: self._request("subagent_busy", bool(v)))
        self.degrade_card.slider.valueChanged.connect(self._on_degrade)
        self.scale_card.slider.valueChanged.connect(self._on_scale)
        self.slug_card.comboBox.currentIndexChanged.connect(self._on_slug)
        self.topmost_card.switchButton.checkedChanged.connect(
            lambda v: self._request("pet.topmost", bool(v)))
        self.roam_card.switchButton.checkedChanged.connect(
            lambda v: self._request("pet.roam", bool(v)))
        self.follow_card.switchButton.checkedChanged.connect(
            lambda v: self._request("pet.follow_window", bool(v)))
        self.vertical_card.switchButton.checkedChanged.connect(
            lambda v: self._request("pet.vertical_roam", bool(v)))
        self.reactions_card.switchButton.checkedChanged.connect(
            lambda v: self._request("pet.event_reactions", bool(v)))
        # 余额气泡：余额三键
        self.balance_card.switchButton.checkedChanged.connect(
            lambda v: self._request("balance.enabled", bool(v)))
        self.balance_refresh_card.slider.valueChanged.connect(self._on_balance_refresh)
        self.balance_edition_card.comboBox.currentIndexChanged.connect(
            self._on_balance_edition)
        self.balance_dir_card.clicked.connect(self._on_balance_dir)
        self.balance_open_card.clicked.connect(
            lambda: self._open_explorer(self._effective_balance_dir()))
        # 审批弹窗：三开关 + 样例按钮（样例纯预览，零 settings_req，A2）
        self.approval_card.switchButton.checkedChanged.connect(
            lambda v: self._request("approval.popup_enabled", bool(v)))
        self.approval_takeover_card.switchButton.checkedChanged.connect(
            lambda v: self._request("approval.platform_takeover", bool(v)))
        self.approval_sounds_card.switchButton.checkedChanged.connect(
            lambda v: self._request("approval.popup_sounds", bool(v)))
        self.approval_sample_card.clicked.connect(self._on_sample_approval)
        self.pkg_card.comboBox.currentIndexChanged.connect(self._on_pkg)
        self.enable_card.switchButton.checkedChanged.connect(
            lambda v: self._request("pet.enable-pet", bool(v)))
        self.logdir_card.clicked.connect(
            lambda: self._open_explorer(self.meta.get("log_dir", "")))
        self.busdir_card.clicked.connect(
            lambda: self._open_explorer(self.meta.get("bus_dir", "")))
        self.vscode_card.clicked.connect(self._open_vscode)

        # 预览轮播 QTimer（LOOP_MS/FRAMES_PER_STATE 节奏；关闭即停）
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(
            max(1, sprites.LOOP_MS // sprites.FRAMES_PER_STATE))
        self._preview_timer.timeout.connect(self._preview_step)

    @staticmethod
    def _detach_range(card) -> None:
        """断 RangeSettingCard 内部槽：slider→__onValueChanged→qconfig.set
        会写全局 config/config.json；configItem.valueChanged→setValue 同理。"""
        try:
            card.slider.valueChanged.disconnect(
                card._RangeSettingCard__onValueChanged)
        except (TypeError, RuntimeError):
            pass
        try:
            card.configItem.valueChanged.disconnect(card.setValue)
        except (TypeError, RuntimeError):
            pass

    @staticmethod
    def _detach_combo(card) -> None:
        """断 ComboBoxSettingCard 内部槽（_onCurrentIndexChanged→qconfig.set）。"""
        try:
            card.comboBox.currentIndexChanged.disconnect(
                card._onCurrentIndexChanged)
        except (TypeError, RuntimeError):
            pass
        try:
            card.configItem.valueChanged.disconnect(card.setValue)
        except (TypeError, RuntimeError):
            pass

    # ------------------------------------------------------ 程序化赋值 ----
    # 一律 blockSignals 包裹，防"刷新→触发→再写"回环。
    @staticmethod
    def _set_switch(card, val: bool) -> None:
        sb = card.switchButton
        sb.blockSignals(True)
        try:
            card.setChecked(bool(val))
        finally:
            sb.blockSignals(False)

    @staticmethod
    def _set_slider(card, iv: int, text: str) -> None:
        s = card.slider
        s.blockSignals(True)
        try:
            s.setValue(int(iv))
            card.valueLabel.setText(text)
        finally:
            s.blockSignals(False)

    @staticmethod
    def _set_combo_data(card, data) -> bool:
        cb = card.comboBox
        idx = cb.findData(data)
        if idx < 0:
            return False
        cb.blockSignals(True)
        try:
            cb.setCurrentIndex(idx)
        finally:
            cb.blockSignals(False)
        return True

    def _mark(self, card, key: str, explicit: bool) -> None:
        """缺键角标：副标题追加"默认值/未设置"提示（enable-pet 三态特判）。
        key 统一用总线全名（pet.xxx）；CONTENT 按叶子名存，这里剥前缀。"""
        self._default_marks[key] = not explicit
        if explicit:
            suffix = ""
        elif key == "pet.enable-pet":
            suffix = MARK_UNSET
        else:
            suffix = MARK_DEFAULT
        leaf = key[4:] if key.startswith("pet.") else key
        if key.startswith("balance."):
            leaf = "balance_" + key[8:]   # CONTENT 用 balance_enabled 等扁平名
        if key.startswith("approval."):
            leaf = "approval_" + key[len("approval."):]   # approval_popup_enabled 等
        text = CONTENT[leaf] + suffix
        if card.contentLabel.text() != text:
            card.setContent(text)

    # ---------------------------------------------------------- 读快照 ----
    def _scan_slugs(self) -> list:
        try:
            if not self.home:
                return []
            return list(sprites.installed_pets(self.home))
        except Exception as exc:
            self._log_error(exc)
            return []

    def _normalize_view(self, raw: dict) -> dict:
        top = dict(TOP_DEFAULTS)
        top.update(raw.get("top") or {})
        pet = dict(PET_DEFAULTS)
        pet.update(raw.get("pet") or {})
        bal = dict(BALANCE_DEFAULTS)
        bal.update(raw.get("balance") or {})
        appr = dict(APPROVAL_DEFAULTS)
        appr.update(raw.get("approval") or {})
        expl = raw.get("explicit") or {}
        return {"top": top, "pet": pet, "balance": bal, "approval": appr,
                "explicit": {"top": list(expl.get("top") or []),
                             "pet": list(expl.get("pet") or []),
                             "balance": list(expl.get("balance") or []),
                             "approval": list(expl.get("approval") or [])}}

    def _poll_view(self) -> None:
        """mtime+内容签名双判：内容没变不空转；同 tick 内容变了仍处理。"""
        try:
            p = self.bus_dir / "settings_view.json"
            if not p.is_file():
                if self._view is None:
                    self._view = self._normalize_view({})
                    self._apply_view()
                return
            mt = p.stat().st_mtime
            raw = p.read_text(encoding="utf-8")
            if raw == self._view_sig:
                self._view_mtime = mt
                return
            self._view_mtime = mt
            self._view_sig = raw
            self._view = self._normalize_view(json.loads(raw))
            self._apply_view()
        except Exception as exc:
            self._log_error(exc)   # 坏快照：留证不炸，保留上一份视图

    def _has_pending(self, key: str) -> bool:
        return any(p["key"] == key for p in self._pending.values())

    def _effective(self, section: str, key: str):
        """未决键取乐观值（最后一条 pending），否则取快照值。"""
        if section == "top":
            full = key
        elif section == "balance":
            full = "balance." + key
        elif section == "approval":
            full = "approval." + key
        else:
            full = "pet." + key
        val = _MISSING
        for p in self._pending.values():
            if p["key"] == full:
                val = p["value"]   # 同键多条 pending（快速拖滑杆）取最后一条=最新乐观值
        if val is not _MISSING:
            return val
        return (self._view or {}).get(section, {}).get(key)

    def _apply_view(self) -> None:
        """把快照刷到卡片（blockSignals；未决键跳过保乐观值）。"""
        v = self._view
        if v is None:
            return
        self._view_apply_count += 1
        top, pet, expl = v["top"], v["pet"], v["explicit"]

        if not self._has_pending("tray_enabled"):
            self._set_switch(self.tray_card, top["tray_enabled"])
        self._mark(self.tray_card, "tray_enabled",
                   "tray_enabled" in expl["top"])
        if not self._has_pending("bus_enabled"):
            self._set_switch(self.bus_card, top["bus_enabled"])
        self._mark(self.bus_card, "bus_enabled", "bus_enabled" in expl["top"])
        if not self._has_pending("degrade_window_s"):
            n = max(WINDOW_MIN, min(WINDOW_MAX, int(top["degrade_window_s"])))
            self._set_slider(self.degrade_card, n, str(n))
        self._mark(self.degrade_card, "degrade_window_s",
                   "degrade_window_s" in expl["top"])
        if not self._has_pending("verbose_log"):
            self._set_switch(self.verbose_card, top["verbose_log"])
        self._mark(self.verbose_card, "verbose_log",
                   "verbose_log" in expl["top"])
        if not self._has_pending("subagent_busy"):
            self._set_switch(self.subagent_card, top["subagent_busy"])
        self._mark(self.subagent_card, "subagent_busy",
                   "subagent_busy" in expl["top"])
        if not self._has_pending("package_index_url"):
            self._rebuild_pkg_combo(str(top["package_index_url"] or ""))
        self._mark(self.pkg_card, "package_index_url",
                   "package_index_url" in expl["top"])

        if not self._has_pending("pet.enable-pet"):
            self._set_switch(self.enable_card, pet["enable-pet"])
        self._mark(self.enable_card, "pet.enable-pet",
                   "enable-pet" in expl["pet"])
        # slug 选项 = 库存实时扫描；列表变了才重建（blockSignals）
        slugs = self._scan_slugs()
        if slugs != self._slugs:
            self._slugs = slugs
            self._rebuild_slug_combo(slugs, str(pet["slug"] or ""))
        elif not self._has_pending("pet.slug"):
            self._rebuild_slug_combo(slugs, str(pet["slug"] or ""))
        self._mark(self.slug_card, "pet.slug", "slug" in expl["pet"])
        if not self._has_pending("pet.scale"):
            sc = sprites.clamp_scale(float(pet["scale"]))
            iv = max(SCALE_X10_MIN, min(SCALE_X10_MAX, int(round(sc * 10))))
            self._set_slider(self.scale_card, iv, f"{iv / 10:.1f}")
        self._mark(self.scale_card, "pet.scale", "scale" in expl["pet"])
        if not self._has_pending("pet.topmost"):
            self._set_switch(self.topmost_card, pet["topmost"])
        self._mark(self.topmost_card, "pet.topmost", "topmost" in expl["pet"])
        if not self._has_pending("pet.roam"):
            self._set_switch(self.roam_card, pet["roam"])
        self._mark(self.roam_card, "pet.roam", "roam" in expl["pet"])
        if not self._has_pending("pet.follow_window"):
            self._set_switch(self.follow_card, pet.get("follow_window", True))
        self._mark(self.follow_card, "pet.follow_window",
                   "follow_window" in expl["pet"])
        if not self._has_pending("pet.vertical_roam"):
            self._set_switch(self.vertical_card, pet.get("vertical_roam", True))
        self._mark(self.vertical_card, "pet.vertical_roam",
                   "vertical_roam" in expl["pet"])
        if not self._has_pending("pet.event_reactions"):
            self._set_switch(self.reactions_card, pet["event_reactions"])
        self._mark(self.reactions_card, "pet.event_reactions",
                   "event_reactions" in expl["pet"])

        # 余额气泡：余额三键（缺键角标同款；providers_dir 卡片副标题带当前生效路径）
        bal = v.get("balance") or BALANCE_DEFAULTS
        if not self._has_pending("balance.enabled"):
            self._set_switch(self.balance_card, bal["enabled"])
        self._mark(self.balance_card, "balance.enabled",
                   "enabled" in expl.get("balance", []))
        if not self._has_pending("balance.refresh_s"):
            n = max(REFRESH_MIN, min(REFRESH_MAX, int(bal["refresh_s"])))
            self._set_slider(self.balance_refresh_card, n, str(n))
        self._mark(self.balance_refresh_card, "balance.refresh_s",
                   "refresh_s" in expl.get("balance", []))
        if not self._has_pending("balance.token_plan_edition"):
            self._set_combo_data(self.balance_edition_card,
                                 str(bal.get("token_plan_edition") or "individual"))
        self._mark(self.balance_edition_card, "balance.token_plan_edition",
                   "token_plan_edition" in expl.get("balance", []))
        self._mark(self.balance_dir_card, "balance.providers_dir",
                   "providers_dir" in expl.get("balance", []))
        self.balance_dir_card.setContent(
            CONTENT["balance_providers_dir"] + "｜当前：" + self._effective_balance_dir())

        # 审批弹窗：三开关（缺键角标同款；修改类缺省 false）
        appr = v.get("approval") or APPROVAL_DEFAULTS
        if not self._has_pending("approval.popup_enabled"):
            self._set_switch(self.approval_card, appr.get("popup_enabled", False))
        self._mark(self.approval_card, "approval.popup_enabled",
                   "popup_enabled" in expl.get("approval", []))
        if not self._has_pending("approval.platform_takeover"):
            self._set_switch(self.approval_takeover_card,
                             appr.get("platform_takeover", False))
        self._mark(self.approval_takeover_card, "approval.platform_takeover",
                   "platform_takeover" in expl.get("approval", []))
        if not self._has_pending("approval.popup_sounds"):
            self._set_switch(self.approval_sounds_card, appr.get("popup_sounds", True))
        self._mark(self.approval_sounds_card, "approval.popup_sounds",
                   "popup_sounds" in expl.get("approval", []))

        # 预览：换宠/拉大小即时重建（未决键用乐观值）
        slug = str(self._effective("pet", "slug") or "")
        scale = float(self._effective("pet", "scale") or PET_DEFAULTS["scale"])
        key = (slug, round(scale, 1))
        if key != self._preview_key:
            self._rebuild_preview(slug, scale)

    def _rebuild_slug_combo(self, slugs: list, current: str) -> None:
        cb = self.slug_card.comboBox
        cb.blockSignals(True)
        try:
            cb.clear()
            for s in slugs:
                cb.addItem(s, userData=s)
            if current not in slugs:
                label = f"{current}（未安装）" if current else "（未设置）"
                cb.addItem(label, userData=current)
            idx = cb.findData(current)
            if idx >= 0:
                cb.setCurrentIndex(idx)
        finally:
            cb.blockSignals(False)

    def _rebuild_pkg_combo(self, current: str) -> None:
        cb = self.pkg_card.comboBox
        cb.blockSignals(True)
        try:
            cb.clear()
            for label, url in PKG_PRESETS:
                cb.addItem(label, userData=url)
            preset_urls = [u for _, u in PKG_PRESETS]
            if current and current not in preset_urls:
                cb.addItem(f"自定义：{current}", userData=current)
            cb.addItem("自定义…", userData=CUSTOM_SENTINEL)
            idx = cb.findData(current)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            cb.blockSignals(False)

    # ---------------------------------------------------------- 回执 ----
    def _poll_ack(self) -> None:
        try:
            p = self.bus_dir / "settings_ack.json"
            if not p.is_file():
                return
            mt = p.stat().st_mtime
            raw = p.read_text(encoding="utf-8")
            if raw == self._ack_sig:
                self._ack_mtime = mt
                return
            self._ack_mtime = mt
            self._ack_sig = raw
            ack = json.loads(raw)
            rid = str(ack.get("id") or "")
            pend = self._pending.pop(rid, None)
            if pend is None:
                return    # 不是本窗口发的（桌宠菜单等），忽略
            self._ack_processed += 1
            key = pend["key"]
            if ack.get("ok"):
                self._info("success", "已保存",
                           f"{key} = {ack.get('value', pend['value'])!r}")
            else:
                err = str(ack.get("error") or "未知错误")
                self._info("error", "保存失败", f"{key}：{err}")
                self._rollback(key)
        except Exception as exc:
            self._log_error(exc)

    def _rollback(self, key: str) -> None:
        """ack fail：卡片回滚到快照值（blockSignals，绝不触发再写）。"""
        v = self._view or self._normalize_view({})
        try:
            if key == "tray_enabled":
                self._set_switch(self.tray_card, v["top"]["tray_enabled"])
            elif key == "bus_enabled":
                self._set_switch(self.bus_card, v["top"]["bus_enabled"])
            elif key == "degrade_window_s":
                n = max(WINDOW_MIN, min(WINDOW_MAX,
                                        int(v["top"]["degrade_window_s"])))
                self._set_slider(self.degrade_card, n, str(n))
            elif key == "verbose_log":
                self._set_switch(self.verbose_card, v["top"]["verbose_log"])
            elif key == "subagent_busy":
                self._set_switch(self.subagent_card, v["top"]["subagent_busy"])
            elif key == "package_index_url":
                self._rebuild_pkg_combo(str(v["top"]["package_index_url"] or ""))
            elif key == "pet.enable-pet":
                self._set_switch(self.enable_card, v["pet"]["enable-pet"])
            elif key == "pet.slug":
                self._rebuild_slug_combo(self._slugs or [],
                                         str(v["pet"]["slug"] or ""))
                self._rebuild_preview(str(v["pet"]["slug"] or ""),
                                      float(v["pet"]["scale"]))
            elif key == "pet.scale":
                sc = sprites.clamp_scale(float(v["pet"]["scale"]))
                iv = max(SCALE_X10_MIN, min(SCALE_X10_MAX, int(round(sc * 10))))
                self._set_slider(self.scale_card, iv, f"{iv / 10:.1f}")
                self._rebuild_preview(str(v["pet"]["slug"] or ""), sc)
            elif key == "pet.topmost":
                self._set_switch(self.topmost_card, v["pet"]["topmost"])
            elif key == "pet.roam":
                self._set_switch(self.roam_card, v["pet"]["roam"])
            elif key == "pet.follow_window":
                self._set_switch(self.follow_card,
                                 v["pet"].get("follow_window", True))
            elif key == "pet.vertical_roam":
                self._set_switch(self.vertical_card,
                                 v["pet"].get("vertical_roam", True))
            elif key == "pet.event_reactions":
                self._set_switch(self.reactions_card, v["pet"]["event_reactions"])
            elif key == "balance.enabled":
                self._set_switch(self.balance_card,
                                 v.get("balance", BALANCE_DEFAULTS)["enabled"])
            elif key == "balance.refresh_s":
                n = max(REFRESH_MIN, min(REFRESH_MAX,
                        int(v.get("balance", BALANCE_DEFAULTS)["refresh_s"])))
                self._set_slider(self.balance_refresh_card, n, str(n))
            elif key == "balance.providers_dir":
                self.balance_dir_card.setContent(
                    CONTENT["balance_providers_dir"] + "｜当前："
                    + self._effective_balance_dir())
            elif key == "balance.token_plan_edition":
                self._set_combo_data(
                    self.balance_edition_card,
                    str(v.get("balance", BALANCE_DEFAULTS).get(
                        "token_plan_edition") or "individual"))
            elif key == "approval.popup_enabled":
                self._set_switch(self.approval_card,
                                 v.get("approval", APPROVAL_DEFAULTS).get(
                                     "popup_enabled", False))
            elif key == "approval.platform_takeover":
                self._set_switch(self.approval_takeover_card,
                                 v.get("approval", APPROVAL_DEFAULTS).get(
                                     "platform_takeover", False))
            elif key == "approval.popup_sounds":
                self._set_switch(self.approval_sounds_card,
                                 v.get("approval", APPROVAL_DEFAULTS).get(
                                     "popup_sounds", True))
        except Exception as exc:
            self._log_error(exc)

    # ---------------------------------------------------------- 写请求 ----
    def _request(self, key: str, value):
        """唯一写入口：request_fn → 宿主官方 writer。绝不自己碰 config.yaml。"""
        try:
            rid = self.request_fn(key, value, source="settings")
            if rid:
                self._pending[str(rid)] = {"key": key, "value": value}
            return rid
        except Exception as exc:
            self._log_error(exc)
            self._info("error", "请求发送失败", f"{key}：{exc}")
            return None

    def _on_tray_toggle(self, checked: bool) -> None:
        """关托盘二次确认（可注入 confirm_tray_off）；取消则开关回弹。"""
        if checked:
            self._request("tray_enabled", True)
            return
        try:
            ok = bool(self.confirm_tray_off())
        except Exception as exc:
            self._log_error(exc)
            ok = False
        if ok:
            self._request("tray_enabled", False)
        else:
            self._set_switch(self.tray_card, True)   # 回弹（blockSignals）

    def _on_degrade(self, iv: int) -> None:
        n = max(WINDOW_MIN, min(WINDOW_MAX, int(iv)))
        self.degrade_card.valueLabel.setText(str(n))
        self._request("degrade_window_s", n)

    def _on_scale(self, iv: int) -> None:
        iv = max(SCALE_X10_MIN, min(SCALE_X10_MAX, int(iv)))
        sc = sprites.clamp_scale(iv / 10)
        self.scale_card.valueLabel.setText(f"{sc:.1f}")
        self._request("pet.scale", sc)
        slug = str(self._effective("pet", "slug") or "")
        self._rebuild_preview(slug, sc)   # 拉大小即时重建预览

    def _on_slug(self, idx: int) -> None:
        data = self.slug_card.comboBox.itemData(idx)
        if data is None:
            return
        self._request("pet.slug", str(data))
        scale = float(self._effective("pet", "scale")
                      or PET_DEFAULTS["scale"])
        self._rebuild_preview(str(data), scale)   # 换宠即时重建预览

    def _on_pkg(self, idx: int) -> None:
        data = self.pkg_card.comboBox.itemData(idx)
        if data is None:
            return
        current = str((self._view or {}).get("top", {}).get(
            "package_index_url") or "")
        if data == CUSTOM_SENTINEL:
            try:
                url = self.ask_custom_url(current)
            except Exception as exc:
                self._log_error(exc)
                url = None
            if url is None:
                self._rebuild_pkg_combo(current)   # 取消：回弹快照
                return
            url = str(url).strip()
            if not url.lower().startswith("https://"):
                self._info("error", "地址无效",
                           "包源地址仅限 https://（明文 http 源可被中间人换包，宿主会拒收）")
                self._rebuild_pkg_combo(current)
                return
            self._request("package_index_url", url)
            self._rebuild_pkg_combo(url)
            return
        self._request("package_index_url", str(data))

    # ---------------------------------------------------------- 弹窗 ----
    def _confirm_tray_off_dialog(self) -> bool:
        try:
            box = MessageBox("关闭托盘？",
                             "关闭托盘会连设置窗口一起停掉，确定？", self)
            return box.exec() == QDialog.Accepted
        except Exception as exc:
            self._log_error(exc)
            return False

    def _ask_custom_url_dialog(self, current: str):
        try:
            dlg = _UrlDialog(current, self)
            if dlg.exec() == QDialog.Accepted and dlg.validate():
                return dlg.url()
            return None
        except Exception as exc:
            self._log_error(exc)
            return None

    # ---- 余额气泡：余额三键处理 ----
    def _effective_balance_dir(self) -> str:
        """当前生效投放目录：显式 providers_dir 优先，空=回落 <home>/balance-providers。"""
        d = str(self._effective("balance", "providers_dir") or "").strip()
        if d:
            return d
        home = self.home or str(self.meta.get("hermes_home") or "")
        return str(Path(home) / "balance-providers") if home else "（未设置 HERMES_HOME）"

    def _on_balance_refresh(self, iv: int) -> None:
        n = max(REFRESH_MIN, min(REFRESH_MAX, int(iv)))
        self.balance_refresh_card.valueLabel.setText(str(n))
        self._request("balance.refresh_s", n)

    def _on_balance_edition(self, idx: int) -> None:
        data = self.balance_edition_card.comboBox.itemData(idx)
        if data is None:
            return
        self._request("balance.token_plan_edition", str(data))

    def _on_sample_approval(self) -> None:
        """审批弹窗「弹出样例窗口」：调 main 注入的钩子（纯预览，零 settings_req，A2）。
        钩子未注入（独立构造/旧宿主）→ 提示不可用，绝不自己碰总线。"""
        fn = self.show_sample_approval
        if fn is None:
            self._info("error", "样例窗口不可用", "审批弹窗管理器未就绪（请经托盘设置入口打开）")
            return
        try:
            fn()
        except Exception as exc:
            self._log_error(exc)
            self._info("error", "样例窗口弹出失败", str(exc))

    def _on_balance_dir(self) -> None:
        """投放目录：弹文件夹选择（可注入 ask_providers_dir）。空串=恢复默认。"""
        current = str(self._effective("balance", "providers_dir") or "")
        try:
            chosen = self.ask_providers_dir(current)
        except Exception as exc:
            self._log_error(exc)
            chosen = None
        if chosen is None:
            return   # 取消：不动
        self._request("balance.providers_dir", str(chosen).strip())

    def _ask_providers_dir_dialog(self, current: str):
        try:
            from PySide6.QtWidgets import QFileDialog
            start = current or self._effective_balance_dir()
            d = QFileDialog.getExistingDirectory(self, "选择适配器投放目录", start)
            return d or None   # 取消返回 None；选空目录也按 None（不写）
        except Exception as exc:
            self._log_error(exc)
            return None

    # ---------------------------------------------------------- 预览 ----
    def _rebuild_preview(self, slug: str, scale: float) -> None:
        """活体预览：PetSheet idle 行裁帧轮播；素材缺失占位不炸。"""
        self._preview_key = (slug, round(float(scale), 1))
        self._preview_timer.stop()
        self._preview_frames = []
        self._preview_idx = 0
        path = None
        try:
            if self.home and slug:
                path = sprites.sheet_path(self.home, slug)
        except Exception as exc:
            self._log_error(exc)
        if path is None:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(
                f"素材缺失：{slug or '（未设置）'} 未安装或缺 spritesheet")
            return
        try:
            sheet = sprites.PetSheet(path, sprites.clamp_scale(scale))
            frames = sheet.frames(sprites.row_index("idle", sheet.rows))
        except Exception as exc:
            self._log_error(exc)
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"素材解码失败：{slug}")
            return
        if not frames:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"素材为空：{slug}")
            return
        self._preview_frames = frames
        self.preview_label.setText("")
        self.preview_label.setPixmap(frames[0])
        self.preview_label.setFixedSize(
            max(sprites.FRAME_W, frames[0].width()) + 8,
            max(sprites.FRAME_H, frames[0].height()) + 8)
        self._preview_timer.start()

    def _preview_step(self) -> None:
        try:
            if not self._preview_frames:
                return
            self._preview_idx = (self._preview_idx + 1) % len(self._preview_frames)
            self.preview_label.setPixmap(self._preview_frames[self._preview_idx])
        except Exception as exc:
            self._log_error(exc)

    # ---------------------------------------------------------- 关于 ----
    def _update_about(self) -> None:
        try:
            data = (self.state or {}).get("data") or {}
            tray = data.get("tray") or {}
            ver = self.meta.get("version") or "unknown"
            self.version_card.setContent(f"gateway-ui {ver}")
            self._update_approval_channel()
            if not data:
                self.update_card.setContent("无状态数据（网关未连接）")
                self.uptime_card.setContent("—")
                self.state_card.setContent("—")
                return
            upd = "有可用更新" if tray.get("update_available") else "已是最新"
            tier = tray.get("tier") or "unknown"
            self.update_card.setContent(f"{upd}｜通道：{tier}")
            up = data.get("gateway_uptime_s")
            if up is None:
                up = max(0.0, time.time() - float(data.get("boot_ts") or time.time()))
            up = float(up)
            h, m, s = int(up // 3600), int(up % 3600 // 60), int(up % 60)
            self.uptime_card.setContent(f"{h}h {m}m {s}s")
            self.state_card.setContent(
                f"桌宠：{data.get('pet_state', '?')}｜"
                f"活跃会话：{data.get('active_sessions', 0)}｜"
                f"灯：{tray.get('light', '?')}")
        except Exception as exc:
            self._log_error(exc)

    def _update_approval_channel(self) -> None:
        """关于页审批通道卡：transport 是否接管 + 搭配提醒（R28-7 / R29 分流）。
        只配 security 不开 popup_enabled → 黄色警告（审批会超时 fail-closed）；
        分流交还（takeover 关）但没配 transport_fallback: builtin → 同样警告
        （交还即被快速拒绝，远程审批断头）。"""
        try:
            appr = (self._view or {}).get("approval") or {}
            selected = bool(appr.get("transport_selected", False))
            enabled = bool(self._effective("approval", "popup_enabled"))
            takeover = bool(self._effective("approval", "platform_takeover"))
            fb = bool(appr.get("fallback_builtin", False))
            if not selected:
                self.approval_channel_card.setContent(
                    "未接管（审批走内置面）｜如需弹窗：config.yaml 配 "
                    "security.approval.transport: gateway-ui")
            elif enabled and takeover:
                self.approval_channel_card.setContent(
                    "已接管（全部会话）｜远程审批也弹本机窗")
            elif enabled:
                note = "远程审批交还原生聊天卡" if fb else \
                    "⚠ 交还将被快速拒绝——须配 security.approval.transport_fallback: builtin"
                self.approval_channel_card.setContent(
                    f"已接管（仅本机会话）｜{note}")
            else:
                self.approval_channel_card.setContent(
                    "⚠ 已接管但弹窗未启用——审批将超时拒绝，请开启「启用审批弹窗」")
        except Exception as exc:
            self._log_error(exc)

    def _open_explorer(self, path: str) -> None:
        try:
            if path:
                subprocess.Popen(["explorer", str(path)])
        except Exception as exc:
            self._log_error(exc)

    def _open_vscode(self) -> None:
        try:
            if not self._code_cmd or not self.home:
                return
            if self._code_cmd.lower().endswith(".cmd"):
                subprocess.Popen(f'"{self._code_cmd}" "{self.home}"', shell=True)
            else:
                subprocess.Popen([self._code_cmd, self.home])
        except Exception as exc:
            self._log_error(exc)

    # ---------------------------------------------------------- 轮询 ----
    def _tick(self) -> None:
        try:
            self._poll_view()
            self._poll_ack()
            self._update_about()
        except Exception as exc:
            self._log_error(exc)

    def closeEvent(self, e) -> None:
        """关闭即销毁预览/轮询 QTimer，防泄漏。"""
        try:
            self._preview_timer.stop()
            self._poll_timer.stop()
        except Exception:
            pass
        super().closeEvent(e)
