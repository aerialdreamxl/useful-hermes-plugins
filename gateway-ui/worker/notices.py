# -*- coding: utf-8 -*-
"""gateway-ui worker notice 跟踪器（事件反应，纯逻辑无 Qt/无 IO）。

state.json 的 notice 字段（宿主 statemachine 派生，{seq,kind,ts,platform}）
→ NoticeTracker.feed(state_dict) 判"新 notice"：
- seq 去重：同 seq 只反应一次（state 每拍重写，notice 常驻不变）；
- 启动首轮不回放：第一次 feed 只记基线 seq，陈旧 notice 不触发反应
  （worker 重启/重连不补闪上一轮的旧事件）；
- 过期忽略：notice.ts 距今 > max_age_s 视为陈旧（宿主时钟漂移/旧文件兜底）；
- seq 回退（宿主重启 seq 归零）：接受新基线但不回放，之后的增量照常。

reaction 映射 = PetWindow.play_reaction 的 kind 约定（另一并行任务实现该方法）：
done→"done" / failed→"failed" / await_input→"await_input" / stopped→"stopped"。
本模块不碰 pet_window.py / main.py——最终 tick 接线由收口接线。
"""
from __future__ import annotations

import time

# notice kind → play_reaction kind（白名单；未知 kind 不反应，绝不抛）
REACTION_KINDS = {"done": "done", "failed": "failed",
                  "await_input": "await_input", "stopped": "stopped"}
DEFAULT_MAX_AGE_S = 120.0   # notice 新鲜度窗：超过即陈旧（worker 停摆后恢复不补闪）


class NoticeTracker:
    """feed(state_dict) → 新 notice dict（仅 seq/kind/ts/platform）或 None。
    now_fn 可注入（单测用假时钟）；缺省 time.time。"""

    def __init__(self, max_age_s: float = DEFAULT_MAX_AGE_S, now_fn=None):
        self.max_age_s = float(max_age_s)
        self._now = now_fn or time.time
        self._last_seq = None    # None=尚未见过任何 state（首轮基线未建立）
        self.last_notice = None  # 最近一次判定为"新"的 notice（留证/调试）

    def feed(self, state) -> dict | None:
        if not isinstance(state, dict):
            return None
        notice = state.get("notice")
        if not isinstance(notice, dict):
            return None          # 无 notice（null/缺字段）：不动基线
        try:
            seq = int(notice.get("seq"))
        except (TypeError, ValueError):
            return None
        if seq <= 0:
            return None
        if self._last_seq is None:
            # 启动首轮：只记基线，绝不回放陈旧 notice
            self._last_seq = seq
            return None
        if seq == self._last_seq:
            return None          # 同 seq 去重（state 每拍重写，notice 常驻）
        if seq < self._last_seq:
            # seq 回退 = 宿主重启（notice_seq 归零重数）：重设基线，不回放旧 notice，
            # 之后的增量照常反应（否则要等 seq 爬回旧基线才恢复，会漏掉重启后所有事件）。
            self._last_seq = seq
            return None
        self._last_seq = seq
        kind = REACTION_KINDS.get(str(notice.get("kind") or ""))
        if kind is None:
            return None          # 未知 kind：推进基线但不反应
        try:
            ts = float(notice.get("ts"))
        except (TypeError, ValueError):
            return None
        if self._now() - ts > self.max_age_s:
            return None          # 过期忽略
        out = {"seq": seq, "kind": kind, "ts": ts,
               "platform": str(notice.get("platform") or "")}
        self.last_notice = out
        return out

    def reaction_kind(self, notice: dict) -> str | None:
        """feed 返回值 → play_reaction kind（便捷纯函数；非法返回 None）。"""
        if not isinstance(notice, dict):
            return None
        return REACTION_KINDS.get(str(notice.get("kind") or ""))