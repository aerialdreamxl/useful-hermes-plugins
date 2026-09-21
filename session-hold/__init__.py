"""session-hold: /hold pauses the session; /unhold resumes WITHOUT the user
typing "continue" — and the pause survives a restart. v2 makes the pause
invisible to the model wherever possible.

Modes (per session entry; the durable state lives in
$HERMES_HOME/session-hold/state.json + frozen/<sha1>.json):

- PARK (default, live process): the `llm_execution` middleware holds the final
  provider request at the next API boundary and blocks the turn thread; /unhold
  hands the SAME request to the provider. The model sees one slow call — no
  wrap-up message, no resume note, no trace of the pause. If the process dies
  while parked, the durable snapshot lets /unhold after a restart replay frame
  1 byte-exactly via the `llm_request` middleware.
- TOOL-BLOCK (v1, fallback): used when park is disabled (config), the request
  is not snapshot-safe, or a replay episode hit an anomaly. `pre_tool_call`
  blocks tools with a wrap-up directive; /unhold kicks a continuation turn whose
  kickoff note the replay layer normally swallows from the model's view.

Commands: /hold [off|status|<key>], /unhold [<key>]. (/pause and /resume are
built-in registry commands; register_command refuses name conflicts.)

Hard rules honored: never touches the gateway's running-agent escape hatches;
fail-open everywhere except the intentional hold blocks; middleware callbacks
run inline (no per-callback timeout, verified plugins_dispatch.py) which is
what makes park legal — and why every park path is wrapped in try/except with
a next_call() escape.
"""
from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:  # package import (hermes loads dir plugins as packages); flat-import fallback
    from . import freeze as _freeze
except ImportError:  # pragma: no cover
    import freeze as _freeze  # type: ignore

PLUGIN_ID = "session-hold"
logger = logging.getLogger(f"hermes.plugins.{PLUGIN_ID}")

_ARM_TTL_SECS = 8 * 3600  # an armed-but-never-fired hold expires so a stale
                          # /hold can't booby-trap tomorrow's new work

HOLD_MESSAGE = (
    "\u23f8 SESSION HOLD \u2014 the user paused this session with /hold. This tool call "
    "has been refused. Do NOT call any other tool or take any side-effecting action for "
    "the rest of this turn. Write a short summary (2-4 lines) of what you were doing and "
    "what remains, then end the turn and wait."
)

def _resume_note(task_note: str) -> str:
    tail = f"\nThe task that was interrupted: {task_note}" if task_note else ""
    return (
        "[System note: The user released the /hold pause on this session. If the previous "
        "turn left work unfinished, resume it immediately from where it stopped \u2014 check "
        f"the current state first, then continue WITHOUT asking for confirmation to proceed.{tail}]"
    )

# ---------------------------------------------------------------- state store

_ctx_ref: list = []
_lock = threading.RLock()
_state: Dict[str, dict] = {}   # key -> {phase, turn_id, task_note, armed_at, ...}
_loaded = False


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _state_path() -> Path:
    return _home() / "session-hold" / "state.json"


def _persist_locked() -> None:
    """Write the durable state; caller holds _lock. Never raises. Uses the same
    atomic writer the core turn-marker store uses (Windows-safe)."""
    try:
        from utils import atomic_json_write
        # GC dead entries: released/idle with nothing pending behind them
        for k in [k for k, v in _state.items()
                  if v.get("phase") in {"idle", "released"}
                  and not v.get("resume_note") and not v.get("replay")]:
            _state.pop(k)
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, dict(_state), indent=None, mode=0o600)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: persist failed: %s", PLUGIN_ID, exc)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and isinstance(v, dict):
                    _state[k] = v
        # Expire stale arms restored from a previous run.
        now = time.time()
        changed = False
        for k, v in list(_state.items()):
            if v.get("phase") == "armed" and now - float(v.get("armed_at") or 0) > _ARM_TTL_SECS:
                _state.pop(k)
                changed = True
        if changed:
            _persist_locked()
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: state load failed: %s", PLUGIN_ID, exc)


def _entry(key: str) -> Optional[dict]:
    with _lock:
        _load()
        e = _state.get(key)
        if e and e.get("phase") == "armed" and time.time() - float(e.get("armed_at") or 0) > _ARM_TTL_SECS:
            _state.pop(key)
            _persist_locked()
            return None
        return e

# ---------------------------------------------------------- session key resolve

def _approval_key() -> str:
    try:
        from tools.approval import get_current_session_key
        return str(get_current_session_key(default="") or "")
    except Exception:  # noqa: BLE001
        return ""


def _cli_ref():
    try:
        return getattr(getattr(_ctx_ref[0], "_manager", None), "_cli_ref", None) if _ctx_ref else None
    except Exception:  # noqa: BLE001
        return None


def _tui_server():
    try:
        srv = importlib.import_module("tui_gateway.server")
        sessions = getattr(srv, "_sessions", None)
        return srv if isinstance(sessions, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _resolve_key() -> Tuple[str, str]:
    """Best-effort (key, kind) for the session the user means while running a
    slash command OUTSIDE a turn (RPC/input thread: no approval contextvar).
    kind: explicit|approval|cli|tui-single|tui-idle|tui-recent|tui-busy|none."""
    key = _approval_key()
    if key:
        return key, "approval"
    cli = _cli_ref()
    if cli is not None:
        k = str(getattr(cli, "session_id", "") or "")
        if k:
            return k, "cli"
    srv = _tui_server()
    if srv is not None:
        recs = [s for s in list(srv._sessions.values()) if isinstance(s, dict)]
        running = [str(s.get("session_key") or "") for s in recs if s.get("running")]
        running = [k for k in running if k]
        if len(running) == 1:
            return running[0], "tui-single"
        if len(running) > 1:
            return "", "tui-busy"
        # nothing running: single-window desktop is unambiguous; otherwise the
        # most recently touched chat is the one the user is typing in.
        keyed = [str(s.get("session_key") or "") for s in recs]
        keyed = [k for k in keyed if k]
        if len(keyed) == 1:
            return keyed[0], "tui-idle"
        if len(keyed) > 1:
            best = max(recs, key=lambda s: _as_float(s.get("last_active")))
            return str(best.get("session_key") or ""), "tui-recent"
    return "", "none"


def _as_float(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0

# ------------------------------------------------------------- v2 singletons

_SNAPSTORE = _freeze.SnapshotStore(lambda: _home() / "session-hold")


def _mw_keys(ctx: dict) -> Tuple[str, ...]:
    """Key ladder for middleware ctx (same shape as hooks: routing key first,
    then the ephemeral task uuid, then the durable agent session id)."""
    return _turn_keys(str((ctx or {}).get("task_id") or ""),
                      str((ctx or {}).get("session_id") or ""))


def _mw_log(msg: str) -> None:
    logger.info("%s: %s", PLUGIN_ID, msg)


def _find_agent(key: str):
    """Live agent object for a routing key (park's interrupt poll). None =
    unobservable; the poll keeps waiting rather than guessing."""
    try:
        srv = _tui_server()
        if srv is not None:
            for s in list(srv._sessions.values()):
                if isinstance(s, dict) and s.get("session_key") == key:
                    ag = s.get("agent")
                    if ag is not None:
                        return ag
        cli = _cli_ref()
        if cli is not None and key and key == str(getattr(cli, "session_id", "") or ""):
            return getattr(cli, "agent", None)
    except Exception:  # noqa: BLE001
        pass
    return None


def _park_persist() -> None:
    """persist callback handed to freeze.py (it mutates _state under _lock)."""
    _persist_locked()


# ----------------------------------------------------------------- hooks

def _turn_keys(task_id: str = "", session_id: str = "") -> Tuple[str, ...]:
    """Candidate routing keys for an in-turn hook call, in ladder order (same
    shape smart-terminal-cwd proved on desktop: approval contextvar = routing
    session_key during a turn; task_id may be an ephemeral per-turn uuid;
    session_id = the durable agent session id)."""
    return tuple(dict.fromkeys(k for k in
                               (_approval_key(), str(task_id or ""), str(session_id or "")) if k))


def _lookup_entry(keys: Tuple[str, ...]) -> Optional[dict]:
    with _lock:
        _load()
        for k in keys:
            if (e := _state.get(k)):
                return e
    return None


def _make_pre_tool_call() -> Callable[..., Optional[dict]]:
    def _pre_tool_call(tool_name: str = "", args: Optional[dict] = None,
                       session_id: str = "", task_id: str = "", turn_id: str = "",
                       **_: Any) -> Optional[dict]:
        try:
            with _lock:
                _load()
                e = None
                for k in _turn_keys(task_id, session_id):
                    if (cand := _state.get(k)):
                        e = cand
                        break
                if not e:
                    return None
                phase = e.get("phase")
                if phase in {"armed", "held"} and e.get("mode", "park") == "park":
                    # PARK mode owns the pause at the API boundary (freeze.py):
                    # tools keep flowing — which is literally "pause AFTER the
                    # next tool call": the tool runs, and the model never gets
                    # to see its result until /unhold.
                    return None
                if phase == "armed":
                    e.update(phase="held", turn_id=turn_id, held_at=time.time(),
                             last_tool=tool_name)
                    _persist_locked()
                    return {"action": "block", "message": HOLD_MESSAGE}
                if phase == "held":
                    # Sticky by design: the pause spans turns AND restarts and is
                    # released only by /unhold (or /hold off). Blocks every tool
                    # in every turn of this session key until then.
                    e["last_tool"] = tool_name
                    return {"action": "block", "message": HOLD_MESSAGE}
            return None
        except Exception as exc:  # noqa: BLE001 - fail-open; hold is advisory safety, not a lock
            logger.warning("%s: pre_tool_call failed: %s", PLUGIN_ID, exc)
            return None
    return _pre_tool_call


def _make_pre_llm_call() -> Callable[..., Optional[dict]]:
    def _pre_llm_call(session_id: str = "", task_id: str = "", user_message: Any = None,
                      **_: Any) -> Optional[dict]:
        try:
            note = ""
            with _lock:
                _load()
                e = None
                for k in _turn_keys(task_id, session_id):
                    if (cand := _state.get(k)):
                        e = cand
                        break
                if e is None:
                    return None
                # remember a compact task anchor for hold/resume messages
                text = user_message if isinstance(user_message, str) else str(
                    (user_message or {}).get("content", "") if isinstance(user_message, dict) else "")
                if text and not text.lstrip().startswith("[System") and not text.lstrip().startswith("/"):
                    e["task_note"] = text[:400]
                if e.get("resume_note"):
                    e.pop("resume_note")
                    note = _resume_note(str(e.get("task_note") or ""))
                    _persist_locked()
            return {"context": note} if note else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: pre_llm_call failed: %s", PLUGIN_ID, exc)
            return None
    return _pre_llm_call

# ------------------------------------------------------------ kick channels

def _kick_tui(srv, key: str, note: str) -> bool:
    """Synthesize a continuation turn on desktop/TUI, mirroring core's
    session_auto_continue._maybe_schedule_auto_continue kickoff. Thread-guarded,
    best-effort; True when the kick was scheduled."""
    sid, session = None, None
    for cand_sid, s in list(srv._sessions.items()):
        if isinstance(s, dict) and s.get("session_key") == key:
            sid, session = cand_sid, s
            break
    if session is None or session.get("running"):
        return False

    def _run() -> None:
        rid = f"__hold_resume__{int(time.time() * 1000)}"
        try:
            try:
                srv._start_agent_build(sid, session)
                err = srv._wait_agent(session, rid, timeout=120.0)
            except Exception:  # noqa: BLE001
                err = {"error": {"message": "agent build failed"}}
            if err:
                _flag_resume_note(key, note)
                return
            with session["history_lock"]:
                if session.get("running") or session.get("_finalized"):
                    _flag_resume_note(key, note)
                    return
                session["running"] = True
                session["last_active"] = time.time()
            slot = getattr(srv, "_ensure_active_session_slot", None)
            if callable(slot) and slot(sid, session) is not None:
                with session["history_lock"]:
                    session["running"] = False
                _flag_resume_note(key, note)
                return
            srv._run_prompt_submit(rid, sid, session, note, display_kind="auto_continue")
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: tui kick failed: %s", PLUGIN_ID, exc)
            _flag_resume_note(key, note)

    threading.Thread(target=_run, name="session-hold-resume", daemon=True).start()
    return True


def _flag_resume_note(key: str, note: str) -> None:
    with _lock:
        _load()
        e = _state.setdefault(key, {"phase": "idle"})
        e["resume_note"] = note
        _persist_locked()


def _kick_cli(key: str, note: str) -> bool:
    cli = _cli_ref()
    q = getattr(cli, "_pending_input", None) if cli is not None else None
    try:
        if q is not None:
            q.put(note)
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: cli kick failed: %s", PLUGIN_ID, exc)
    return False


def _make_post_api_request() -> Callable[..., None]:
    """Close a park-replay episode: when the replayed-pending turn produces a
    final answer (no tool calls), the held work concluded — release the hold
    and discard the snapshot. Tool-block episodes are released by /unhold
    directly and are not touched here."""
    def _post_api_request(assistant_message: Any = None, session_id: str = "",
                          task_id: str = "", **_: Any) -> None:
        try:
            if _freeze.assistant_has_tool_calls(assistant_message):
                return
            with _lock:
                _load()
                for k in _turn_keys(task_id, session_id):
                    if _state.get(k, {}).get("replay") == "pending":
                        _freeze.close_replay_episode(
                            _state, _lock, _SNAPSTORE, (k,), _persist_locked)
                        return
        except Exception as exc:  # noqa: BLE001 - observer, always fail-open
            logger.debug("%s: post_api_request failed open: %s", PLUGIN_ID, exc)
    return _post_api_request

# ------------------------------------------------------------ commands

def _status_line(key: str) -> str:
    e = _entry(key) if key else None
    if not e:
        return f"\u25cf {key or '(no session resolved)'}: idle"
    bits = [f"\u25cf {key}: {e.get('phase')}"]
    mode = e.get("mode", "park")
    if mode == "park":
        bits.append("park @ API boundary" if key in _freeze.PARKS.parked_keys()
                    else "park (arms at next API call)")
    else:
        bits.append("tool-block")
    if e.get("replay") == "pending":
        bits.append("restart-replay queued for next turn")
    if e.get("last_tool"):
        bits.append(f"blocked at `{e['last_tool']}`")
    if e.get("resume_note"):
        bits.append("continuation queued for next turn")
    return " \u2014 ".join(bits)


def _make_hold_command() -> Callable[[str], str]:
    def _hold(raw_args: str = "") -> str:
        arg = (raw_args or "").strip()
        with _lock:
            _load()
        if arg.lower() in {"off", "cancel", "disarm"} or arg.lower().startswith(
                ("off ", "cancel ", "disarm ")):
            explicit = arg.partition(" ")[2].strip()
            key = explicit or _resolve_key()[0]
            if not key:
                return "\u26a0 could not resolve the session; pass /hold off <session_key>."
            _freeze.PARKS.release(key)  # wake a parked thread; it drains on the gone entry
            _SNAPSTORE.discard(key)
            if _state.pop(key, None):
                _persist_locked()
                return f"\u25cf hold disarmed for {key}"
            return "\u25cf no active hold to disarm."
        if arg.lower() == "status" or arg == "":
            key, kind = _resolve_key()
            if kind == "tui-busy":
                return ("\u26a0 multiple sessions are running \u2014 target one explicitly: "
                        "/hold <session_key>")
            if kind == "none":
                return "\u26a0 could not resolve the current session; pass /hold <session_key>."
            if arg.lower() == "status":
                return _status_line(key)
            if not key:
                return "\u26a0 could not resolve the current session; pass /hold <session_key>."
            e = _state.setdefault(key, {})
            e.pop("resume_note", None)  # a fresh arm starts clean; don't strand a stale continuation
            e.update(phase="armed", armed_at=time.time(), armed_kind=kind, mode="park")
            _persist_locked()
            return (f"\u23f8 Hold armed on {key}. The session pauses at the next API call "
                    "— the model never learns a pause happened. Release later with /unhold "
                    "(works even after a restart).")
        # explicit session key form
        with _lock:
            _load()
            e = _state.setdefault(arg, {})
            e.pop("resume_note", None)
            e.update(phase="armed", armed_at=time.time(), armed_kind="explicit", mode="park")
            _persist_locked()
        return f"\u23f8 Hold armed on {arg}."
    return _hold


def _make_unhold_command() -> Callable[[str], str]:
    def _unhold(raw_args: str = "") -> str:
        arg = (raw_args or "").strip()
        key = arg or _resolve_key()[0]
        if not key:
            return "\u26a0 could not resolve the session; pass /unhold <session_key>."
        with _lock:
            _load()
            e = _state.get(key)
            if not e or (e.get("phase") not in {"armed", "held"} and not e.get("resume_note")):
                return f"\u25cf no active hold on {key}."
            was = e.get("phase")
            had_note = bool(e.get("resume_note"))
            task_note = str(e.get("task_note") or "")
        # 1) a thread is parked on this key in THIS process: release it and the
        # exact frozen request goes out — fully invisible, no continuation turn.
        if _freeze.PARKS.release(key):
            return (f"\u25b6 {key} resumed — the parked API call continues; "
                    "the model never sees the pause.")
        # 2) park mode with a durable snapshot (we restarted since the park):
        # arm the replay episode, then kick a continuation turn.
        replayed = False
        with _lock:
            e = _state.get(key)
            if (e is not None and e.get("phase") == "held"
                    and e.get("mode", "park") == "park"
                    and _SNAPSTORE.load(key)):
                e["replay"] = "pending"
                e.pop("resume_note", None)
                _persist_locked()
                replayed = True
        if replayed:
            tail = f" The task that was in flight: {task_note}" if task_note else ""
            note = (_freeze.KICK_PREFIX + " A /hold pause was released for this session"
                    " and the paused request resumes automatically." + tail)
            srv = _tui_server()
            if srv is not None and _kick_tui(srv, key, note):
                return (f"\u25b6 {key} resumed — the frozen API call replays on the next "
                        "turn; the model sees one continuous run.")
            # no live session: replay still fires when the user opens the chat
            # and sends ANYTHING (frame 1 = frozen + their message).
            return (f"\u25b6 {key}: replay armed. Open the session and send anything — "
                    "the paused request resumes seamlessly (your message is kept).")
        # 3) v1 tool-block semantics (or armed-never-fired): clear + kick note.
        note = _resume_note(task_note)
        with _lock:
            e = _state.get(key)
            if e is not None and not had_note:
                e.pop("resume_note", None)
                e["phase"] = "idle"
                _persist_locked()
        srv = _tui_server()
        if srv is not None and _kick_tui(srv, key, note):
            return f"\u25b6 Hold released \u2014 {key} is resuming now (no typing needed)."
        if _kick_cli(key, note):
            return f"\u25b6 Hold released \u2014 continuation queued; CLI will run it as soon as it accepts input."
        _flag_resume_note(key, note)
        verb = "was already released" if had_note else f"{was} cleared"
        return (f"\u25b6 {verb}. No live session found to auto-kick \u2014 the continuation is "
                "queued as a one-shot: the next message in this session resumes the held work "
                "automatically (open the chat and send anything, e.g. `/unhold` again).")
    return _unhold

# ------------------------------------------------------------------ register

def register(ctx) -> None:
    _ctx_ref.append(ctx)
    ctx.register_hook("pre_tool_call", _make_pre_tool_call())
    ctx.register_hook("pre_llm_call", _make_pre_llm_call())
    ctx.register_hook("post_api_request", _make_post_api_request())
    _freeze.set_agent_finder(_find_agent)
    ctx.register_middleware(
        "llm_execution",
        _freeze.make_park_middleware(_state, _lock, _SNAPSTORE, _park_persist,
                                     _mw_keys, log_fn=_mw_log))
    ctx.register_middleware(
        "llm_request",
        _freeze.make_request_replay_middleware(_state, _lock, _SNAPSTORE,
                                               _mw_keys, _park_persist, log_fn=_mw_log))
    ctx.register_command("hold", _make_hold_command(),
                         description="Pause this session at the next API call, invisibly to "
                                     "the model (survives restarts); '/hold off' disarms, "
                                     "'/hold status' shows state",
                         args_hint="[off|status|<session_key>]")
    ctx.register_command("unhold", _make_unhold_command(),
                         description="Release a /hold pause and resume the session automatically "
                                     "\u2014 even after a Hermes restart",
                         args_hint="[<session_key>]")
    logger.info("%s: registered /hold + /unhold (park @ API boundary, durable replay)", PLUGIN_ID)
