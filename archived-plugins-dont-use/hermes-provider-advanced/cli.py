"""``hermes advanced-providers`` CLI (v2 spec §4, N3).

P1 verbs:
- ``status``   — every ``advanced:`` declaration: parsed fields, matched
                 route/oauth key, proxy reachability probe, overlay liveness.
- ``selfcheck``— N1 upgrade-resilience probes: overlay still resolves as
                 ``custom``, the ``http_client`` gate still exists,
                 ``advanced`` still an unknown key (i.e. core hasn't adopted
                 it yet — adoption means the plugin degrades to a shim).

P3 adds ``oauth login/list/signout``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

try:  # package context (deployed as hermes_plugins.<slug>.cli)
    from . import CLI_NAME
except Exception:  # standalone import (tests) — mirror the constant
    CLI_NAME = "advanced-providers"


def _adv(mod: str):
    """Import an advlib submodule across both package contexts.

    Deployed: cli runs as ``hermes_plugins.<slug>.cli`` → relative import
    resolves ``.advlib.<mod>`` under the plugin package.
    Tests: cli imported top-level → no package; fall back to ``advlib.<mod>``
    (the repo root sits on sys.path in that context).
    """
    import importlib

    dotted = f"advlib.{mod}" if mod else "advlib"
    if __package__:
        try:
            return importlib.import_module(f".{dotted}", package=__package__)
        except ImportError:
            pass
    return importlib.import_module(dotted)


def build_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="adv_verb")

    sub.add_parser("status", help="List every providers-advanced entry, its route match and proxy probe.")
    sub.add_parser("selfcheck", help="N1 probes: standalone profile registration, http_client gate, key adoption.")
    sub.add_parser("help", help="Show this help.")

    # P3: account-store verbs.
    oauth = sub.add_parser("oauth", help="account-store verbs: login/list/import/signout")
    oauth_sub = oauth.add_subparsers(dest="adv_oauth_verb")
    oauth_sub.add_parser("list", help="list stored OAuth accounts (tokens never printed)")
    signout = oauth_sub.add_parser("signout", help="delete an account credential")
    signout.add_argument("--platform", default="")
    signout.add_argument("--account", default="")
    login = oauth_sub.add_parser("login", help="interactive login (platform adapter's own flow)")
    login.add_argument("--platform", default="")
    login.add_argument("--name", default="", help="store slot name (default: derived from credential)")
    login.add_argument("--proxy", default="")
    login.add_argument("--json", dest="json_src", default="", help="import instead of interactive login")
    login.add_argument("--json-format", dest="json_format", default="", choices=["", "sub2api", "cpa"])
    imp = oauth_sub.add_parser("import", help="import accounts from JSON (sub2api rows / CPA records / bare)")
    imp.add_argument("--file", required=False, default="", help="path or inline JSON")
    imp.add_argument("--platform", default="", help="filter which platform's candidates to store")
    imp.add_argument("--name", default="", help="override the store slot name (single-account sources)")
    imp.add_argument("--force", action="store_true", help="overwrite existing slots")
    refresh = oauth_sub.add_parser(
        "refresh", help="rotate an account's at/rt NOW (positional: platform://account "
                        "or bare account name with --platform)")
    refresh.add_argument("target", help="'antigravity://me@x.com' or 'me@x.com' with --platform")
    refresh.add_argument("--platform", default="")
    refresh.add_argument("--proxy", default="",
                         help="override the entry's advanced.proxy for this refresh call")
    models = oauth_sub.add_parser(
        "models", help="list what the ACCOUNT's upstream catalog serves (CPA-style, per-account)")
    models.add_argument("target", nargs="?", default="",
                        help="'platform://account' (or --platform/--account separately)")
    models.add_argument("--platform", default="")
    models.add_argument("--account", default="")
    models.add_argument("--refresh", action="store_true", help="bypass the 15-min catalog cache")


def run_command(args: argparse.Namespace) -> int:
    verb = getattr(args, "adv_verb", "") or "help"
    if verb == "help":
        _print_help()
        return 0
    if verb == "status":
        return _cmd_status()
    if verb == "selfcheck":
        return _cmd_selfcheck()
    if verb == "oauth":
        return _cmd_oauth(args)
    _print_help()
    return 2


def _print_help() -> None:
    print(f"hermes {CLI_NAME} — per-provider advanced: settings\n")
    print("  status     parsed providers-advanced: entries, route match, proxy probe, profile status")
    print("  selfcheck  N1 upgrade-resilience probes")
    print("  oauth      account store: login / import / list / signout\n")
    print("Config shape (under top-level `providers-advanced:`):")
    print("providers-advanced:")
    print("  my-proxy-model:")
    print('    name: "My DeepSeek Proxy"   # registered display name: [ADVANCED] My DeepSeek Proxy')
    print('    base_url: "https://api.deepseek.com/v1"')
    print('    advanced:')
    print('      proxy: "http://127.0.0.1:7890"')
    print("  # OAuth dialect (P3):")
    print("  my-codex-account:")
    print('    name: "[OAUTH] ChatGPT Codex"')
    print('    base_url: "codex://me@example.com"   # <platform>://<account>')
    print('    api_mode: advanced_oauth')
    print("    advanced:")
    print("      oauth_fingerprint: hermes|sub2api|cpa   # request identity (default hermes)")


def _print_oauth_help() -> None:
    print("hermes advanced-providers oauth <verb>")
    print("  login --platform codex|claude|antigravity   interactive browser login")
    print("  import --file dump.json           store accounts from sub2api/CPA/bare JSON")
    print("  list                              show slots + expiry (tokens never printed)")
    print("  signout --platform <p> --account <a>")


def _advlib():
    """The advlib package (dual package-context import — the CLI half is
    diagnostic-only; sharing one instance with the overlay's
    ``hermes_advlib`` is not required because the route table is a read-only
    mtime-keyed cache)."""
    try:
        return _adv("") or None
    except Exception:
        return None


def _cmd_status() -> int:
    config_scan = _adv("config_scan")
    proxy_factory = _adv("proxy_factory")
    probe_proxy = proxy_factory.probe_proxy
    RouteTable = config_scan.RouteTable

    advlib = _advlib()
    if advlib is None:
        print("[FAIL] advlib not loadable — see selfcheck output")
        return 1

    table = RouteTable.from_config()

    profile_ok, profile_detail = _probe_provider_profile()
    print(f"profile:   {'OK' if profile_ok else 'NOT ACTIVE'} — {profile_detail}")
    print(f"entries:   {len(table.all_entries)} providers-advanced entry(ies) parsed")

    if not table.all_entries and not table.warnings:
        print("\nNo entries in providers-advanced:. Native providers remain completely untouched.")
        return 0

    print()
    for entry in table.all_entries:
        mode = entry.auth_mode
        print(f"- {entry.display_name} ({entry.slug}) [{mode}]")
        if mode == "api":
            print(f"    route:   {entry.route_url}")
            if entry.proxy:
                ok, detail = probe_proxy(entry.proxy)
                print(f"    proxy:   {_redact(entry.proxy)} -> {'REACHABLE' if ok else 'UNREACHABLE'} ({detail})")
            else:
                print("    proxy:   (none)")
        else:
            print(f"    account: {entry.oauth_platform}:{entry.oauth_account or '<unset>'}  [P3 runtime]")
            if entry.proxy:
                ok, detail = probe_proxy(entry.proxy)
                print(f"    proxy:   {_redact(entry.proxy)} -> {'REACHABLE' if ok else 'UNREACHABLE'} ({detail})")

    if table.warnings:
        print("\nwarnings:")
        for w in table.warnings:
            print(f"  ! {w}")
    return 0


def _cmd_selfcheck() -> int:
    """N1: fail loudly if the injection surface drifted under us."""
    failures = 0

    # 1. advlib loadable
    advlib = _advlib()
    print(f"[{'OK' if advlib else 'FAIL'}] advlib loads under fixed module name")
    failures += 0 if advlib else 1

    # 2. standalone 'advanced' profile is registered independently
    profile_ok, detail = _probe_provider_profile()
    print(f"[{'OK' if profile_ok else 'FAIL'}] standalone 'advanced' profile — {detail}")
    failures += 0 if profile_ok else 1

    # 3. http_client gate still in core (semantic anchor, text probe)
    gate_ok, gate_detail = _probe_gate()
    print(f"[{'OK' if gate_ok else 'FAIL'}] core http_client gate — {gate_detail}")
    failures += 0 if gate_ok else 1

    # 4. 'advanced' still unknown to the core key table (shim signal)
    adopted = _probe_key_adoption()
    print(f"[{'INFO' if not adopted else 'NOTE'}] core _KNOWN_PROVIDER_KEYS adoption: "
          f"{'advanced NOW KNOWN — plugin can degrade to shim' if adopted else 'advanced still custom (plugin owns semantics)'}")

    # 5. hook signature still receives base_url
    sig_ok, sig_detail = _probe_hook_signature()
    print(f"[{'OK' if sig_ok else 'FAIL'}] build_client_kwargs_extras(base_url=...) — {sig_detail}")
    failures += 0 if sig_ok else 1

    # 6. transport seams (P2 proxy + P3.6 OAuth catalog) still wrapped —
    #    a core rename turns these "original"/"missing" and OAuth entries
    #    silently lose per-provider proxy / account model listing.
    seam_detail = _probe_seams()
    seam_ok = seam_detail is None
    print(f"[{'OK' if seam_ok else 'FAIL'}] transport seams (proxy + oauth catalog) — "
          f"{seam_detail or 'all wrapped'}")
    failures += 0 if seam_ok else 1

    print()
    if failures:
        print(f"selfcheck: {failures} probe(s) FAILED — advanced: blocks may be INERT. "
              "See WARNING lines in the agent log for the injection path that drifted.")
        return 1
    print("selfcheck: all P1 injection surfaces intact.")
    return 0


def _cmd_oauth(args: argparse.Namespace) -> int:
    verb = getattr(args, "adv_oauth_verb", "") or ""
    # Kill switch: the network verbs (login/refresh/models) are dead while
    # list/signout stay usable — cleaning up the local store needs no OAuth.
    if verb in ("login", "refresh", "models"):
        oauth_gate = _adv("oauth_gate")

        if oauth_gate.oauth_disabled():
            print(f"[oauth refused] {oauth_gate.DISABLED_MSG}")
            return 2
    if verb == "list":
        return _oauth_list()
    if verb == "signout":
        return _oauth_signout(getattr(args, "platform", ""), getattr(args, "account", ""))
    if verb == "login":
        return _oauth_login(args)
    if verb == "import":
        return _oauth_import(args)
    if verb == "refresh":
        return _oauth_refresh(args)
    if verb == "models":
        return _oauth_models(args)
    _print_oauth_help()
    return 2


def _oauth_parse_target(args) -> "tuple[str, str]":
    """(platform, account) from either 'platform://account' positional or
    --platform/--account flags. Raises ValueError with usage on garbage."""
    target = str(getattr(args, "target", "") or "").strip()
    platform = str(getattr(args, "platform", "") or "").strip().lower()
    account = str(getattr(args, "account", "") or "").strip()
    if "://" in target:
        p, a = _adv("config_scan").parse_oauth_ref_from_url(target)
        if not p:
            raise ValueError(f"cannot parse '{target}' — expected platform://account")
        return (platform or p), a
    if not platform or not (account or target):
        raise ValueError("give 'platform://account' or --platform <p> <account>")
    return platform, (account or target)


def _oauth_refresh(args) -> int:
    """Force-rotate an account's access+refresh tokens (issue #2)."""
    try:
        platform, account = _oauth_parse_target(args)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 2
    store = _oauth_store()
    if store.get(platform, account) is None:
        print(f"[FAIL] no stored account {platform}://{account} (see: oauth list)")
        return 1
    # The refresh is a network call: use the entry's advanced.proxy if the
    # config binds this account (same lookup the listing path uses).
    proxy = ""
    try:
        entry = _adv("config_scan").RouteTable.from_config().lookup_oauth_ref(
            f"{platform}://{account}")
        proxy = str((entry.get("proxy") if isinstance(entry, dict)
                     else getattr(entry, "proxy", "")) or "")
    except Exception:
        proxy = ""
    proxy = str(getattr(args, "proxy", "") or "") or proxy
    ctx = _adv("oauth_client").OAuthTokenContext(platform, account, store, proxy=proxy)
    try:
        fresh = ctx.force_refresh()
    except Exception as exc:
        print(f"[FAIL] refresh {platform}://{account}: {type(exc).__name__}: {exc}")
        return 1
    has_rt = bool(str(fresh.get("refresh_token") or "").strip())
    print(f"[OK] {platform}://{account} rotated: "
          f"access_token={'present' if fresh.get('access_token') else 'MISSING'}"
          f", refresh_token={'rotated' if has_rt else 'kept'}"
          f", expiry={_describe_expiry(fresh)}")
    return 0


def _oauth_models(args) -> int:
    """List what the ACCOUNT's upstream catalog serves (per-account OAuth)."""
    try:
        platform, account = _oauth_parse_target(args)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 2
    ref = f"{platform}://{account}"
    models = _adv("oauth_models").list_models_for_ref(ref, force_refresh=getattr(args, "refresh", False))
    if models is None:
        print(f"[FAIL] no catalog from {ref}'s upstream — the platform has no listing "
              f"endpoint, the account lacks a refresh-capable credential, or the probe "
              f"failed (see debug log).")
        return 1
    print(f"{len(models)} model(s) from {ref} upstream catalog:\n")
    for m in models:
        print(f"  {m}")
    return 0


def _oauth_store():
    return _adv("account_store").AccountStore()


def _oauth_list() -> int:
    adapters = _adv("adapters")
    get_any, enabled_names = adapters.get_any, adapters.names

    store = _oauth_store()
    accounts = store.list_accounts()
    if not accounts:
        print("No OAuth accounts in the store.")
        print(f"Store: {store.path}")
        print("Add one: hermes advanced-providers oauth login --platform codex")
        return 0
    enabled = set(enabled_names())
    print(f"{len(accounts)} OAuth account(s) in {store.path}\n")
    for platform, name, cred in accounts:
        adapter = get_any(platform)
        display = adapter.display if adapter else platform
        flag = "" if platform in enabled else "  [adapter DISABLED]"
        has_rt = "refresh" if str(cred.get("refresh_token") or "").strip() else "access-only"
        exp = _describe_expiry(cred)
        src = str(cred.get("_imported_from") or "")
        src_txt = f"  from {src}" if src else ""
        print(f"- {platform}://{name}  ({display})  {has_rt}  {exp}{src_txt}{flag}")
    return 0


def _describe_expiry(cred: dict) -> str:
    try:
        exp = _adv("oauth_tokens").parse_expiry(
            cred.get("expired") or cred.get("expires_at") or cred.get("expires_at_ms"))
    except Exception:
        exp = None
    if exp is None:
        return "expiry-unknown"
    import time

    left = int(exp - time.time())
    if left <= 0:
        return f"expired {-left}s ago"
    return f"valid {left // 60}m{left % 60:02d}s"


def _oauth_signout(platform: str, account: str) -> int:
    if not platform or not account:
        print("usage: hermes advanced-providers oauth signout --platform <p> --account <a>")
        return 2
    store = _oauth_store()
    if store.delete(platform, account):
        print(f"Removed {platform}://{account} from the store.")
        return 0
    print(f"No stored account for {platform}://{account} (nothing removed).")
    return 1


def _oauth_login(args) -> int:
    """Interactive login via the platform adapter's own flow (core-reused).

    --json / --json-format shortcut to import instead of a browser dance.
    """
    platform = (getattr(args, "platform", "") or "").strip().lower()
    if not platform:
        print("usage: hermes advanced-providers oauth login --platform <codex|claude|...>")
        print("       (or use: hermes advanced-providers oauth import --file <json>)")
        return 2
    if getattr(args, "json_src", ""):
        return _oauth_import(args)  # login --json == import
    adapters = _adv("adapters")
    get, AdapterUnavailable = adapters.get, adapters.AdapterUnavailable

    adapter = get(platform)
    if adapter is None:
        print(f"[FAIL] platform '{platform}' adapter unavailable (disabled or unknown).")
        return 1
    store = _oauth_store()
    account_name = (getattr(args, "name", "") or "").strip()
    if not account_name:
        print(f"Logging in to {adapter.display} — the account slot will be named from the "
              "credential (email) or pass --name <slot>.")
    try:
        acct = adapter.login_interactive(account_name or _auto_name(adapter, platform),
                                         store, proxy=getattr(args, "proxy", "") or "")
    except AdapterUnavailable as exc:
        print(f"[INFO] {exc}")
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"[FAIL] {platform} login error: {type(exc).__name__}: {exc}")
        return 1
    if acct is None:
        print(f"[FAIL] {platform} login produced no credential (see messages above).")
        return 1
    print(f"Stored {platform}://{acct.get('email') or account_name} in the account store.")
    return 0


def _auto_name(adapter, platform: str) -> str:
    """Adapter login helpers key by a placeholder when the caller gave none;
    codex derives the email after the flow."""
    return "__login__"


def _oauth_import(args) -> int:
    """Import credentials from a JSON file (sub2api / CPA / bare / keyed)."""
    src = getattr(args, "file", "") or getattr(args, "json_src", "") or ""
    if not src:
        print("usage: hermes advanced-providers oauth import --file <path-or-json>")
        return 2
    importers = _adv("importers")

    try:
        payload = importers.load_json(src)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    cands = importers.extract_accounts(payload)
    if not cands:
        print("[FAIL] no recognized account credential in that source "
              "(expected access/refresh/id token fields).")
        return 1
    store = _oauth_store()
    platform_filter = (getattr(args, "platform", "") or "").strip().lower()
    name_filter = (getattr(args, "name", "") or "").strip()
    added = 0
    for c in cands:
        platform, name, acct = c["platform"], c["name"], c["account"]
        if platform_filter and platform != platform_filter:
            continue
        slot = name_filter or name
        existing = store.get(platform, slot)
        if existing is not None and not getattr(args, "force", False):
            print(f"  skip   {platform}://{slot} (already stored; --force to overwrite)")
            continue
        store.put(platform, slot, acct)
        tag = "updated" if existing is not None else "added"
        print(f"  {tag:7} {platform}://{slot}")
        added += 1
    if added:
        print(f"\n{added} account(s) stored. Reference them in config: "
              'base_url: "<platform>://<account>", api_mode: advanced_oauth')
    else:
        print("\nNothing imported (all filtered or already present).")
    return 0


# ---------------------------------------------------------------- probes

def _probe_provider_profile() -> tuple[bool, str]:
    """Check that 'advanced' is registered independently and native 'custom' is untouched."""
    try:
        advlib = _advlib()
        if advlib is not None:
            try:
                from .advlib.provider_profile import build_and_register_advanced_profile, ensure_model_provider_bridge
                ensure_model_provider_bridge()
                build_and_register_advanced_profile()
            except Exception:
                try:
                    from advlib.provider_profile import build_and_register_advanced_profile, ensure_model_provider_bridge
                    ensure_model_provider_bridge()
                    build_and_register_advanced_profile()
                except Exception:
                    pass

        from providers import get_provider_profile

        adv_profile = get_provider_profile("advanced")
        if adv_profile is None:
            return False, "standalone 'advanced' profile not registered"
        adv_cls = type(adv_profile).__name__
        if adv_cls != "AdvancedProviderProfile":
            return False, f"'advanced' profile is {adv_cls}, expected AdvancedProviderProfile"

        custom_profile = get_provider_profile("custom")
        if custom_profile is not None and type(custom_profile).__name__ != "CustomProfile":
            return False, f"native 'custom' profile was unexpectedly modified ({type(custom_profile).__name__})"

        return True, f"standalone 'advanced' -> {adv_cls}; native 'custom' untouched"
    except Exception as exc:
        return False, f"probe error: {type(exc).__name__}: {exc}"


def _probe_gate() -> tuple[bool, str]:
    """The setdefault-before-gate ordering in create_openai_client survives."""
    try:
        import inspect

        import agent.agent_runtime_helpers as arh

        src = inspect.getsource(arh.create_openai_client)
        hook_at = src.find("build_client_kwargs_extras")
        gate_at = src.find('if "http_client" not in client_kwargs')
        if hook_at < 0:
            return False, "build_client_kwargs_extras call site gone"
        if gate_at < 0:
            return False, "http_client gate gone"
        if not (hook_at < gate_at):
            return False, "hook no longer runs BEFORE the gate — injection dead"
        return True, "hook setdefaults ahead of the gate"
    except Exception as exc:
        return False, f"probe error: {type(exc).__name__}: {exc}"


def _probe_key_adoption() -> bool:
    try:
        from hermes_cli.config_providers import _KNOWN_PROVIDER_KEYS

        return "advanced" in set(_KNOWN_PROVIDER_KEYS)
    except Exception:
        return False


def _probe_hook_signature() -> tuple[bool, str]:
    try:
        import inspect

        from providers.base import ProviderProfile

        sig = inspect.signature(ProviderProfile.build_client_kwargs_extras)
        # Contract we rely on: called with base_url= kwarg (create_openai_client
        # passes base_url; anything else means the seam moved).
        params = sig.parameters
        if "context" in params and params["context"].kind == inspect.Parameter.VAR_KEYWORD:
            return True, "accepts **context (base_url kwarg tolerated)"
        if "base_url" in params:
            return True, "explicit base_url parameter"
        return False, f"signature drifted: {sig}"
    except Exception as exc:
        return False, f"probe error: {type(exc).__name__}: {exc}"


def _probe_seams():
    """Transport wrappers present in THIS process after install.

    The CLI process may not be the one that loaded the plugin (the gateway/
    desktop installed its own copy), so we install idempotently here and
    probe the result — what we report is the same code the core seams will
    run under after any agent process boots (identity checks fail loudly as
    'original'/'missing' if the core renamed a hook under us).
    Returns None when every seam is wrapped, else the offending statuses.
    """
    try:
        tp = _adv("transport_patches")
        tp.install()
        states = tp.probe()
    except Exception as exc:
        return f"probe error: {type(exc).__name__}: {exc}"
    bad = {k: v for k, v in states.items() if v not in ("wrapped", "already")}
    if bad:
        return ", ".join(f"{k}={v}" for k, v in bad.items())
    return None


def _redact(value: str) -> str:
    try:
        from urllib.parse import urlsplit, urlunsplit

        p = urlsplit(str(value or ""))
        if p.username or p.password:
            netloc = p.hostname or ""
            if p.port:
                netloc = f"{netloc}:{p.port}"
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
        return str(value or "")
    except Exception:
        return "<url>"
