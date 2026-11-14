"""gateway-ui 状态机 —— 十四/十五/写者租约作者裁定判据，纯逻辑无 IO。

忙线判据（忙线判据）：活跃以 session_id 为键幂等记账；0=idle，1=该会话阶段
（thinking/acting），≥2=恒 busy。tool-loop 多 API 请求不虚增；未知键多活跃不瞎销账。
权威源（十五/写者租约）：runner._running_agent_items() 是全 turn 总登记（含桌面端），
阶段直读 agent._last_activity_desc；连续 3 拍不可达退事件兜底。
降级（降级判定）：生效 base_url ≠ 主配置端点即降级，provider 名不参与；轮内换道要算。
error/happy/listening 只在零活跃窗口作瞬时展示层。
"""
from __future__ import annotations

ACTIVE_TTL = 1800.0   # 兜底 GC：进程外死法漏销账
PHASE_TTL = 60.0

# ---- 事件反应 notice（完成/失败/等人类输入）----
# 平台白名单实锤（源码取证，只许本机交互面产生 notice）：
# - desktop/tui：tui_gateway/server.py:1528-1532 _resolve_session_platform()
#   （HERMES_DESKTOP=1 且非 HERMES_DESKTOP_TERMINAL → "desktop"，否则 "tui"），
#   server.py:2637 AIAgent(platform=platform) 直传。
# - cli：hermes_cli/cli_agent_setup_mixin.py:686 platform="cli"；
#   gateway LOCAL 平台也映射 "cli"（gateway/run_turn_runner.py:1167 platform_key）。
# 其余（telegram/discord/qqbot/api_server/cron/subagent…）一律不产生 notice。
NOTICE_PLATFORMS = frozenset({"desktop", "tui", "cli"})
NOTICE_TURN_TTL = 600.0    # turn_id 去重表寿命（同 turn 重复 on_session_end 幂等）
NOTICE_KEY_TTL = 30.0      # await_input 去重窗（source+session_key，压 coalesced 连发）
NOTICE_SESSION_TTL = 600.0  # session→platform 归因表寿命


def notice_platform(fl: dict) -> str:
    """平台归一（小写去空白）；未知/空 → ""（白名单外）。"""
    return str(fl.get("platform") or "").strip().lower()


def platform_from_session_key(key: str) -> str:
    """gateway session_key（agent:{ns}:{platform}:{chat_type}:{chat_id}…，
    gateway/run.py:2977 _parse_session_key 同口径）取 platform 段。
    仅用于 await_input 的保守归因：解析不出 → ""（宁漏报不误报）。"""
    try:
        parts = str(key or "").split(":")
        if len(parts) >= 5 and parts[0] == "agent":
            return parts[2].strip().lower()
    except Exception:
        pass
    return ""


def derive_notice_kind(fl: dict) -> str:
    """turn_end 结局 → notice kind。interrupted 不算失败（用户 /stop 是主动行为，
    不该弹"失败"反应）：interrupted 优先于 failed（turn_finalizer.py:572 completed
    要求 not failed and not interrupted，两者可同真）；无结局标志（post_llm_call/
    agent_loop_stopped 的旧 turn_end）→ ""，不产生 notice。"""
    if fl.get("interrupted"):
        return "stopped"
    if fl.get("failed"):
        return "failed"
    if fl.get("completed"):
        return "done"
    return ""


def phase_from_activity(desc: str) -> str:
    """agent._last_activity_desc → 阶段（源码实锤写入点：tool_executor
    'executing tool: X'；chat_completion 'waiting for provider response'/
    'model reading the prompt'/'receiving stream response'）。"""
    d = (desc or "").lower()
    if "executing tool" in d or ("executing " in d and "tools" in d):
        return "acting"
    return "thinking"


def sess_key(fl: dict) -> str:
    return fl.get("session_key") or fl.get("task_key") or fl.get("turn_key") or ""


def url_label(u: str) -> str:
    try:
        return str(u).split("//", 1)[-1].split("/", 1)[0][:40] or str(u)[:40]
    except Exception:
        return str(u)[:40]


class StateMachine:
    def __init__(self):
        self.active: dict = {}          # session_key -> last_event_ts
        self.phase: dict = {}           # session_key -> (phase, ts)
        self.authority = None           # None=事件兜底；dict=权威 {key: phase}
        self.authority_streak = 0
        self.baseline_provider = None
        self.baseline_base_url = None
        self.error_ts: list = []
        self.last_degrade = None
        self.happy_until = 0.0
        self.error_active_until = 0.0
        self.state_until = 0.0          # listening 超时
        self.pet_state = "idle"
        self.update_available = False
        # ---- 事件反应 notice ----
        self.notice = None            # 最后一个 notice {seq,kind,ts,platform}，无则 None
        self.notice_seq = 0           # notice 单调序号（独立于事件 _SEQ）
        self._notice_turns: dict = {}   # turn_key -> ts（同 turn 幂等去重）
        self._notice_waits: dict = {}   # (source, session_key) -> ts（await_input 去重窗）
        self._sess_plat: dict = {}      # session_key -> (platform, ts)（await_input 归因）

    # ---------------------------------------------------------- 事件记账 ----
    def _note_degrade(self, ts: float, fl: dict) -> None:
        bb = self.baseline_base_url
        eff = fl.get("base_url")
        if eff and bb and eff != bb:
            if self.last_degrade is None or ts - self.last_degrade["ts"] > 2:
                self.last_degrade = {"from": bb, "to": eff,
                                     "provider": fl.get("provider") or "", "ts": ts}

    # ---------------------------------------------------------- notice 派生 ----
    def _emit_notice(self, kind: str, ts: float, platform: str) -> None:
        """notice 只带 seq/kind/ts/platform（字段白名单铁律：绝无正文/命令/密钥）。"""
        self.notice_seq += 1
        self.notice = {"seq": self.notice_seq, "kind": kind,
                       "ts": round(ts, 3), "platform": platform}

    def _note_session_platform(self, fl: dict, ts: float) -> None:
        """turn 事件顺手记 session→platform（await_input 归因用；只记白名单值）。"""
        key = sess_key(fl)
        plat = notice_platform(fl)
        if key and plat in NOTICE_PLATFORMS:
            self._sess_plat[key] = (plat, ts)

    def _notice_turn_end(self, ts: float, fl: dict) -> None:
        """on_session_end（per-turn）→ done/failed/stopped。平台白名单门控；
        同 turn_id 幂等（重发/双 hook 不重复弹）；无 turn_key 不去重（宁可偶重不漏）。"""
        plat = notice_platform(fl)
        if plat not in NOTICE_PLATFORMS:
            return
        kind = derive_notice_kind(fl)
        if not kind:
            return
        tk = str(fl.get("turn_key") or "")
        if tk:
            if tk in self._notice_turns:
                return
            self._notice_turns[tk] = ts
        self._emit_notice(kind, ts, plat)

    def _notice_await(self, ts: float, fl: dict) -> None:
        """approval/clarify → await_input。平台归因三阶（宁漏报不误报）：
        1) 事件自带 platform（clarify 宿主侧从 pre_tool_call 归因补齐）；
        2) session→platform 表（turn_start 实锤）；
        3) gateway session_key 的 platform 段（agent:ns:platform:…）。
        surface="gateway" 不直接当平台用——它只说明走 gateway 审批通道，
        背后可能是 telegram/discord 等消息平台（一律不 notice）。"""
        plat = notice_platform(fl)
        sk = str(fl.get("session_key") or "")
        sid = str(fl.get("session_id") or "")
        if plat not in NOTICE_PLATFORMS:
            hit = self._sess_plat.get(sk) or self._sess_plat.get(sid)
            if hit is not None and ts - hit[1] <= NOTICE_SESSION_TTL:
                plat = hit[0]
        if plat not in NOTICE_PLATFORMS:
            plat = platform_from_session_key(sk)
        if plat not in NOTICE_PLATFORMS:
            return
        src = str(fl.get("source") or "")
        wk = (src, sk)
        last = self._notice_waits.get(wk)
        if last is not None and ts - last <= NOTICE_KEY_TTL:
            return  # coalesced 连发/同会话短窗重复等待：压成一条
        self._notice_waits[wk] = ts
        self._emit_notice("await_input", ts, plat)

    def apply(self, kind: str, ts: float, fl: dict) -> None:
        if kind == "inbound":
            self.state_until = ts + 3
        elif kind == "turn_start":
            key = sess_key(fl) or str(ts)
            self.active[key] = ts
            self.phase[key] = ("thinking", ts)
            self._note_degrade(ts, fl)
            self._note_session_platform(fl, ts)
        elif kind == "post_api":
            key = sess_key(fl)
            if key and key in self.active:
                self.active[key] = ts
            if key:
                self.phase[key] = ("thinking", ts)
            self._note_degrade(ts, fl)
        elif kind == "tool":
            key = sess_key(fl) or "?"
            self.phase[key] = ("acting", ts)
            if key in self.active:
                self.active[key] = ts
        elif kind == "turn_end":
            key = sess_key(fl)
            if self.active.pop(key, None) is not None:
                self.phase.pop(key, None)
            elif key:
                self.phase.pop(key, None)
                if len(self.active) == 1:
                    # 键漂移且仅一个活跃：销最老；多活跃绝不瞎猜（旧 FIFO 乱销=闪烁源）
                    oldest = min(self.active, key=self.active.get)
                    self.active.pop(oldest, None)
                    self.phase.pop(oldest, None)
            if fl.get("ok") is False:
                self.error_ts.append(ts)
            else:
                self.happy_until = ts + 2
            # 事件反应：on_session_end 结局 notice（旧 turn_end 无结局标志 → 不产生）
            self._notice_turn_end(ts, fl)
        elif kind == "await_input":
            self._notice_await(ts, fl)
        elif kind == "error":
            self.error_ts.append(ts)
            self.error_active_until = ts + 30

    # ---------------------------------------------------------- 权威源 ----
    def set_authority(self, items) -> None:
        """items = runner._running_agent_items() 的 (key, agent) 列表。"""
        auth = {}
        for k, agent in items:
            desc = str(getattr(agent, "_last_activity_desc", "") or "")
            auth[k] = phase_from_activity(desc)
        self.authority = auth
        self.authority_streak = 0

    def authority_unreachable(self) -> None:
        self.authority_streak += 1
        if self.authority_streak >= 3:
            self.authority = None

    # ---------------------------------------------------------- 派生 ----
    def active_count(self) -> int:
        if self.authority is not None:
            return len(self.authority)
        return len(self.active)

    def derive(self, now: float) -> str:
        auth = self.authority
        if auth is not None:
            n = len(auth)
            if n >= 2:
                return "busy"
            if n == 1:
                return next(iter(auth.values()))
        else:
            n = len(self.active)
            if n >= 2:
                return "busy"
            if n == 1:
                best, best_ts = "thinking", 0.0
                for _k, (ph, ts) in self.phase.items():
                    if ts > best_ts:
                        best, best_ts = ph, ts
                return best
        if now < self.error_active_until and self.error_ts:
            return "error"
        if now < self.happy_until:
            return "happy"
        if now < self.state_until:
            return "listening"
        return "idle"

    def tier_and_light(self, now: float, window: float) -> tuple:
        deg = self.last_degrade
        red = bool(deg and now - deg["ts"] <= window)
        recent_err = [t for t in self.error_ts if now - t <= window]
        err_active = now < self.error_active_until
        red = red or len(recent_err) >= 2 or err_active
        light = "red" if red else ("orange" if self.active_count() else "green")
        if red:
            tier = "需要留意"
        elif recent_err:
            tier = "轻微波动"
        elif self.update_available:
            tier = "更新可用！"
        else:
            tier = "暂无异常"
        return tier, light

    def gc(self, now: float) -> None:
        self.error_ts = [t for t in self.error_ts if now - t <= 3600]
        for k in [k for k, t in self.active.items() if now - t > ACTIVE_TTL]:
            self.active.pop(k, None)
        for k in [k for k, (_ph, t) in self.phase.items() if now - t > PHASE_TTL]:
            self.phase.pop(k, None)
        # 事件反应 notice 去重表 GC（防长活进程无限膨胀）
        for k in [k for k, t in self._notice_turns.items() if now - t > NOTICE_TURN_TTL]:
            self._notice_turns.pop(k, None)
        for k in [k for k, t in self._notice_waits.items() if now - t > NOTICE_KEY_TTL]:
            self._notice_waits.pop(k, None)
        for k in [k for k, (_p, t) in self._sess_plat.items() if now - t > NOTICE_SESSION_TTL]:
            self._sess_plat.pop(k, None)