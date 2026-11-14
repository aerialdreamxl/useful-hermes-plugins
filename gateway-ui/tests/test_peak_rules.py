# -*- coding: utf-8 -*-
"""test_peak_rules.py — 余额气泡 peak_rules 纯函数断言（CONTRACT §10.3）。

纯 stdlib 无 Qt，side-env 或宿主 python 均可跑（-X utf8=1）。
判据：ds_cn 工作日峰/谷各时点、周末、节假日、边界精确秒、next_boundary
严格大于/跨日/跨周末、windows 自定义优先、fmt_countdown 纯 HH:MM:SS（小时位累加）、未知兜底。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import peak_rules as pr

TZ8 = timezone(timedelta(hours=8))
R = []


def ck(name, cond):
    R.append((name, bool(cond)))


def ts(y, mo, d, h=0, mi=0, s=0):
    """北京时间 epoch（测试全部用显式日历时刻，不依赖机器时区）。"""
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ8).timestamp()


# ---- ds_cn：工作日各时点（2026-11-16 周一）----
ck("ds_cn weekday 00:00 valley", pr.is_peak_at(ts(2026, 11, 16, 0), "ds_cn") is False)
ck("ds_cn weekday 08:59:59 valley", pr.is_peak_at(ts(2026, 11, 16, 8, 59, 59), "ds_cn") is False)
ck("ds_cn weekday 09:00:00 peak", pr.is_peak_at(ts(2026, 11, 16, 9, 0, 0), "ds_cn") is True)
ck("ds_cn weekday 10:30 peak", pr.is_peak_at(ts(2026, 11, 16, 10, 30), "ds_cn") is True)
ck("ds_cn weekday 11:59:59 peak", pr.is_peak_at(ts(2026, 11, 16, 11, 59, 59), "ds_cn") is True)
ck("ds_cn weekday 12:00:00 valley", pr.is_peak_at(ts(2026, 11, 16, 12, 0, 0), "ds_cn") is False)
ck("ds_cn weekday 13:00 valley", pr.is_peak_at(ts(2026, 11, 16, 13), "ds_cn") is False)
ck("ds_cn weekday 13:59:59 valley", pr.is_peak_at(ts(2026, 11, 16, 13, 59, 59), "ds_cn") is False)
ck("ds_cn weekday 14:00:00 peak", pr.is_peak_at(ts(2026, 11, 16, 14, 0, 0), "ds_cn") is True)
ck("ds_cn weekday 16:00 peak", pr.is_peak_at(ts(2026, 11, 16, 16), "ds_cn") is True)
ck("ds_cn weekday 17:59:59 peak", pr.is_peak_at(ts(2026, 11, 16, 17, 59, 59), "ds_cn") is True)
ck("ds_cn weekday 18:00:00 valley", pr.is_peak_at(ts(2026, 11, 16, 18, 0, 0), "ds_cn") is False)
ck("ds_cn weekday 23:59:59 valley", pr.is_peak_at(ts(2026, 11, 16, 23, 59, 59), "ds_cn") is False)

# ---- 周末全谷（2026-11-14 周六 / 11-15 周日）----
ck("ds_cn saturday 10:00 valley", pr.is_peak_at(ts(2026, 11, 14, 10), "ds_cn") is False)
ck("ds_cn saturday 15:00 valley", pr.is_peak_at(ts(2026, 11, 14, 15), "ds_cn") is False)
ck("ds_cn sunday 10:00 valley", pr.is_peak_at(ts(2026, 11, 15, 10), "ds_cn") is False)
ck("ds_cn sunday 16:00 valley", pr.is_peak_at(ts(2026, 11, 15, 16), "ds_cn") is False)

# ---- 节假日全谷 ----
HOL = ["2026-10-01"]
ck("ds_cn holiday 10:00 valley", pr.is_peak_at(ts(2026, 10, 1, 10), "ds_cn", holidays=HOL) is False)
ck("ds_cn holiday 15:00 valley", pr.is_peak_at(ts(2026, 10, 1, 15), "ds_cn", holidays=HOL) is False)
ck("ds_cn non-holiday same hour peak", pr.is_peak_at(ts(2026, 10, 2, 10), "ds_cn", holidays=HOL) is True)
ck("ds_cn holiday list not mutated", HOL == ["2026-10-01"])

# ---- next_boundary：严格大于 + 状态翻转 + 跳过不变候选 ----
t = ts(2026, 11, 16, 8, 30)
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb 08:30 -> 09:00", b == ts(2026, 11, 16, 9))
ck("nb strictly greater", b > t)
t = ts(2026, 11, 16, 10)
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb 10:00 -> 12:00", b == ts(2026, 11, 16, 12))
t = ts(2026, 11, 16, 12, 30)
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb 12:30 -> 14:00 (skip valley noon)", b == ts(2026, 11, 16, 14))
t = ts(2026, 11, 16, 16)
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb 16:00 -> 18:00", b == ts(2026, 11, 16, 18))

# ---- 跨日：工作日 18 点后 → 次日 9 点（0 点候选状态不变被跳过）----
t = ts(2026, 11, 16, 19)
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb Mon 19:00 -> Tue 09:00 (cross day)", b == ts(2026, 11, 17, 9))
ck("nb cross-day strictly greater", b > t)

# ---- 跨周末：周五晚 → 周一 9 点 ----
t = ts(2026, 11, 13, 19)  # 周五
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb Fri 19:00 -> Mon 09:00 (cross weekend)", b == ts(2026, 11, 16, 9))
t = ts(2026, 11, 14, 10)  # 周六
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb Sat 10:00 -> Mon 09:00", b == ts(2026, 11, 16, 9))

# ---- 边界精确秒 ----
t = ts(2026, 11, 16, 9) - 1
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb 08:59:59 -> 09:00:00 exact", b == ts(2026, 11, 16, 9))
t = ts(2026, 11, 16, 9)
b = pr.next_boundary(t, "ds_cn", None, None)
ck("nb exactly 09:00:00 -> 12:00 (strict >)", b == ts(2026, 11, 16, 12))

# ---- 节假日影响边界推进 ----
t = ts(2026, 9, 30, 19)  # 周三晚，次日国庆全谷
b = pr.next_boundary(t, "ds_cn", None, HOL)
ck("nb before holiday skips holiday -> Oct 2 09:00", b == ts(2026, 10, 2, 9))

# ---- windows 自定义（含非 +8 时区 / 与 rule 并存优先）----
W_UTC = [{"days": [1, 2, 3, 4, 5], "ranges": [[9, 12], [14, 18]], "tz_offset_h": 0}]
t_utc = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
ck("windows utc 10:00 peak", pr.is_peak_at(t_utc, windows=W_UTC) is True)
t_utc2 = datetime(2026, 11, 16, 13, tzinfo=timezone.utc).timestamp()
ck("windows utc 13:00 valley", pr.is_peak_at(t_utc2, windows=W_UTC) is False)
ck("windows beats rule (utc 10 = cn 18 valley)",
   pr.is_peak_at(t_utc, rule="ds_cn", windows=W_UTC) is True)
W_NIGHT = [{"days": [1, 2, 3, 4, 5, 6, 7], "ranges": [[0, 6]], "tz_offset_h": 8}]
ck("windows all-days 03:00 peak", pr.is_peak_at(ts(2026, 11, 14, 3), windows=W_NIGHT) is True)
ck("windows all-days 07:00 valley", pr.is_peak_at(ts(2026, 11, 14, 7), windows=W_NIGHT) is False)
t = ts(2026, 11, 16, 3)
b = pr.next_boundary(t, None, W_NIGHT, None)
ck("nb windows 03:00 -> 06:00", b == ts(2026, 11, 16, 6))
t = ts(2026, 11, 16, 7)
b = pr.next_boundary(t, None, W_NIGHT, None)
ck("nb windows 07:00 -> next day 00:00", b == ts(2026, 11, 17, 0))
ck("windows holiday valley", pr.is_peak_at(ts(2026, 10, 1, 3), windows=W_NIGHT, holidays=HOL) is False)
W_MULTI = [{"days": [6, 7], "ranges": [[10, 12]], "tz_offset_h": 8},
           {"days": [1, 2, 3, 4, 5], "ranges": [[9, 12], [14, 18]], "tz_offset_h": 8}]
ck("multi windows weekend entry peak", pr.is_peak_at(ts(2026, 11, 14, 11), windows=W_MULTI) is True)
ck("multi windows weekday entry peak", pr.is_peak_at(ts(2026, 11, 16, 15), windows=W_MULTI) is True)
ck("multi windows weekend 15 valley", pr.is_peak_at(ts(2026, 11, 14, 15), windows=W_MULTI) is False)

# ---- 未知规则 / 都无：兜底不炸 ----
t = ts(2026, 11, 16, 10)
ck("unknown rule is_peak False", pr.is_peak_at(t, rule="bogus") is False)
ck("no rule is_peak False", pr.is_peak_at(t) is False)
ck("unknown rule nb = ts+86400", pr.next_boundary(t, "bogus", None, None) == t + 86400)
ck("no rule nb = ts+86400", pr.next_boundary(t, None, None, None) == t + 86400)
ck("empty windows falls to rule", pr.is_peak_at(t, rule="ds_cn", windows=[]) is True)
ck("malformed windows no crash", pr.is_peak_at(t, windows=[{"days": "x"}]) is False)

# ---- fmt_countdown ----
ck("fmt 0", pr.fmt_countdown(0) == "00:00:00")
ck("fmt 59s", pr.fmt_countdown(59) == "00:00:59")
ck("fmt 61s", pr.fmt_countdown(61) == "00:01:01")
ck("fmt 3661s", pr.fmt_countdown(3661) == "01:01:01")
ck("fmt 23:59:59", pr.fmt_countdown(86399) == "23:59:59")
ck("fmt exactly 24h no prefix", pr.fmt_countdown(86400) == "24:00:00")
ck("fmt 24h+1 hours accumulate", pr.fmt_countdown(86401) == "24:00:01")
ck("fmt 25h no Nd prefix", pr.fmt_countdown(90000) == "25:00:00")
ck("fmt 3 days hours accumulate", pr.fmt_countdown(3 * 86400 + 7265) == "74:01:05")
ck("fmt negative clamps 0", pr.fmt_countdown(-5) == "00:00:00")
ck("fmt float truncates", pr.fmt_countdown(61.9) == "00:01:01")
ck("fmt garbage -> 00:00:00", pr.fmt_countdown("xyz") == "00:00:00")

# ---- 扫描上界：13 天内必找到（长节假日表也不炸）----
long_hol = [(datetime(2026, 11, 16, tzinfo=TZ8) + timedelta(days=i)).date().isoformat()
            for i in range(10)]
t = ts(2026, 11, 16, 10)
b = pr.next_boundary(t, "ds_cn", None, long_hol)
ck("nb 10-day holiday still within 13d scan", t < b <= t + 13 * 86400)
ck("nb after long holiday lands on workday peak",
   b == ts(2026, 11, 26, 9))

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R) - len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)