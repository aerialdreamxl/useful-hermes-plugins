"""LIVE smoke: antigravity facade against the real cloudcode-pa upstream.

Not part of the offline suite (it spends real quota). Run it by hand:

    export HERMES_HOME=<your hermes home>
    export HTTPS_PROXY=<proxy url> HTTP_PROXY=<proxy url>
    <venv python> tests/live_antigravity_smoke.py [account] [fingerprint]

It exercises what the 2026-10-29 fixes changed, end to end:
  1. gemini non-stream           (generateContent)
  2. close() then reuse          (the no-op-close self-heal that stopped the
                                  "client has been closed" fallback to Qwen)
  3. claude route                (streamGenerateContent merged to non-stream)
  4. tool roundtrip              (schema cleaning via the ported engine)
  5. $ref/union-heavy schema     (the 400 that motivated the engine port)

Prints only PASS/FAIL lines and message excerpts — never a token.
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from advlib.account_store import AccountStore  # noqa: E402
from advlib.adapters import get as get_adapter  # noqa: E402

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else ""
FINGERPRINT = sys.argv[2] if len(sys.argv) > 2 else "cpa"
PROXY = os.environ.get("HTTPS_PROXY", "")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", ""))


def say(ok: bool, label: str, extra: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    return ok


def main() -> int:
    if not str(HERMES_HOME) or ACCOUNT == "":
        # these defaults used to be one developer machine's paths; refuse to guess
        print("[FAIL] pass <account> arg and set HERMES_HOME/HTTPS_PROXY")
        return 2
    store = AccountStore(HERMES_HOME / ".advanced-custom-providers.json")
    if store.get("antigravity", ACCOUNT) is None:
        print(f"[FAIL] no stored antigravity account {ACCOUNT!r}")
        return 2
    adapter = get_adapter("antigravity")
    harness = {
        "model": "gemini-3-flash",
        "request_timeout": 300,
        "proxy": PROXY,
    }
    client = adapter.build_openai_client(harness, ACCOUNT, store,
                                         fingerprint=FINGERPRINT, proxy=PROXY)
    print(f"facade={type(client).__name__} fingerprint={FINGERPRINT} proxy={PROXY}")
    results = []

    # 1. gemini non-stream
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model="gemini-3-flash", max_tokens=32,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}])
        text = (r.choices[0].message.content or "").strip()
        results.append(say("OK" in text.upper(), "gemini-3-flash non-stream",
                           f"{text[:60]!r} in {time.time()-t0:.1f}s"))
    except Exception as exc:
        results.append(say(False, "gemini-3-flash non-stream", f"{type(exc).__name__}: {exc}"))

    # 2. close() must be harmless; the same object keeps working (self-heal)
    try:
        client.close()
        r = client.chat.completions.create(
            model="gemini-3-flash", max_tokens=16,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}])
        text = (r.choices[0].message.content or "").strip()
        results.append(say("OK" in text.upper(), "reuse after core close()",
                           f"is_closed={client.is_closed()} {text[:40]!r}"))
    except Exception as exc:
        results.append(say(False, "reuse after core close()", f"{type(exc).__name__}: {exc}"))

    # 3. claude route (stream upstream, merged to non-stream)
    try:
        r = client.chat.completions.create(
            model="claude-sonnet-4-6", max_tokens=32,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}])
        text = (r.choices[0].message.content or "").strip()
        results.append(say(bool(text), "claude-sonnet-4-6 merged stream", f"{text[:60]!r}"))
    except Exception as exc:
        results.append(say(False, "claude-sonnet-4-6 merged stream",
                           f"{type(exc).__name__}: {exc}"))

    # 4. tool roundtrip
    tools = [{"type": "function", "function": {
        "name": "calculator", "description": "Evaluate a simple arithmetic expression",
        "parameters": {"type": "object",
                       "properties": {"expression": {"type": "string",
                                                     "description": "e.g. 2+2"}},
                       "required": ["expression"]}}}]
    try:
        r = client.chat.completions.create(
            model="gemini-3-flash", max_tokens=64, tools=tools, tool_choice="auto",
            messages=[{"role": "user",
                       "content": "Use the calculator tool for 2+2, then answer."}])
        msg = r.choices[0].message
        called = bool(getattr(msg, "tool_calls", None))
        results.append(say(called, "tool roundtrip (gemini)",
                           f"tool={msg.tool_calls[0].function.name if called else '-'}"))
    except Exception as exc:
        results.append(say(False, "tool roundtrip (gemini)", f"{type(exc).__name__}: {exc}"))

    # 5. ref/union-heavy schema through the ported engine (was a 400)
    hard = [{"type": "function", "function": {
        "name": "locate", "description": "Find a path",
        "parameters": {
            "type": "object", "required": ["path"],
            "properties": {
                "path": {"$ref": "#/$defs/Path"},
                "mode": {"type": ["string", "null"], "enum": ["fast", "deep"]},
                "where": {"anyOf": [{"type": "object", "properties": {"root": {"type": "string"}}},
                                    {"type": "object", "properties": {"base": {"type": "string"}}}]},
            },
            "$defs": {"Path": {"type": "string", "description": "filesystem path"}}}}},
        # a second empty-properties tool: the placeholder path
        {"type": "function", "function": {
            "name": "now", "description": "Current time",
            "parameters": {"type": "object", "properties": {}}}}]
    for model in ("gemini-3-flash", "claude-sonnet-4-6"):
        try:
            r = client.chat.completions.create(
                model=model, max_tokens=64, tools=hard, tool_choice="auto",
                messages=[{"role": "user",
                           "content": "Call the now tool, then locate /tmp."}])
            msg = r.choices[0].message
            called = bool(getattr(msg, "tool_calls", None))
            # The PASS criterion is the WIRE: a 400 here is what the schema
            # engine exists to prevent. Which tool the model picks is up to it.
            detail = (f"tool={msg.tool_calls[0].function.name}" if called
                      else f"no tool call (finish={r.choices[0].finish_reason})")
            results.append(say(True, f"$ref/union/placeholder schema accepted ({model})", detail))
        except Exception as exc:
            results.append(say(False, f"$ref/union/placeholder schema accepted ({model})",
                               f"{type(exc).__name__}: {exc}"))

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
