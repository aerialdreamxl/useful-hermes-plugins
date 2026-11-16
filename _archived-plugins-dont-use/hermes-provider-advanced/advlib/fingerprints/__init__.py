"""Request-identity fingerprints (v3 R6) — one .py per profile, removable.

A fingerprint rewrites the OUTBOUND IDENTITY of requests made with an OAuth
account (User-Agent / originator / version / account headers). It never
touches payload semantics or protocol choice — that is the platform adapter.

Profiles self-register by living in this package; the loader falls back to
``hermes`` (WARN once) when the requested profile file is absent — the
delisting path.

Contract (module level):
    NAME = "hermes"
    def apply(request, *, platform: str, account: dict, phase: str) -> None
        request: live httpx.Request — mutate request.headers in place
        phase:   "inference" | "auth" | "other" (URL-derived by oauth_client)
        Never raise: identity polish must not kill a request; on internal
        error return silently (caller already logged construction once).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("hermes.advanced_providers")

_PROFILES: "dict[str, Any]" = {}


def load(name: str):
    """Fingerprint module for *name*, or None (caller falls back to hermes)."""
    key = str(name or "hermes").strip().lower() or "hermes"
    if key in _PROFILES:
        return _PROFILES[key]
    import importlib

    try:
        mod = importlib.import_module(f"{__name__}.{key}")
    except Exception:
        return None
    if getattr(mod, "NAME", key) != key or not hasattr(mod, "apply"):
        return None
    _PROFILES[key] = mod
    return mod


def available() -> list[str]:
    import importlib
    import pkgutil

    out = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
            if hasattr(mod, "apply"):
                out.append(info.name)
        except Exception:
            continue
    return out


def _covers(mod: Any, platform: str) -> bool:
    """Does profile *mod* define an identity for *platform*?

    Profiles answer with ``supports(platform)`` (dynamic — it can probe whether
    the vendored source it imitates still provides the platform) or a static
    ``SUPPORTS`` tuple. Unknown shape = assume yes (never silently disable a
    profile the user asked for).
    """
    probe = getattr(mod, "supports", None)
    if callable(probe):
        try:
            return bool(probe(platform))
        except Exception:
            return False
    declared = getattr(mod, "SUPPORTS", None)
    if declared is not None:
        try:
            return platform in declared
        except Exception:
            return False
    return True


#: Fallback order for platforms a profile does not cover. ``hermes`` first:
#: prefer the host's own identity, then the CPA port, then Sub2API, then error.
FALLBACK_ORDER = ("hermes", "cpa", "sub2api")


def resolve(platform: str, name: str = "hermes") -> tuple[Any, str, list[str]]:
    """Resolve the profile that will actually supply *platform*'s identity.

    Returns ``(module, effective_name, skipped)``; ``module is None`` means no
    loaded profile covers the platform (the caller reports an error).

    Requested profile is tried first, then ``FALLBACK_ORDER``. A profile that
    is missing on disk or declares no identity for the platform is skipped —
    that is the whole point of the chain: ``hermes`` has no antigravity
    identity, so it must land on ``cpa`` rather than do nothing.
    """
    key = str(name or "hermes").strip().lower() or "hermes"
    chain = [key] + [n for n in FALLBACK_ORDER if n != key]
    skipped: "list[str]" = []
    for candidate in chain:
        mod = load(candidate)
        if mod is None:
            skipped.append(f"{candidate}:missing")
            continue
        if not _covers(mod, platform):
            skipped.append(f"{candidate}:no-identity")
            continue
        return mod, candidate, skipped
    return None, "", skipped


_FALLBACK_LOGGED: "set[tuple[str, str]]" = set()
_NO_IDENTITY_LOGGED: "set[tuple[str, str]]" = set()


def describe(platform: str, name: str = "hermes") -> dict:
    """Diagnostics for one (platform, fingerprint) pair — used by tests/CLI."""
    mod, effective, skipped = resolve(platform, name)
    return {"platform": platform, "requested": str(name or "hermes"),
            "effective": effective or None, "skipped": skipped,
            "ok": mod is not None}


def apply(request: Any, *, platform: str, account: dict, phase: str, name: str,
          access_token: str = "") -> None:
    """Apply the resolved fingerprint. Never raises.

    Falls back per :func:`resolve`; reports a visible ERROR when nothing
    covers the platform instead of pretending the identity was applied.
    """
    mod, effective, skipped = resolve(platform, name)
    requested = str(name or "hermes").strip().lower() or "hermes"
    if mod is None:
        pair = (platform, requested)
        if pair not in _NO_IDENTITY_LOGGED:
            _NO_IDENTITY_LOGGED.add(pair)
            logger.error(
                "advanced-providers: no OAuth fingerprint covers platform %r "
                "(fingerprint=%s, tried=%s) — request identity left unchanged",
                platform, requested, ", ".join(skipped) or "none")
        return "", skipped
    if effective != requested:
        pair = (platform, requested)
        if pair not in _FALLBACK_LOGGED:
            _FALLBACK_LOGGED.add(pair)
            logger.warning(
                "advanced-providers: fingerprint %s has no %s identity; "
                "using %s instead (tried=%s)", requested, platform, effective,
                ", ".join(skipped))
    try:
        mod.apply(request, platform=platform, account=account, phase=phase,
                  access_token=access_token)
    except Exception:
        logger.debug("advanced-providers: fingerprint %s apply failed (ignored)", effective,
                     exc_info=True)
    return effective, skipped
