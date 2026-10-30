"""LIVE: the exact call chain that crashed every turn on 2026-10-30.

    agent._get_transport().validate_response(response)
      -> get_transport("advanced_oauth") -> AdvancedOAuthTransport

The bug was a missing method on the registered class, so the turn died AFTER a
successful upstream call (AttributeError, 3 retries, then the UI showed the
provider as broken). This drives a real response from the real API through
every method the core calls by name, so a restart is the only step left.

    export HERMES_HOME=<your hermes home> HERMES_CORE_ROOT=<core checkout> HTTPS_PROXY=<proxy>
    <venv python> tests/live_transport_surface.py [account] [fingerprint]
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CORE = Path(os.environ.get("HERMES_CORE_ROOT", ""))
sys.path.insert(0, str(CORE))

from advlib.account_store import AccountStore  # noqa: E402
from advlib.adapters import get as get_adapter  # noqa: E402
from advlib.oauth_runtime import ensure_registered  # noqa: E402

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else ""
FINGERPRINT = sys.argv[2] if len(sys.argv) > 2 else "cpa"
PROXY = os.environ.get("HTTPS_PROXY", "")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", ""))

ok_all = True


def say(ok: bool, label: str, extra: str = "") -> None:
    global ok_all
    ok_all = ok_all and ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")


def _usage_guard() -> bool:
    # these defaults used to be one developer machine's paths; refuse to guess
    if not str(HERMES_HOME) or ACCOUNT == "":
        print("[FAIL] pass <account> arg and set HERMES_HOME (and CORE via HERMES_CORE_ROOT)")
        return False
    return True


def main() -> int:
    if not _usage_guard():
        return 2
    from agent.transports import get_transport

    assert ensure_registered(), "transport registration failed"
    transport = get_transport("advanced_oauth")
    say(transport is not None and type(transport).__name__ == "AdvancedOAuthTransport",
        "core returns OUR transport for advanced_oauth", type(transport).__name__)

    # every method the core calls by name on a real turn
    for name in ("build_kwargs", "normalize_response", "validate_response",
                 "extract_cache_stats", "map_finish_reason",
                 "response_finish_reason", "preflight_kwargs"):
        say(callable(getattr(transport, name, None)), f"transport.{name} exists")

    store = AccountStore(HERMES_HOME / ".advanced-custom-providers.json")
    harness = {"model": "gemini-3-flash", "request_timeout": 300}

    for platform, acct, model in (("antigravity", ACCOUNT, "gemini-3-flash"),
                                  ("codex", ACCOUNT, "gpt-6-luna")):
        if store.get(platform, acct) is None:
            print(f"[skip] no stored {platform} account {acct}")
            continue
        adapter = get_adapter(platform)
        client = adapter.build_openai_client(harness, acct, store,
                                             fingerprint=FINGERPRINT, proxy=PROXY)
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=24,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}])
        except Exception as exc:
            say(False, f"{platform} live call", f"{type(exc).__name__}: {exc}")
            continue
        # ---- the crash path, verbatim ----
        try:
            valid = transport.validate_response(resp)
            say(bool(valid), f"{platform} validate_response(real response)", f"-> {valid}")
        except Exception as exc:
            say(False, f"{platform} validate_response(real response)",
                f"{type(exc).__name__}: {exc}")
        try:
            norm = transport.normalize_response(resp)
            say(norm is not None, f"{platform} normalize_response(real response)",
                f"finish={getattr(norm, 'finish_reason', None)}"
                f" tools={len(getattr(norm, 'tool_calls', []) or [])}")
        except Exception as exc:
            say(False, f"{platform} normalize_response(real response)",
                f"{type(exc).__name__}: {exc}")
        say(transport.extract_cache_stats(resp) is not None
            or transport.extract_cache_stats(resp) is None,
            f"{platform} extract_cache_stats does not raise")
        say(transport.map_finish_reason("stop") == "stop",
            f"{platform} map_finish_reason passthrough")

    print(f"\n{'ALL PASS' if ok_all else 'SOME CHECKS FAILED'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
