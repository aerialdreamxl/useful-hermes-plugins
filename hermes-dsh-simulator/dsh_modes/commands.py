# -*- coding: utf-8 -*-
"""The ``/dshsim`` slash command.

* ``/dshsim``              -> status / usage
* ``/dshsim minimal``      -> take over THIS session with the DSH minimal
                              replica — only accepted in a message-less
                              session (''改不了了'' otherwise). Registers the
                              session in the persistent take-over store
                              (state.py) and starts labeling its title.
* ``/dshsim optout``       -> drop the takeover (mode + store entry + title
                              label); available at ANY time.
"""

from __future__ import annotations

from . import inject, registry, sessions, state, titles
from .registry import MODE_MINIMAL

USAGE = "用法：/dshsim minimal ｜ /dshsim optout（不带参数＝查看状态）"


def handle(raw_args: str):
    arg = (raw_args or "").strip().lower()
    if arg in ("", "status", "?", "help"):
        return _status()
    if arg in ("minimal", "simple", "on"):
        return _enable_minimal()
    if arg in ("optout", "off", "native", "reset"):
        return _optout()
    return "未知参数：%s\n%s" % (arg, USAGE)


def _enable_minimal() -> str:
    session_key, surface, reason = sessions.resolve_session()
    if not session_key:
        return "⚠️ 无法确定当前会话（%s）。请在桌面端或 CLI 的新会话里使用。" % reason
    ok, _why = sessions.can_enable(session_key)
    if not ok:
        return (
            "🙅 改不了了——这个会话已经有消息了。\n"
            "DSH 极简模式只接受全新会话：先 /dshsim minimal，再发第一条消息。\n"
            "（任何时候都可以用 /dshsim optout 还原）"
        )
    registry.set_mode(session_key, MODE_MINIMAL)
    state.add(session_key, surface=surface)        # persist: survives restarts
    inject.ensure_session_tools(session_key)       # best effort; the middleware re-asserts on the first request
    titles.ensure_label(session_key)               # best effort; re-asserted per request
    return (
        "✅ 已接管本会话：DSH 极简模式（minimal）\n"
        "• 系统提示词 → 覆写为 DSH 极简原文（下一轮请求起生效）\n"
        "• 模型工具面 → 仅 bash / str_replace_editor（1:1 DSH schema，背后静默转发 Hermes 工具）\n"
        "• 面外工具的调用会被兜底拒绝（正常流程中模型看不到它们）\n"
        "• 已登记持久化接管清单（重启后自动续管）；会话名将带 [DSHSIM] 标\n"
        "还原：/dshsim optout（任何时候可用）"
    )


def _optout() -> str:
    session_key, _surface, reason = sessions.resolve_session()
    if not session_key:
        return "⚠️ 无法确定当前会话（%s）；未做任何更改。" % reason
    was_mode = registry.clear_mode(session_key)
    was_stored = state.remove(session_key)
    was_labeled = titles.strip_label(session_key)
    if was_mode or was_stored or was_labeled:
        inject.strip_session_tools(session_key)    # best effort; payload-strip + rebuilds converge the rest
        return "↩️ 已取消接管：本会话恢复 Hermes Agent 默认行为（已移出接管清单、[DSHSIM] 标已移除）。"
    return "ℹ️ 本会话当前就是原生行为（未接管），无需取消。"


def _status() -> str:
    session_key, _surface, reason = sessions.resolve_session()
    if not session_key:
        return "状态：无法确定当前会话（%s）。\n%s" % (reason, USAGE)
    if registry.get_mode(session_key) == MODE_MINIMAL:
        return "当前会话：DSH 极简模式（minimal）已接管（持久化清单已登记）。optout 可随时还原。"
    return "当前会话：Hermes 原生行为（未接管）。\n" + USAGE