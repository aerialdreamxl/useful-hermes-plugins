"""Agent Trace Viewer — dashboard backend (read-only).

Mounted by the web server at ``/api/plugins/agent-trace-viewer/``.

Purpose: restore DeepSeek-Harness-style agent traces from Hermes' canonical
session store (``state.db``). Three endpoints:

- ``GET /sessions``                    — session list (searchable, paged)
- ``GET /sessions/{id}/messages``      — raw trace rows (chronological)
- ``GET /sessions/{id}/export``        — reconstructed OpenAI ``messages[]``

Safety contract:
- Every connection opens ``state.db`` with ``PRAGMA query_only = ON`` —
  this backend can NEVER write to the session store.
- ``/export`` carries its own auth check: it requires the dashboard session
  token (header ``X-Hermes-Session-Token`` or ``?token=``), the same value the
  SPA receives as ``window.__HERMES_SESSION_TOKEN__``. Token-less requests from
  a non-loopback peer are refused with 403; loopback stays allowed (with a
  warning) so the local viewer keeps working.
- Reconstruction is deterministic and lossless: ``content`` + ``tool_calls`` +
  ``tool_call_id`` + ``reasoning`` map 1:1 onto the OpenAI chat schema.
  Hermes' ``api_content`` column is ~all NULL in practice, so we rebuild
  instead of trusting it.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Export auth (F24)
# ---------------------------------------------------------------------------
# 导出 = 全库会话转录（system prompt / reasoning / 工具入参）。核心的
# auth_middleware 已经在 /api/* 上验 X-Hermes-Session-Token，但本端点不依赖
# 挂载层：自己再验一遍，同口径（web_server._SESSION_TOKEN，SPA 注入为
# window.__HERMES_SESSION_TOKEN__）。核心 header 名实查 web_server.py 为
# X-Hermes-Session-Token；X-Hermes-Token 作为兼容别名一并接受。
_TOKEN_HEADERS = ("X-Hermes-Session-Token", "X-Hermes-Token")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _core_session_token() -> str:
    """核心进程的会话令牌；核心模块不可用时返回 ""（随后只放过 loopback）。"""
    try:
        from hermes_cli import web_server

        return str(getattr(web_server, "_SESSION_TOKEN", "") or "")
    except Exception:  # noqa: BLE001 — 独立加载/单测环境没有核心模块
        return ""


def _has_session_token(request: Request) -> bool:
    """header / Bearer / ?token= 任一与核心令牌同值。"""
    expected = _core_session_token()
    if not expected:
        return False
    candidates: list[str] = []
    for name in _TOKEN_HEADERS:
        value = request.headers.get(name, "")
        if value:
            candidates.append(value)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    query_token = request.query_params.get("token", "")
    if query_token:
        candidates.append(query_token)
    return any(hmac.compare_digest(c.encode(), expected.encode()) for c in candidates)


def _is_loopback(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip().lower()
    if host.startswith("::ffff:"):  # IPv4-mapped IPv6
        host = host[len("::ffff:"):]
    return host in _LOOPBACK_HOSTS


def _require_export_token(request: Request) -> None:
    """导出端点的独立鉴权：令牌/核心已认证主体 → loopback 告警放行 → 其余 403。"""
    if (getattr(request.state, "token_authenticated", False)
            or getattr(request.state, "native_http_principal", None)
            or getattr(request.state, "hosted_http_authenticated", None) is True):
        return  # 与核心 _require_token 同口径：外层 seam 已认证的主体不再重复验令牌
    if (getattr(request.app.state, "auth_required", False)
            and getattr(request.state, "session", None) is not None):
        return  # OAuth 门开启时 cookie 会话是权威（核心此时不注入静态令牌）
    if _has_session_token(request):
        return
    if _is_loopback(request):
        peer = getattr(getattr(request, "client", None), "host", "?")
        log.warning("agent-trace-viewer: /export 无会话令牌，来源 loopback(%s)，兼容放行", peer)
        return
    raise HTTPException(status_code=403, detail="export requires the dashboard session token")


# ---------------------------------------------------------------------------
# state.db resolution
# ---------------------------------------------------------------------------

def _state_db_path() -> Path:
    """Locate state.db across plausible Hermes homes (env wins)."""
    candidates: list[Path] = []

    env = os.environ.get("HERMES_HOME")
    if env:
        candidates.append(Path(env))
    try:
        from hermes_constants import get_process_hermes_home

        candidates.append(get_process_hermes_home())
    except Exception:  # noqa: BLE001 — resolver optional in some contexts
        pass

    for base in candidates:
        try:
            p = Path(base) / "state.db"
            if p.exists():
                return p
        except (OSError, TypeError):
            continue
    raise HTTPException(status_code=503, detail="state.db not found")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_state_db_path()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")  # hard read-only guarantee
    return con


def _ts(value: Any) -> Optional[str]:
    """Normalize timestamps to ISO-8601 regardless of stored shape."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    return str(value)


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    try:
        with _connect() as con:
            n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return {"ok": True, "db": str(_state_db_path()), "sessions": n}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"state.db unreadable: {exc}")


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str = Query("", max_length=200),
) -> dict:
    where = ""
    params: list[Any] = []
    if q:
        where = "WHERE (COALESCE(title,'') LIKE ? OR COALESCE(display_name,'') LIKE ? OR id LIKE ?)"
        like = f"%{q}%"
        params = [like, like, like]

    with _connect() as con:
        rows = con.execute(
            f"""
            SELECT id, display_name, title, model, source, profile_name,
                   message_count, tool_call_count, started_at,
                   last_activity_at, archived
            FROM sessions
            {where}
            ORDER BY COALESCE(last_activity_at, started_at) DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

    return {
        "sessions": [
            {
                "id": r["id"],
                "title": r["title"] or r["display_name"] or r["id"],
                "display_name": r["display_name"],
                "model": r["model"],
                "source": r["source"],
                "profile": r["profile_name"],
                "message_count": r["message_count"],
                "tool_call_count": r["tool_call_count"],
                "started_at": _ts(r["started_at"]),
                "last_activity_at": _ts(r["last_activity_at"]),
                "archived": bool(r["archived"]),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Trace rows
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/messages")
async def session_messages(
    session_id: str,
    limit: int = Query(300, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    order: str = Query("latest", pattern="^(latest|oldest)$"),
) -> dict:
    """Raw trace rows for the viewer.

    order=latest returns the NEWEST `limit` rows (chronological ascending in
    the payload after reversal); order=oldest pages from the start.
    """
    with _connect() as con:
        exists = con.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="session not found")

        direction = "DESC" if order == "latest" else "ASC"
        rows = con.execute(
            f"""
            SELECT id, role, content, tool_call_id, tool_calls, tool_name,
                   timestamp, reasoning, reasoning_content, compacted, active
            FROM messages
            WHERE session_id = ?
            ORDER BY id {direction}
            LIMIT ? OFFSET ?
            """,
            (session_id, limit, offset),
        ).fetchall()

    if order == "latest":
        rows = list(reversed(rows))  # payload is always chronological asc

    out = []
    for r in rows:
        tool_calls = None
        if r["tool_calls"]:
            try:
                tool_calls = json.loads(r["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                tool_calls = None
        out.append(
            {
                "row_id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "tool_call_id": r["tool_call_id"],
                "tool_calls": tool_calls,
                "tool_name": r["tool_name"],
                "timestamp": _ts(r["timestamp"]),
                "reasoning": r["reasoning"],
                "compacted": bool(r["compacted"]),
                "active": bool(r["active"]),
            }
        )

    return {"session_id": session_id, "order": order, "count": len(out), "messages": out}


# ---------------------------------------------------------------------------
# OpenAI messages[] export
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/export")
async def export_messages(session_id: str, request: Request) -> dict:
    """Reconstruct the OpenAI chat-completions ``messages`` list for a session.

    Mapping (deterministic, lossless):
    - system           ← sessions.system_prompt
    - role='user'      ← {role, content}
    - role='assistant' ← {role, content?, tool_calls?, reasoning_content?}
    - role='tool'      ← {role, tool_call_id, name?, content}
    Internal rows (role='session_meta') are skipped.

    Auth: requires the dashboard session token (see ``_require_export_token``);
    a token-less non-loopback caller gets 403.
    """
    _require_export_token(request)
    with _connect() as con:
        sess = con.execute(
            "SELECT id, title, display_name, model, started_at, system_prompt "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not sess:
            raise HTTPException(status_code=404, detail="session not found")

        rows = con.execute(
            "SELECT role, content, tool_call_id, tool_calls, tool_name, "
            "reasoning, reasoning_content, compacted "
            "FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()

    messages: list[dict] = []
    if sess["system_prompt"]:
        messages.append({"role": "system", "content": sess["system_prompt"]})

    for r in rows:
        role = r["role"]
        if role == "session_meta":
            continue
        if role == "user":
            messages.append({"role": "user", "content": r["content"] or ""})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            msg["content"] = r["content"] if r["content"] else None
            if r["tool_calls"]:
                try:
                    parsed = json.loads(r["tool_calls"])
                    calls = []
                    for c in parsed:
                        fn = c.get("function") or {}
                        calls.append(
                            {
                                "id": c.get("id") or c.get("call_id"),
                                "type": "function",
                                "function": {
                                    "name": fn.get("name"),
                                    "arguments": fn.get("arguments") or "{}",
                                },
                            }
                        )
                    if calls:
                        msg["tool_calls"] = calls
                except (json.JSONDecodeError, TypeError):
                    pass
            reasoning = r["reasoning"] or r["reasoning_content"]
            if reasoning:
                msg["reasoning_content"] = reasoning
            messages.append(msg)
        elif role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": r["tool_call_id"],
                    "name": r["tool_name"],
                    "content": r["content"] or "",
                }
            )

    return {
        "meta": {
            "generator": "hermes-agent-trace-viewer/1.0",
            "session_id": session_id,
            "title": sess["title"] or sess["display_name"],
            "model": sess["model"],
            "started_at": _ts(sess["started_at"]),
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "message_count": len(messages),
            "schema": "openai-chat-completions-messages",
        },
        "messages": messages,
    }
