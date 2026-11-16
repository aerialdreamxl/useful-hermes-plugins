"""Fingerprint ``hermes`` (default): keep Hermes' native client identity.

"能原生就原生" — identity headers already set upstream (core's
``apply_required_codex_headers`` for the official codex host, SDK defaults)
are respected; this profile only FILLS what is missing, never rewrites.
"""

from __future__ import annotations

from typing import Any

NAME = "hermes"


def _codex_identity(access_token: str) -> dict:
    try:
        from agent.codex_headers import codex_cloudflare_headers

        return codex_cloudflare_headers(access_token)
    except Exception:
        return {}


def _version_tuple(version: str) -> tuple:
    parts = []
    for chunk in str(version or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3]) or (0,)


#: Anthropic rejects OAuth requests whose Claude Code UA is far behind the
#: current release ("Claude Code X does not support this model", see core's own
#: note in anthropic_adapter.py). Core's detection falls back to a stale
#: constant when no CLI is installed — the normal case on a Windows desktop —
#: so treat anything below the version the CPA/Sub2API ports pin as "no native
#: identity" and let the resolver drop to a profile carrying a current one.
_CLAUDE_MIN_VERSION = (2, 1, 258)


def _claude_identity() -> dict:
    """Hermes' own Claude identity, mirroring ``agent.anthropic_adapter``'s
    OAuth branch (``user-agent: claude-code/<ver> (external, cli)`` +
    ``x-app: cli``). Empty dict = "no usable native identity": core helper
    gone, or the detected CLI version is old enough to be rejected."""
    try:
        from agent.anthropic_adapter import _get_claude_code_version

        version = str(_get_claude_code_version() or "").strip()
    except Exception:
        return {}
    if not version or _version_tuple(version) < _CLAUDE_MIN_VERSION:
        return {}
    return {
        "user-agent": f"claude-code/{version} (external, cli)",
        "x-app": "cli",
    }


def supports(platform: str) -> bool:
    """Hermes natively covers codex and claude; there is no antigravity client
    in the core at all, so that platform falls through to the next profile."""
    if platform == "codex":
        try:
            from agent.codex_headers import codex_cloudflare_headers  # noqa: F401
        except Exception:
            return False
        return True
    if platform == "claude":
        return bool(_claude_identity())
    return False


def apply(request: Any, *, platform: str, account: dict, phase: str,
          access_token: str = "") -> None:
    headers = request.headers
    if platform == "codex":
        for name, value in _codex_identity(access_token or "").items():
            if not str(headers.get(name) or "").strip():
                try:
                    headers[name] = value
                except Exception:
                    return
    elif platform == "claude":
        # Core's own values win over our placeholder UA; nothing else is set.
        for name, value in _claude_identity().items():
            try:
                headers[name] = value
            except Exception:
                return
