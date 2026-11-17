# -*- coding: utf-8 -*-
"""test_approval_host.py — 审批弹窗宿主桥接（PROMPT §12.4 断言 1-4，宿主 python）。

用真 ApprovalRequest（hermes_cli.approval_transport）验 digest/respond 绑定；
present 跑真线程，短 timeout（6s→自终止线 1s）+ 快 poll。全部 scratch 总线，
绝不碰真实总线/config。覆盖：pending 登记/注销、req 快照白名单、resp 消费、
invalid/no_response fail-closed 矩阵、worker 死、cancel、stop、withdrawn。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import scratch, repo  # noqa: E402
from _loader import load_plugin  # noqa: E402

sys.path.insert(0, str(repo()))   # 官方源码只读取证（ApprovalRequest 真协议）
# Gateway 真实依赖根（uv archive 含 ruamel 等）：自动探测，环境重建后仍有效。
_uv_root = Path(os.environ["HERMES_HOME"]) / "cache" / "uv" / "archive-v0"
for _cand in sorted(_uv_root.iterdir()):
    if (_cand / "ruamel" / "yaml" / "__init__.py").is_file():
        sys.path.insert(0, str(_cand))
        break
gui = load_plugin()
ah = gui.approval_host
from hermes_cli.approval_transport import ApprovalRequest  # noqa: E402

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

BUS = scratch("gui28-approval-host")
BUS.mkdir(parents=True, exist_ok=True)
ah._POLL_S = 0.05          # 快 poll，测试提速


def clean():
    for f in BUS.glob("approval_*"):
        f.unlink(missing_ok=True)
    (BUS / "tray_lease.json").unlink(missing_ok=True)
    ah.init(BUS, stop_event=threading.Event(), logger=None)


def make_req(timeout=6.0, surface="transport:gateway-ui", command="rm -rf /x"):
    return ApprovalRequest.create(
        command=command, description="危险命令样例", pattern_key="rm",
        pattern_keys=("rm",), session_key="agent:ns:desktop:dm:12345",
        surface=surface, allow_session=True, allow_permanent=True,
        timeout_seconds=timeout)


def fresh_lease():
    p = BUS / "tray_lease.json"
    p.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    now = time.time()
    os.utime(p, (now, now))


def write_resp(rid, digest, choice):
    p = BUS / "approval_resp.json"
    entries = []
    if p.is_file():
        try:
            entries = json.loads(p.read_text(encoding="utf-8")).get("entries") or []
        except Exception:
            entries = []
    entries.append({"request_id": rid, "digest": digest, "choice": choice,
                    "clicked_ts": time.time()})
    p.write_text(json.dumps({"entries": entries}), encoding="utf-8")


def run_present(req, resp=None, resp_delay=0.0, worker=True, stop=False, cancel=False):
    """跑 present 于真线程，返回 (decision, elapsed)。resp=(choice) 或延迟写。"""
    clean()
    if worker:
        fresh_lease()
    if stop:
        ah._STOP.set()
    if resp is not None and resp_delay <= 0:
        write_resp(req.request_id, req.digest, resp)
    box = {}
    def target():
        t0 = time.time()
        box["d"] = ah.present(req)
        box["dt"] = time.time() - t0
    th = threading.Thread(target=target, daemon=True)
    th.start()
    if resp is not None and resp_delay > 0:
        time.sleep(resp_delay)
        write_resp(req.request_id, req.digest, resp)
    if cancel:
        time.sleep(0.1)
        ah.cancel_session("agent:ns:desktop:dm:12345")
    th.join(timeout=15)
    return box.get("d"), box.get("dt", 99)


# ==================================================== 断言 2：桥接全链 ====
req = make_req()
d, dt = run_present(req, resp="once")
ck("bridge: decision returned", d is not None)
ck("bridge: choice == once", getattr(d, "choice", None) == "once")
ck("bridge: request_id bound", getattr(d, "request_id", None) == req.request_id)
ck("bridge: digest bound (host-owned, passthrough)", getattr(d, "request_digest", None) == req.digest)
ck("bridge: fast return on pre-written resp (<2s)", dt < 2)
ck("bridge: pending drained to 0", ah.pending_count() == 0)
reqf = json.loads((BUS / "approval_req.json").read_text(encoding="utf-8"))
ck("bridge: req snapshot emptied after consume", reqf.get("requests") == [])
wd = (BUS / "approval_withdrawn.jsonl").read_text(encoding="utf-8").strip().splitlines()
ck("bridge: withdrawn appended", len(wd) == 1)
ck("bridge: withdrawn reason completed", json.loads(wd[0]).get("reason") == "completed")
ck("bridge: withdrawn request_id matches", json.loads(wd[0]).get("request_id") == req.request_id)

# 延迟 resp（present 阻塞中 → 轮询取回）
req = make_req()
d, dt = run_present(req, resp="deny", resp_delay=0.3)
ck("bridge: delayed resp consumed", getattr(d, "choice", None) == "deny")
ck("bridge: delayed return bounded (<3s)", dt < 3)

# session/always 合法 choice
for choice in ("session", "always"):
    req = make_req()
    d, _ = run_present(req, resp=choice)
    ck(f"bridge: choice {choice} accepted", getattr(d, "choice", None) == choice)

# ============================================ 断言 2：req 快照白名单 ====
req = make_req(command="rm -rf /secret/path")
clean()
fresh_lease()
box = {}
th = threading.Thread(target=lambda: box.update(d=ah.present(req)), daemon=True)
th.start()
time.sleep(0.2)
snap = json.loads((BUS / "approval_req.json").read_text(encoding="utf-8"))
ck("snapshot: one request pending", len(snap.get("requests") or []) == 1)
r0 = snap["requests"][0]
ck("snapshot: command present (redacted host copy)", r0.get("command") == "rm -rf /secret/path")
ck("snapshot: digest carried", r0.get("digest") == req.digest)
ck("snapshot: allowed_choices list", set(r0.get("allowed_choices") or []) == {"once", "session", "always", "deny"})
ck("snapshot: session_key clipped <=16", len(str(r0.get("session_key") or "")) <= 16)
ck("snapshot: surface carried", r0.get("surface") == "transport:gateway-ui")
ck("snapshot: expires_ts > now", float(r0.get("expires_ts") or 0) > time.time())
ck("snapshot: no raw secret field", "session_key" in r0 and "prompt" not in r0)
ck("snapshot: pending_count == 1", ah.pending_count() == 1)
write_resp(req.request_id, req.digest, "once")
th.join(timeout=10)

# ==================================== 断言 3：fail-closed 矩阵 ====
# invalid choice（worker 发非法 choice）→ no_response
req = make_req()
d, _ = run_present(req, resp="bogus")
ck("failclosed: invalid choice -> no_response", getattr(d, "choice", None) == "no_response")

# worker 死（无 tray_lease）→ no_response 快速
req = make_req()
d, dt = run_present(req, worker=False)
ck("failclosed: worker dead -> no_response", getattr(d, "choice", None) == "no_response")
ck("failclosed: worker dead fast (<2s)", dt < 2)

# 自终止到点（timeout=6→1s，无 resp，worker 活）→ no_response
req = make_req(timeout=6.0)
d, dt = run_present(req, worker=True)
ck("failclosed: self-terminate -> no_response", getattr(d, "choice", None) == "no_response")
ck("failclosed: self-terminate ~1s (timeout-5)", 0.8 < dt < 3)

# cancel_session → no_response（reason withdrawn）
req = make_req(timeout=30)
clean()
fresh_lease()
ah.note_request_session(req.request_id, "agent:ns:desktop:dm:12345")
box = {}
th = threading.Thread(target=lambda: box.update(d=ah.present(req)), daemon=True)
th.start()
time.sleep(0.15)
ah.cancel_session("agent:ns:desktop:dm:12345")
th.join(timeout=10)
ck("failclosed: cancel -> no_response", getattr(box.get("d"), "choice", None) == "no_response")
wd = (BUS / "approval_withdrawn.jsonl").read_text(encoding="utf-8").strip().splitlines()
ck("failclosed: cancel reason withdrawn", json.loads(wd[-1]).get("reason") == "withdrawn")

# stop event → no_response
req = make_req(timeout=30)
d, dt = run_present(req, worker=True, stop=True)
ck("failclosed: stop -> no_response", getattr(d, "choice", None) == "no_response")
ck("failclosed: stop fast (<2s)", dt < 2)

# no_response 不在 allowed_choices（宿主会判 invalid）
req = make_req()
ck("failclosed: no_response not in allowed_choices", "no_response" not in req.allowed_choices)

# ============================================ 断言 1：注册惰性 ====
# init 清空残留；未 present 时 pending 恒 0、req 文件不主动写
clean()
ck("inert: pending_count 0 without present", ah.pending_count() == 0)
ck("inert: no req file before present", not (BUS / "approval_req.json").exists())

# ==================================== 并发多 pending（≤8 槽）====
clean()
fresh_lease()
reqs = [make_req(timeout=30) for _ in range(3)]
threads = []
for rq in reqs:
    t = threading.Thread(target=lambda rq=rq: ah.present(rq), daemon=True)
    t.start()
    threads.append(t)
time.sleep(0.3)
ck("concurrent: pending_count == 3", ah.pending_count() == 3)
snap = json.loads((BUS / "approval_req.json").read_text(encoding="utf-8"))
ck("concurrent: req snapshot has 3", len(snap.get("requests") or []) == 3)
for rq in reqs:
    write_resp(rq.request_id, rq.digest, "once")
for t in threads:
    t.join(timeout=10)
ck("concurrent: all drained", ah.pending_count() == 0)

# ==================================== hint 上限 / cancel 无匹配 ====
clean()
for i in range(ah._HINT_CAP + 20):
    ah.note_request_session(f"rid{i}", "sk")
ck("hint: capped at _HINT_CAP", len(ah._SESSION_HINTS) <= ah._HINT_CAP)
ah.cancel_session("nonexistent-session")   # 无匹配不抛
ck("cancel: no-match no-op safe", ah.pending_count() == 0)

# ==================================== R29：平台分流（takeover 默认 false）====
# 分类器纯函数：agent:<ns>:<platform>:<chat_type>:… 第三段；本地集 local/cli +
# NOTICE_PLATFORMS（desktop/tui/cli）双保险；空/非布局 → 本地（宁弹窗不静默拒）。
ck("route: local key platform parsed", ah.session_platform_of("agent:main:local:dm:abc") == "local")
ck("route: feishu parsed", ah.session_platform_of("agent:main:feishu:group:gid:uid") == "feishu")
ck("route: qqbot parsed", ah.session_platform_of("agent:main:qqbot:group:gid:uid") == "qqbot")
ck("route: non-layout -> empty", ah.session_platform_of("some-random-key") == "")
for sk in ("agent:main:local:dm:abc", "agent:main:cli:dm:x", "agent:main:desktop:dm:x",
           "agent:main:tui:dm:x", "", "odd-key"):
    ck(f"route: local {sk or '<empty>'}", ah.is_local_session(sk))
for sk in ("agent:main:feishu:group:g:u", "agent:main:qqbot:group:g:u",
           "agent:main:weixin:dm:c", "agent:main:telegram:dm:c",
           "agent:main:discord:group:g", "agent:main:futurebot:dm:c"):
    ck(f"route: remote {sk.split(':')[2]}", not ah.is_local_session(sk))

# 远程会话 + takeover=false（默认）→ 立即 no_response，不登记 pending/不写 req/
# 不写 withdrawn（原生面自己管生命周期），hint 释放。
clean()
fresh_lease()
req = make_req(timeout=30)
ah.note_request_session(req.request_id, "agent:main:feishu:group:g:u")
d = ah.present(req)          # 同步直返（无线程等待——分流的即时性本身就是断言）
ck("route: remote deferred -> no_response", getattr(d, "choice", None) == "no_response")
ck("route: deferred pending stays 0", ah.pending_count() == 0)
ck("route: deferred no req file", not (BUS / "approval_req.json").exists())
ck("route: deferred no withdrawn", not (BUS / "approval_withdrawn.jsonl").exists())
ck("route: deferred hint released", req.request_id not in ah._SESSION_HINTS)

# 本地会话同配置 → 照常弹窗链路（req 写出，resp 消费；手工线程保 hint 不被 clean 清）
req = make_req(timeout=30)
clean()
fresh_lease()
ah.note_request_session(req.request_id, "agent:main:local:dm:abc")
box = {}
th = threading.Thread(target=lambda: box.update(d=ah.present(req)), daemon=True)
th.start()
time.sleep(0.2)
ck("route: local hint -> pending registered", ah.pending_count() == 1)
write_resp(req.request_id, req.digest, "once")
th.join(timeout=10)
ck("route: local still popup path", getattr(box.get("d"), "choice", None) == "once")

# takeover=true → 远程也接管（弹窗链路；同样手工线程保 hint）
# 注意顺序：clean() 内部 init 会重置 _PRESENT_CFG_FN——必须先 clean 再设探针。
req = make_req(timeout=30)
clean()
fresh_lease()
ah._PRESENT_CFG_FN = lambda: {"platform_takeover": True}
ah.note_request_session(req.request_id, "agent:main:qqbot:group:g:u")
box = {}
th = threading.Thread(target=lambda: box.update(d=ah.present(req)), daemon=True)
th.start()
time.sleep(0.2)
ck("route: takeover=true remote pending registered", ah.pending_count() == 1)
write_resp(req.request_id, req.digest, "once")
th.join(timeout=10)
ck("route: takeover=true remote accepted", getattr(box.get("d"), "choice", None) == "once")
ck("route: takeover=true remote drained", ah.pending_count() == 0)
ah._PRESENT_CFG_FN = None

# cfg_fn 抛异常 → 按默认（不接管）安全侧
ah._PRESENT_CFG_FN = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
req = make_req(timeout=30)
ah.note_request_session(req.request_id, "agent:main:weixin:dm:c")
d = ah.present(req)
ck("route: cfg_fn error -> safe defer", getattr(d, "choice", None) == "no_response")
ah._PRESENT_CFG_FN = None

# 无 hint（session_key 空）→ 保守按本地（本机审批绝不静默拒）
clean()
fresh_lease()
req = make_req(timeout=30)
d, dt = run_present(req, resp="once")
ck("route: no hint -> local popup path", getattr(d, "choice", None) == "once")

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)
