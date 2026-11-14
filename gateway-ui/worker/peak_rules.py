# -*- coding: utf-8 -*-
"""peak_rules.py — 峰谷规则纯函数（CONTRACT §10.3，余额气泡 BalanceBubble）。

零 Qt、零文件 IO、纯 stdlib。单一事实源：worker 跨边界推进用它，
宿主经 importlib 复用同一文件（禁止两处各写一份规则逻辑）。

接口契约（不许偏离）：
  is_peak_at(ts, rule=None, windows=None, holidays=None) -> bool
  next_boundary(ts, rule, windows, holidays) -> float   # 严格 > ts
  fmt_countdown(seconds) -> str                          # HH:MM:SS（>24h 小时位累加，无天数前缀）

规则：
- 内置 ds_cn：UTC+8 工作日(ISO 1-5) 9-12 与 14-18 峰，其余谷；周末全谷；
  holidays（YYYY-MM-DD 列表）全谷。边界候选 = 每日 {0,9,12,14,18} 时北京时间。
- windows 自定义：[{"days":[1..7],"ranges":[[h1,h2],...],"tz_offset_h":8}]，
  与 rule 同时存在时 windows 优先；holidays 对 windows 同样全谷。
- 都无（或未知 rule）：is_peak_at False、next_boundary 返回 ts+86400（兜底不炸）。
- next_boundary 返回下一次**峰/谷状态切换**的边界（跳过状态不变的候选点），
  扫描未来 ≤13 天；找不到切换点时回落首个候选点（必存在）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DAY_S = 86400.0
SCAN_DAYS = 13
DS_CN_OFFSET_H = 8
DS_CN_HOURS = (0, 9, 12, 14, 18)
_TZ8 = timezone(timedelta(hours=DS_CN_OFFSET_H))


def _tz(offset_h):
    return timezone(timedelta(hours=float(offset_h)))


def _norm_holidays(holidays):
    if not holidays:
        return frozenset()
    try:
        return frozenset(str(h) for h in holidays)
    except TypeError:
        return frozenset()


def _hour_frac(dt):
    """当日小时分数（9:30 -> 9.5），边界判定用半开区间 [h1, h2)。"""
    return (dt.hour + dt.minute / 60.0 + dt.second / 3600.0
            + dt.microsecond / 3.6e9)


def _windows_peak(ts, windows, hol):
    for w in windows:
        try:
            days = w.get("days") or []
            ranges = w.get("ranges") or []
            dt = datetime.fromtimestamp(ts, _tz(w.get("tz_offset_h", 8)))
            if dt.date().isoformat() in hol:
                continue
            if dt.isoweekday() not in days:
                continue
            hf = _hour_frac(dt)
            for r in ranges:
                if len(r) >= 2 and float(r[0]) <= hf < float(r[1]):
                    return True
        except Exception:
            continue
    return False


def is_peak_at(ts, rule=None, windows=None, holidays=None) -> bool:
    """ts（epoch 秒）时刻是否处于峰段。windows 优先于 rule；都无 → False。"""
    ts = float(ts)
    hol = _norm_holidays(holidays)
    if windows:
        return _windows_peak(ts, windows, hol)
    if rule == "ds_cn":
        dt = datetime.fromtimestamp(ts, _TZ8)
        if dt.date().isoformat() in hol:
            return False
        if dt.isoweekday() > 5:
            return False
        hf = _hour_frac(dt)
        return (9.0 <= hf < 12.0) or (14.0 <= hf < 18.0)
    return False


def _candidate_times(ts, rule, windows):
    """(ts, ts+13d] 内全部边界候选 epoch（去重升序）。
    ds_cn：每日 {0,9,12,14,18} 时北京时间；windows：每日 0 时 + 各 range 端点
    （按各 entry 自己的 tz_offset_h）。"""
    out = set()
    limit = ts + SCAN_DAYS * DAY_S
    if windows:
        for w in windows:
            try:
                tz = _tz(w.get("tz_offset_h", 8))
                hours = {0.0}
                for r in (w.get("ranges") or []):
                    if len(r) >= 2:
                        hours.add(float(r[0]))
                        hours.add(float(r[1]))
                day0 = datetime.fromtimestamp(ts, tz).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                for d in range(SCAN_DAYS + 1):
                    day = day0 + timedelta(days=d)
                    for h in hours:
                        c = (day + timedelta(hours=h)).timestamp()
                        if ts < c <= limit:
                            out.add(c)
            except Exception:
                continue
    elif rule == "ds_cn":
        day0 = datetime.fromtimestamp(ts, _TZ8).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for d in range(SCAN_DAYS + 1):
            day = day0 + timedelta(days=d)
            for h in DS_CN_HOURS:
                c = (day + timedelta(hours=h)).timestamp()
                if ts < c <= limit:
                    out.add(c)
    return sorted(out)


def next_boundary(ts, rule, windows, holidays) -> float:
    """下一次峰/谷切换的 epoch 秒，严格 > ts。
    扫描未来 ≤13 天候选点，取首个状态翻转点；无切换点回落首个候选；
    无规则兜底 ts+86400（不炸）。"""
    ts = float(ts)
    if not windows and rule != "ds_cn":
        return ts + DAY_S
    cands = _candidate_times(ts, rule, windows)
    if not cands:
        return ts + DAY_S
    for c in cands:
        if is_peak_at(c, rule, windows, holidays) != \
                is_peak_at(c - 1.0, rule, windows, holidays):
            return c
    return cands[0]


def fmt_countdown(seconds) -> str:
    """HH:MM:SS；>24h 小时位累加（25h → 25:00:00，作者裁定不要 Nd 前缀）；
    负数钳 0。"""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        s = 0
    if s < 0:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"