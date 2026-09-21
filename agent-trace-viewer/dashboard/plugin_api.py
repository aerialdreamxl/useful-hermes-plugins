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
- Reconstruction is deterministic and lossless: ``content`` + ``tool_calls`` +
  ``tool_call_id`` + ``reasoning`` map 1:1 onto the OpenAI chat schema.
  Hermes' ``api_content`` column is ~all NULL in practice, so we rebuild
  instead of trusting it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger(__name__)

router = APIRouter()


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
    # Known install on this host (fallback of last resort).
    candidates.append(Path(r"D:/01-AI/Hermes-Agent/Data"))

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
async def export_messages(session_id: str) -> dict:
    """Reconstruct the OpenAI chat-completions ``messages`` list for a session.

    Mapping (deterministic, lossless):
    - system           ← sessions.system_prompt
    - role='user'      ← {role, content}
    - role='assistant' ← {role, content?, tool_calls?, reasoning_content?}
    - role='tool'      ← {role, tool_call_id, name?, content}
    Internal rows (role='session_meta') are skipped.
    """
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
