"""OAuth kill switch + in-process refresh single-flight.

ADVANCED_DISABLE_OAUTH=1
    Everything OAuth stops: the transport dialect is not registered, scheme
    refs (``codex://…``) are refused with a clear message instead of being
    silently handed to another client, model listing / refresh / login all
    dead-end before a request. Proxy routing (P1) keeps working — it does not
    need OAuth. This is the switch for "the account layer is the suspect,
    rule it out without uninstalling the plugin".

single_flight()
    Claude/Codex refresh tokens are single-use rotated. The account-store CAS
    in oauth_tokens only defends against OTHER processes; two threads in this
    process holding the same unexpired RT would both refresh upstream, and the
    second request replays a token the issuer already destroyed — CPA's
    singleflight (errgroup.Do keyed by refresh token) exists precisely to
    serialize that. Ported here as a per-(platform, account) leader/wait
    primitive; the native proxy-aware refreshers live in the platform
    adapters (claude/codex/antigravity ``refresh()``).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes.advanced_providers")

_TRUTHY = ("1", "true", "yes", "on")


def oauth_disabled() -> bool:
    """True when ADVANCED_DISABLE_OAUTH is set to a truthy value."""
    return str(os.environ.get("ADVANCED_DISABLE_OAUTH", "")).strip().lower() in _TRUTHY


def oauth_enabled() -> bool:
    return not oauth_disabled()


#: Single source for the refusal message — a dead feature must say why.
DISABLED_MSG = ("advanced_oauth is disabled by ADVANCED_DISABLE_OAUTH — unset it "
                "to restore OAuth accounts (proxy routing stays unaffected)")


def refuse_oauth(what: str = "OAuth") -> None:
    """Raise the standard refusal (callers are OUR paths only)."""
    raise RuntimeError(f"advanced-providers: {what} refused: {DISABLED_MSG}")


# ---------------------------------------------------------------- in-flight dedup
# Single-use rotated refresh tokens make concurrency a credential-safety
# issue, not a performance one: two threads in this process holding the same
# unexpired RT both "refreshing" it means the second arrives with a token the
# issuer already destroyed — CPA treats that as a replay attack and revokes
# the whole grant (account dead until a full re-login). Go's singleflight is
# the reference behavior; this is the same shape in threads:
#   first caller per key runs the network call; everyone else waits and
#   reuses its result (or its failure) rather than firing a second refresh.
# Nested refresh (a waiter whose own call re-enters token()) is handled the
# same way as CPA's: it waits, then adopts the settled result from the store
# — the caller in oauth_tokens re-reads and short-circuits on a rotated RT.

class _Flight:
    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None


_IN_FLIGHT: "dict[Any, _Flight]" = {}
#: RLock: the leader may re-enter single_flight on the same thread (a refresher
#: that itself triggers token() -> refresh). Keying the map is only ever a
#: dict lookup; holding it across an event.wait NEVER happens (waits outside).
_IN_FLIGHT_LOCK = threading.RLock()
_WAIT_TIMEOUT = 120.0


def single_flight(key: Any, fn: Callable[[], Any], *,
                  adopt: Optional[Callable[[Any], Any]] = None,
                  wait_timeout: float = _WAIT_TIMEOUT) -> Any:
    """Run *fn* at most once per *key* across concurrent callers; every caller
    gets the first run's outcome (errors included, so one failed refresh does
    not trigger a retry stampede against a possibly-spent token).

    *adopt* is CPA's settle-then-share refinement: a waiter whose flight
    finished long ago (slot already gone) calls ``adopt(result_if_ran)`` —
    the caller supplies it because only it knows how to check "did someone
    already do the work" (re-read the store, compare the token). Without it
    the waiter would fire a second replay of the same refresh token.

    The check-and-register happens UNDER the module lock: the leader re-runs
    *adopt* after claiming the slot (the classic double-checked pattern), so
    two threads can never both become leaders of the same key.
    """
    with _IN_FLIGHT_LOCK:
        flight = _IN_FLIGHT.get(key)
        if flight is not None:  # someone else is already refreshing: wait
            waiter = True
        else:
            settled = adopt(None) if adopt is not None else None
            if settled is not None:
                return settled  # already done between our read and here
            flight = _Flight()
            _IN_FLIGHT[key] = flight
            waiter = False
    if waiter:
        finished = flight.event.wait(timeout=wait_timeout)
        if not finished:
            # Leader wedged (should not happen: network calls carry their own
            # timeouts). Never trade one hung refresh for a forever-hung turn,
            # and never fire a second one either — bail out loudly.
            raise TimeoutError(f"advanced-providers: in-flight operation for {key!r} timed out")
        if flight.error is not None:
            raise flight.error
        return flight.value
    try:
        flight.value = fn()
    except BaseException as exc:  # noqa: BLE001 — shared with waiters on purpose
        flight.error = exc
        raise
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.pop(key, None)
        flight.event.set()
    return flight.value
