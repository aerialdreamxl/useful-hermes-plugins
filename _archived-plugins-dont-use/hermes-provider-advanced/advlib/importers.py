"""Account import — normalize foreign credential dumps into store entries.

Sources recognized (recon-verified shapes; product-agnostic by design —
we match SHAPE, then let platform adapters claim the credential):

- a single bare account object (``{access_token, refresh_token, ...}`` —
  sub2api credential maps, hand-written JSON)
- a list of such objects (directory dumps merged, JSON array)
- a dict keyed by name/email mapping to account objects
- sub2api-style rows: ``{name/email, credentials|credential: {...}}``
- CPA-style records: the ``type`` field ("codex" / "claude" / "gemini"…)
  routes the record to its platform
- core auth.json fragments: source-keyed rows carrying ``tokens`` dicts

``oauth import <file>`` runs :func:`extract_accounts` and offers each
candidate for the CLI to store (with a merge/overwrite decision per slot).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.advanced_providers")

# CPA `type` values -> platform keys
_TYPE_PLATFORM = {
    "codex": "codex",
    "openai": "codex",
    "claude": "claude",
    "anthropic": "claude",
    "gemini": "antigravity",
    "antigravity": "antigravity",
    "google": "antigravity",
    "qwen": "qwen",
    "qwen-oauth": "qwen",
}


def load_json(path_or_text: str) -> Any:
    """Parse a file (path) or inline JSON text; raises ValueError with a
    pointer on either failure."""
    text = str(path_or_text or "").strip()
    if not text:
        raise ValueError("empty import source")
    p = Path(text)
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8-sig"))
    except OSError:
        pass  # not a readable path: fall through to inline JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"import source is neither a readable file nor valid JSON: {exc}") from exc


def extract_accounts(payload: Any) -> list[dict]:
    """Recognized candidates: [{platform, name, account, source}], deduped.

    ``name`` is a stable display key (email/type/id) — the CLI lets the user
    rename per slot before storing.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(platform: str, name: str, acct: dict, source: str) -> None:
        key = (platform, str(acct.get("access_token") or acct.get("refresh_token") or name))
        if key in seen:
            return
        seen.add(key)
        out.append({"platform": platform, "name": name, "account": acct, "source": source})

    def try_record(raw: dict, source_hint: str, label: str = "") -> bool:
        """One credential-ish dict: route to adapters in priority order."""
        if not isinstance(raw, dict):
            return False
        # unwrap nested credential containers first (sub2api rows, auth.json)
        inner = raw
        for container_key in ("credentials", "credential", "tokens", "token"):
            sub = raw.get(container_key)
            if isinstance(sub, dict) and any(
                    k in sub for k in ("access_token", "refresh_token", "id_token", "accessToken")):
                inner = {**inner, **sub}  # container fields win over row metadata
                break
        ptype = str(inner.get("type") or inner.get("platform") or inner.get("provider") or "").strip().lower()
        hinted = _TYPE_PLATFORM.get(ptype, "")
        from .adapters import all_adapters  # registry iteration order

        candidates = ([hinted] if hinted else []) + [a.name for a in all_adapters()]
        seen_platforms = set()
        for pname in candidates:
            if not pname or pname in seen_platforms:
                continue
            seen_platforms.add(pname)
            ad = _get(pname)
            if ad is None:
                continue
            # platform_hint comes from the DATA's own type field only — passing
            # the candidate name would make every adapter self-match anything.
            try:
                acct = ad.normalize(inner, platform_hint=pname if pname == hinted else "")
            except Exception:
                continue
            if acct:
                name = str(label or raw.get("name") or raw.get("email")
                           or acct.get("email") or raw.get("id") or "") or f"{pname}-{len(seen)}"
                acct["_import_source"] = source_hint or ptype or pname
                add(pname, name, acct, source_hint or "shape")
                return True
        return False

    if isinstance(payload, dict):
        # CPA/auth/sub2api single record, or a keyed map
        handled = False
        if any(k in payload for k in ("access_token", "refresh_token", "id_token",
                                      "accessToken", "credentials", "tokens")):
            handled = try_record(payload, "record")
        if not handled:
            # dict keyed by name -> record (codex-auth-style dumps)
            for key, val in payload.items():
                if isinstance(val, dict):
                    try_record(val, "keyed", label=str(key))
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            try_record(item, "keyed-list", label=str(key))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                try_record(item, "array")
    return out


def _get(platform: str):
    from .adapters import get

    return get(platform)
