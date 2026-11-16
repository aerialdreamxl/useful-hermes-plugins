"""OAuth account token plumbing — refresh + store write-back (v3 §3.2).

The account store holds LONG-lived material (usually a refresh_token); every
request needs a fresh short-lived access token. Rather than letting adapters
hand out raw dicts, this module owns the shared discipline:

- expiry decision (parse the stored ``expired``/expires_at shape OR refresh
  pre-emptively when unknown),
- refresh via the CORE's own pure refresher per platform
  (``refresh_codex_oauth_pure`` / ``refresh_anthropic_oauth_pure`` — never a
  re-implementation of someone else's OAuth),
- atomic store write-back through AccountStore.mutate + the inter-process
  lock (single-use refresh rotation is persisted BEFORE the new token is
  handed out — losing it would orphan the grant),
- peer-rotation adoption: if our stored refresh was already rotated by
  another writer (CLI/codex CLI), adopt the stored pair instead of replaying
  a spent token (credential_pool SINGLE_USE semantics, ported).

PROXY NOTE (v3 §2 R3): refreshers should route through the entry proxy when
configured. Both core refresher families accept that discipline only via
their own env plumbing; porting that properly is tracked as a P3 hardening
todo — current behaviour: refresh uses the process env proxy policy.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes.advanced_providers")

# Refresh this many seconds BEFORE the known expiry (codex core uses 120s).
REFRESH_SKEW_SECONDS = 120


def parse_expiry(raw: Any) -> Optional[float]:
    """Unix timestamp (seconds) from the several shapes accounts carry, or None.

    Accepted: epoch seconds / epoch millis (``expires_at_ms`` style already
    normalized upstream), ISO strings, CPA's ``expired`` free-form date. None
    means "unknown" — caller refreshes conservatively.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        return ts / 1000.0 if ts > 1e12 else ts
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        ts = float(text)
        return ts / 1000.0 if ts > 1e12 else ts
    try:
        iso = text.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def needs_refresh(acct: dict, *, now: Optional[float] = None) -> bool:
    """True when the stored access token is missing or near/after expiry."""
    import time

    now = now if now is not None else time.time()
    if not str(acct.get("access_token") or "").strip():
        return True
    exp = parse_expiry(acct.get("expired") or acct.get("expires_at") or acct.get("expires_at_ms"))
    if exp is None:
        # Unknown expiry: jwt exp wins if decodable; else refresh (conservative).
        exp = _jwt_expiry(str(acct.get("access_token") or ""))
        if exp is None:
            return True
    return exp - REFRESH_SKEW_SECONDS <= now


def _jwt_expiry(token: str) -> Optional[float]:
    """``exp`` claim of a JWT, no signature check, or None."""
    import base64
    import json

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def refresh_and_persist(platform: str, account_name: str, store, *,
                        proxy_hint: str = "") -> Optional[dict]:
    """Refresh the account's tokens via the platform adapter; persist rotated
    material atomically; return the FRESH account dict (or None on failure).

    Lock discipline (v3): refreshers are NETWORK calls — the process lock is
    never held across one. Read, refresh (unlocked), then an optimistic
    read-modify-write inside ``store.mutate``: if the stored refresh token
    changed between our read and the write, a peer (CLI / codex CLI / another
    agent) rotated the single-use grant — we ADOPT theirs instead of replaying
    a spent token (credential_pool SINGLE_USE semantics, ported).

    In-process discipline (offline audit P1): the store CAS only defends
    against OTHER processes. Two threads in THIS process (parallel turns,
    aux + main client) can read the same unexpired RT and both fire a refresh
    upstream — the second one replays a token the issuer just destroyed, and
    claude/openai treat that as a revocable offense. ``oauth_gate.single_flight``
    (CPA's errgroup/singleflight semantics) serializes per (platform, account):
    one refresh in flight, everyone else waits for its outcome.
    """
    from . import oauth_gate
    from .account_store import AccountStoreError, _SKIP

    if oauth_gate.oauth_disabled():
        logger.warning("advanced-providers: refresh refused — %s", oauth_gate.DISABLED_MSG)
        return None

    refresher = _platform_refresher(platform)
    if refresher is None:
        logger.warning("advanced-providers: no refresher for platform %r", platform)
        return None

    acct = store.get(platform, account_name)
    if not isinstance(acct, dict) or not acct:
        return None
    rt = str(acct.get("refresh_token") or "").strip()
    if not rt:
        logger.info("advanced-providers: account %s:%s has no refresh_token — re-login needed",
                    platform, account_name)
        return None

    def _settle_update(updated: dict) -> Optional[dict]:
        """CAS-persist *updated*; return the account the store ended on.

        Runs INSIDE the flight: the token endpoint consumed the single-use RT
        the moment the refresher returned, so persisting must be part of the
        same serialized unit — a late thread that finds the flight already
        settled must never be able to re-run the refresher with the same RT.
        """
        adopted: list[dict] = []

        def _merge(data: dict) -> Any:
            bucket = data.setdefault(platform, {})
            base = dict(bucket.get(account_name) or {})
            stored_rt = str(base.get("refresh_token") or "")
            if stored_rt and stored_rt != rt:
                # peer rotated between read and write — keep theirs, persist nothing
                adopted.append(base)
                return _SKIP
            base.update(updated)
            base["last_refresh"] = _now_iso()
            bucket[account_name] = base
            return base

        try:
            result = store.mutate(_merge)
        except AccountStoreError as exc:
            logger.warning("advanced-providers: refresh persist blocked: %s", exc)
            return None
        if adopted:
            logger.info("advanced-providers: refresh token rotated by a peer during refresh — adopting")
            return adopted[0]
        from . import oplog

        oplog.refresh_ok(platform=platform, account=account_name, proxy=proxy_hint)
        return result if isinstance(result, dict) else updated

    def _settled(_ran: Any = None) -> Optional[dict]:
        # "Did someone already rotate this token?" Checked under the flight
        # lock for the would-be leader (double-checked pattern) and used to
        # short-circuit late joiners whose flight already settled.
        try:
            cur = store.get(platform, account_name)
        except Exception:
            return None
        if isinstance(cur, dict):
            stored_rt = str(cur.get("refresh_token") or "")
            if stored_rt and stored_rt != rt:
                return cur
        return None

    def _refresh_and_persist() -> Optional[dict]:
        return _settle_update(_call_refresher(refresher, acct, rt, proxy_hint))

    try:
        fresh = oauth_gate.single_flight((platform, account_name), _refresh_and_persist,
                                         adopt=_settled)
    except Exception as exc:
        # A spent-grant error usually means a peer rotated it: adopt if the
        # store now holds a different refresh token than we used.
        adopted_late = _settled()
        if adopted_late is not None:
            logger.info("advanced-providers: refresh token rotated by a peer — adopting stored pair")
            return adopted_late
        logger.warning("advanced-providers: refresh failed for %s:%s (%s: %s)",
                       platform, account_name, type(exc).__name__, exc)
        return None

    return fresh


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call_refresher(refresher, acct: dict, rt: str, proxy: str) -> dict:
    """Call a refresher with the proxy hint when it takes one.

    Our own refreshers (antigravity) accept ``proxy=``; the core's pure
    refreshers are called with their original signature (they own their
    transport and read HTTP(S)_PROXY from the environment).
    """
    try:
        import inspect

        if "proxy" in inspect.signature(refresher).parameters:
            return refresher(acct, rt, proxy=proxy)
    except (TypeError, ValueError):
        pass
    return refresher(acct, rt)


def _platform_refresher(platform: str) -> Optional[Callable[..., dict]]:
    """(acct, rt, proxy=...) -> partial acct update {access_token, refresh_token, ...}.

    Dispatch is the adapter's own ``refresh()`` for EVERY platform (offline
    audit §2.3): the adapters are the layer that knows its issuer's exact
    wire shape and can take a proxy. The core's pure refreshers stay ONLY as
    a fallback for an adapter that ships no refresh() — they cannot honour
    ``advanced.proxy``, so the fallback path logs that limit once.
    """
    try:
        from .adapters import get as get_adapter

        ad = get_adapter(platform)
        fn = getattr(ad, "refresh", None) if ad is not None else None
        if callable(fn):
            return fn
    except Exception:
        logger.debug("advanced-providers: adapter refresh lookup failed for %r",
                     platform, exc_info=True)
    return _core_refresher(platform)


_CORE_FALLBACK_LOGGED: "set[str]" = set()


def _core_refresher(platform: str) -> Optional[Callable[[dict, str], dict]]:
    """Legacy core-pure refreshers (proxy-blind) — fallback only.

    codex: ``refresh_codex_oauth_pure`` (single-use rotation handled there).
    claude: ``refresh_anthropic_oauth_pure``.
    """
    def _warn_no_proxy() -> None:
        if platform not in _CORE_FALLBACK_LOGGED:
            _CORE_FALLBACK_LOGGED.add(platform)
            logger.warning("advanced-providers: platform %r has no adapter refresher; "
                           "using the core pure refresher — it ignores advanced.proxy "
                           "(refresh may time out on a proxy-only network)", platform)

    if platform == "codex":
        def _ref(acct: dict, rt: str) -> dict:
            from hermes_cli.auth_codex import refresh_codex_oauth_pure

            _warn_no_proxy()
            out = refresh_codex_oauth_pure(str(acct.get("access_token") or ""), rt)
            upd = {"access_token": out.get("access_token", ""),
                   "refresh_token": out.get("refresh_token", rt)}
            exp = _jwt_expiry(upd["access_token"])
            if exp:
                upd["expired"] = str(int(exp))
            return upd
        return _ref
    if platform in ("claude", "anthropic"):
        def _ref(acct: dict, rt: str) -> dict:
            from agent.anthropic_credentials import refresh_anthropic_oauth_pure

            _warn_no_proxy()
            out = refresh_anthropic_oauth_pure(rt)
            upd = {"access_token": out.get("access_token", ""),
                   "refresh_token": out.get("refresh_token", rt)}
            exp_ms = out.get("expires_at_ms")
            if exp_ms:
                upd["expired"] = str(int(int(exp_ms) / 1000))
            return upd
        return _ref
    return None
