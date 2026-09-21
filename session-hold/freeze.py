"""session-hold v2 — park middleware and the durable freeze/replay layer.

Two seamless mechanisms behind the same /hold + /unhold commands:

1. PARK (default; the model never learns a pause happened):
   `llm_execution` middleware fires with the FINAL provider request assembled,
   and its caller has no per-callback timeout (hermes_cli/plugins_dispatch.py::
   invoke_middleware runs callbacks inline on the turn thread). On an armed
   hold the middleware durably snapshots the request payload, then blocks the
   turn thread in small polls until /unhold, then hands the SAME request to
   next_call(): the agent loop simply sees one slow API call. No fake
   messages, no prompt notes, no model awareness. Park polls the agent's
   interrupt flag so /stop, /new and steering still reach the turn (an
   interrupt drains the hold to idle; the downstream interruptible call then
   raises through core's normal path). Park has a TTL: on expiry the request
   is passed through and the hold degrades to tool-block (v1) semantics.
   After a process restart the NEXT API call of the held session RE-PARKS
   (held + park-mode + no live parked thread), so the invisible pause keeps
   working post-restart too — /unhold from any window releases it.

2. FREEZE/REPLAY (/unhold after a restart, before the session sends anything
   new): the frozen payload is the complete model-visible context at the pause
   point. /unhold marks the entry replay="pending" and kicks a continuation
   turn; the `llm_request` middleware reconstructs this session's outgoing
   requests as a pure-APPEND continuation of the frozen payload:

     frame 1 := frozen rows (the model's exact pause point; the synthetic
                kickoff user row falls on the discarded side of the swap)
     frame N := frozen rows + live[l1:] — rows the loop appended after frame 1
                (assistant + real tool results + steers), sliced from the live
                payload. len(live) >= l1 gates continuity; a shrunk/rebuilt
                list (compaction, takeover) ends the episode fail-open.

   The episode self-closes when the model delivers a final answer without
   tool calls (post_api_request observer in __init__.py: hold released), when
   a real user message arrives mid-episode, or when the frame budget is
   spent (then the hold degrades to tool-block so pausing still works,
   visibly, per v1).

Why not diff live[:l1] against the snapshot: Hermes re-renders the system row
and prompt-cache markers per attempt/turn, so byte comparisons would break
constantly. Replacing the prefix wholesale keeps the model's view a
monotonically growing chain (prefix caches stay warm) and swallows every
resume artifact (kickoff note, per-turn pre_llm_call context ride inside the
discarded slice).

Known limit: rounds that lived only inside the parked turn never reached the
transcript DB (mid-turn rows flush only at turn end), so the session viewer
may show a gap while the model's view stays continuous.

All code fails open: any internal error returns the request/next_call
untouched rather than breaking a turn.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.plugins.session-hold")

KICK_PREFIX = "[System note: session-hold resume]"
# Core's own crash auto-continue note is also a synthetic user row a resumed
# replay episode may legitimately see (tui_gateway/session_history.py).
_CORE_AUTOCONT_PREFIX = "[System note: Your previous turn"
REPLAY_HEADER = "X-Session-Hold-Replay"
_SNAP_MAX_BYTES = 4_000_000      # skip absurd payloads
_SNAP_MAX_AGE_SECS = 24 * 3600   # a day-old frozen request is stale; normal flow resumes
REPLAY_FRAME_CAP = 48            # per episode: appended assistant rows beyond this end it
POLL_SECS = 0.4
DEFAULT_PARK_TTL_SECS = 8 * 3600


# ------------------------------------------------------------- payload utils

def _json_copy(obj: Any) -> Any:
    """Deep JSON-safe copy; raises TypeError on callbacks/handles/odd keys."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, (str, int, float, bool)):
                raise TypeError(f"unserializable dict key {k!r}")
            out[str(k)] = _json_copy(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_json_copy(i) for i in obj]
    raise TypeError(f"unserializable {type(obj).__name__}")


def _messages_field(request: Dict[str, Any]) -> Optional[str]:
    if isinstance(request.get("messages"), list):
        return "messages"
    if isinstance(request.get("input"), list):
        return "input"
    return None


def _content_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def _is_synth_kickoff(msg: Any) -> bool:
    """A user row synthesized to start a continuation turn (ours, or core's
    crash auto-continue note) — invisible to the model during a replay."""
    return (isinstance(msg, dict) and msg.get("role") == "user"
            and _content_text(msg).startswith((KICK_PREFIX, _CORE_AUTOCONT_PREFIX)))


def _is_replay(request: Dict[str, Any]) -> bool:
    headers = request.get("extra_headers")
    return isinstance(headers, dict) and headers.get(REPLAY_HEADER) == "1"


def _stamp_replay(request: Dict[str, Any]) -> Dict[str, Any]:
    """Marker rides in extra_headers (never top-level: unknown api_kwargs keys
    crash client.create())."""
    headers = dict(request.get("extra_headers") or {})
    headers[REPLAY_HEADER] = "1"
    request["extra_headers"] = headers
    return request


def assistant_has_tool_calls(msg: Any) -> bool:
    """dict OR SDK-object aware."""
    if isinstance(msg, dict):
        return bool(msg.get("tool_calls"))
    return bool(getattr(msg, "tool_calls", None))


# ------------------------------------------------------------- snapshot store

class SnapshotStore:
    """Durable frozen payloads: <base>/frozen/<sha1(key)>.json holds the
    messages/input rows only (envelope params stay live so a model or toolset
    switch across the restart is honored; timeout/http_client are never
    JSON-safe and must not be persisted)."""

    def __init__(self, base_getter: Callable[[], Path]):
        self._base_getter = base_getter

    def _path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self._base_getter() / "frozen" / f"{h}.json"

    def save(self, key: str, request: Dict[str, Any]) -> bool:
        field = _messages_field(request or {})
        if field is None:
            return False
        try:
            blob = json.dumps({"key": key, "saved_at": time.time(),
                               "payload": {"field": field,
                                           "messages": _json_copy(request[field])}},
                              ensure_ascii=False)
        except (TypeError, ValueError, RecursionError) as exc:
            logger.debug("session-hold: snapshot not JSON-safe (%s): %s", key, exc)
            return False
        if len(blob.encode("utf-8")) > _SNAP_MAX_BYTES:
            return False
        try:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            from utils import atomic_json_write
            atomic_json_write(path, json.loads(blob), indent=None, mode=0o600)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("session-hold: snapshot write failed: %s", exc)
            return False

    def ensure(self, key: str, request: Dict[str, Any]) -> str:
        """Park-time save with restart awareness: an EXISTING snapshot (from a
        pre-restart park) is kept intact — it holds the mid-turn rounds the
        transcript DB never persisted; re-saving the resumed request would
        clobber it. Returns 'fresh' (saved now), 'preserved' (pre-existing),
        'failed' (nothing durable)."""
        if self.load(key) is not None:
            return "preserved"
        return "fresh" if self.save(key, request) else "failed"

    def rearm(self, key: str, request: Dict[str, Any]) -> bool:
        """A fresh /hold replaces any stale snapshot with THIS request."""
        self.discard(key)
        return self.save(key, request)

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - FileNotFoundError is the common case
            return None
        try:
            if not isinstance(data, dict) or str(data.get("key") or "") != key:
                return None  # collision or tamper: refuse
            if time.time() - float(data.get("saved_at") or 0) > _SNAP_MAX_AGE_SECS:
                self.discard(key)
                return None
            payload = data.get("payload")
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                return None
            return payload
        except Exception:  # noqa: BLE001
            return None

    def discard(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------ park registry

class ParkRegistry:
    """In-process handoff between the parking turn thread and /unhold."""

    def __init__(self):
        self._lock = threading.Lock()
        self._parks: Dict[str, dict] = {}

    def register(self, key: str, ev: threading.Event) -> None:
        with self._lock:
            self._parks[key] = {"event": ev, "at": time.time()}

    def release(self, key: str) -> bool:
        with self._lock:
            park = self._parks.get(key)
        if park is None:
            return False
        park["event"].set()
        return True

    def has_parked(self, key: str) -> bool:
        with self._lock:
            return key in self._parks

    def clear(self, key: str) -> None:
        with self._lock:
            self._parks.pop(key, None)

    def parked_keys(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._parks)

PARKS = ParkRegistry()

# live-session agent lookup used by the interrupt poll; injected by
# __init__.py: session_key -> Optional[agent]. None = unobservable, keep polling.
_agent_finder: Callable[[str], Any] = lambda key: None


def set_agent_finder(fn: Callable[[str], Any]) -> None:
    global _agent_finder
    _agent_finder = fn


def _interrupted(key: str) -> bool:
    try:
        agent = _agent_finder(key)
        return bool(agent is not None and getattr(agent, "_interrupt_requested", False))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------ llm_execution (park)

def make_park_middleware(state: Dict[str, dict], lock: threading.RLock,
                         store: SnapshotStore, persist: Callable[[], None],
                         keys_fn: Callable[[dict], Tuple[str, ...]],
                         park_ttl_secs: float = DEFAULT_PARK_TTL_SECS,
                         log_fn: Callable[[str], None] = lambda m: None,
                         ) -> Callable[..., Any]:
    """The park seam: block the turn thread at the request boundary.

    Fires on park-mode entries in phase armed (fresh /hold -> promote to held)
    or held (post-restart re-park), provided no thread is already parked for
    that key and no replay episode is pending (the llm_request middleware
    owns those frames). Strict key-ladder match only — never parks unrelated
    traffic on a guess."""

    def _fire(k: str) -> bool:
        v = state.get(k)
        return bool(v and v.get("mode", "park") == "park"
                    and not v.get("replay")
                    and v.get("phase") in {"armed", "held"}
                    and not PARKS.has_parked(k))

    def llm_execution(request: Dict[str, Any] = None, next_call: Callable = None,
                      **ctx: Any) -> Any:
        if request is None or next_call is None or not isinstance(request, dict):
            return next_call(request) if next_call else None
        try:
            if _is_replay(request):
                return next_call(request)  # a swapped frame — never re-park it
            with lock:
                key = next((k for k in keys_fn(ctx) if _fire(k)), None)
            if key is None:
                return next_call(request)
            with lock:
                e = state.get(key)
                if e is None or not _fire(key):
                    return next_call(request)
                fresh = e.get("phase") == "armed"
                if fresh:
                    e.update(phase="held", held_at=time.time())
                e["parked_at"] = time.time()
                persist()
            if _interrupted(key):
                with lock:
                    e = state.get(key)
                    if e is not None and e.get("phase") in {"armed", "held"}:
                        e.update(phase="idle")  # operator already spoke; drain
                        persist()
                return next_call(request)

            # armed: the pause point is THIS request — re-arm the snapshot even
            # if stale debris from a previous hold exists.
            # held re-park (post-restart): preserve the pre-restart snapshot —
            # it holds mid-turn rounds the transcript DB never persisted.
            ok = (store.rearm(key, request) if fresh
                  else store.ensure(key, request) != "failed")
            if not ok:
                with lock:
                    e = state.get(key)
                    if e is not None:
                        e.update(mode="toolblock")
                        persist()
                log_fn(f"request for {key} is not snapshot-safe; fell back to tool-block pause")
                return next_call(request)

            ev = threading.Event()
            PARKS.register(key, ev)
            log_fn(f"parked at the API boundary (session {key})")
            deadline = time.monotonic() + park_ttl_secs
            outcome = "unhold"
            while not ev.wait(POLL_SECS):
                if _interrupted(key):
                    outcome = "interrupt"
                    break
                if time.monotonic() > deadline:
                    outcome = "ttl"
                    break
            PARKS.clear(key)
            # Any wake drains the freeze: unhold/ttl hand the request to the
            # provider NOW (the snapshot is consumed by definition), interrupt
            # lets core's own partial-history path persist the dying turn.
            # The snapshot's ONLY job was surviving a process death while the
            # thread was parked — that window is over either way.
            store.discard(key)
            with lock:
                e = state.get(key)
                if e is None:
                    outcome = "interrupt"       # /hold off while parked
                elif outcome == "unhold":
                    if e.get("phase") == "held":
                        e.update(phase="released", mode=None)
                    persist()
                elif outcome == "interrupt":
                    e.update(phase="idle", mode=None)  # the operator spoke; stand down
                    persist()
                else:  # ttl: pass the call through; degrade to visible tool-block
                    e.update(mode="toolblock", park_expired=True)
                    persist()
                    log_fn(f"park TTL expired for {key}; degrading to tool-block")
            log_fn(f"park released ({outcome}) — handing the request to the provider")
            return next_call(request)
        except Exception as exc:  # noqa: BLE001 - never break a turn from here
            logger.warning("session-hold park failed open: %s", exc)
            try:
                return next_call(request)
            except Exception:  # noqa: BLE001
                raise
    return llm_execution


# ----------------------------------------------- llm_request (restart replay)

def make_request_replay_middleware(state: Dict[str, dict], lock: threading.RLock,
                                   store: SnapshotStore,
                                   keys_fn: Callable[[dict], Tuple[str, ...]],
                                   persist: Callable[[], None] = lambda: None,
                                   log_fn: Callable[[str], None] = lambda m: None,
                                   ) -> Callable[..., Any]:
    """The replay seam. While an entry is HELD with replay == "pending" (set
    by /unhold after a restart), this session's outgoing requests become pure
    appends onto the frozen payload; the episode self-closes via the
    post_api_request observer (model's final answer -> hold released).
    Synthetic kickoff user rows never reach the model during an episode and
    are stripped from pass-through frames too. Anomaly (missing snapshot,
    wire-shape change, shrunken history, real user message arriving, frame
    budget) ends the episode fail-open — released, never stuck held: /unhold
    already expressed the resume intent, so the worst case degrades to the
    reconstructed-history continuation, not a re-pause."""

    episodes: Dict[str, dict] = {}  # key -> {"count": n, "l1": int}

    def llm_request(request: Dict[str, Any] = None, **ctx: Any) -> Optional[dict]:
        try:
            if not isinstance(request, dict) or _is_replay(request):
                return None
            field = _messages_field(request)
            if field is None:
                return None
            live: List[Any] = request[field]

            def _strip_our_kick() -> Optional[dict]:
                # ONLY our own kickoff rows: core's crash auto-continue note
                # is load-bearing for the model when no hold is in play.
                keep = [m for m in live
                        if not (isinstance(m, dict) and m.get("role") == "user"
                                and _content_text(m).startswith(KICK_PREFIX))]
                if len(keep) == len(live):
                    return None
                out = copy.deepcopy(request)
                out[field] = keep
                return {"request": out}

            with lock:
                keys = list(keys_fn(ctx))
                key = next((k for k in keys
                            if state.get(k) and state[k].get("phase") == "held"
                            and state[k].get("replay") == "pending"), None)
            if key is None:
                return _strip_our_kick()
            if int(episodes.get(key, {}).get("count", 0)) >= REPLAY_FRAME_CAP:
                return _strip_our_kick()

            def _end(reason: str) -> None:
                store.discard(key)
                episodes.pop(key, None)
                with lock:
                    e = state.get(key)
                    if e is not None:
                        # the pause intent is honored (a /unhold initiated every
                        # episode): end fully released, never stuck held. The
                        # live frame passes through UNtouched — the kickoff row
                        # stays visible so the model retains its resume cue.
                        e.update(replay=None, phase="released", mode=None)
                        persist()
                log_fn(f"replay episode for {key} ended ({reason}); hold released")
                return None

            snap = store.load(key)
            if snap is None:
                return _end("snapshot missing/stale")
            if str(snap.get("field") or "") != field:
                return _end("wire-shape changed")
            frozen: List[Any] = snap["messages"]
            ep = episodes.get(key)

            if ep is None:
                # ---- frame 1. The live tail row is this turn's prompt: a
                # synthetic kickoff -> replay pure frozen (the resume request
                # is swallowed; the model never sees "please continue"). A
                # REAL user message -> replay frozen + that message, so the
                # pause releases AND the model reads what the user just said.
                tail = live[-1] if live else None
                keep_tail = (isinstance(tail, dict) and tail.get("role") == "user"
                             and not _is_synth_kickoff(tail))
                replay_msgs = list(frozen) + ([tail] if keep_tail else [])
                patched = copy.deepcopy(request)
                patched[field] = replay_msgs
                patched = _stamp_replay(patched)
                episodes[key] = {"count": 1, "l1": len(live)}
                log_fn(f"replayed frozen context (frame 1{', +user message' if keep_tail else ''})"
                       f" into session {key}")
                return {"request": patched}

            # ---- frame N: frozen prefix + the loop's appends after frame 1
            l1 = ep.get("l1")
            if not isinstance(l1, int) or len(live) < l1:
                return _end("history shrank or was rebuilt (compaction/takeover)")
            appended = live[l1:]
            if any(isinstance(m, dict) and m.get("role") == "user"
                   and not _is_synth_kickoff(m) for m in appended):
                return _end("a real user message arrived; replay yielded to it")
            assistants = sum(1 for m in appended
                             if isinstance(m, dict) and m.get("role") == "assistant")
            if assistants > REPLAY_FRAME_CAP:
                return _end("replay frame budget spent")
            replay_msgs = list(frozen) + list(appended)
            patched = copy.deepcopy(request)
            patched[field] = replay_msgs
            patched = _stamp_replay(patched)
            ep["count"] = int(ep.get("count", 0)) + 1
            log_fn(f"replayed frozen context frame {ep['count']} into session {key}")
            return {"request": patched}
        except Exception as exc:  # noqa: BLE001
            logger.debug("session-hold replay failed open: %s", exc)
            return None

    llm_request.episodes = episodes  # exposed for tests
    return llm_request


def close_replay_episode(state: Dict[str, dict], lock: threading.RLock,
                         store: SnapshotStore, keys: Tuple[str, ...],
                         persist: Callable[[], None],
                         log_fn: Callable[[str], None] = lambda m: None) -> bool:
    """post_api_request observer entry: a replayed-pending turn produced a
    final assistant answer (no tool calls) — the held work concluded. Release
    the hold, end the episode, discard the snapshot."""
    with lock:
        for k in keys:
            e = state.get(k)
            if e and e.get("phase") == "held" and e.get("replay") == "pending":
                e.update(phase="released", replay=None, mode=None)
                persist()
                store.discard(k)
                log_fn(f"replay episode for {k} completed; hold released")
                return True
    return False
