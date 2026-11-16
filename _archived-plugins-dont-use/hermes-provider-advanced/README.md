# hermes-provider-advanced

Standalone provider profiles for a dedicated top-level `providers-advanced:`
config section — three features in one plugin:

1. **Proxy routing** — `advanced.proxy` sends ONE provider's traffic through an
   HTTP proxy (the rest of the session stays direct). NO_PROXY, TLS verify
   options and the core's keepalive semantics are honoured.
2. **OAuth subscription binding** — `api_mode: advanced_oauth` plus a
   `codex://<account>`-style base_url connects a ChatGPT-Codex / Claude
   subscription / Google-Antigravity / Qwen-Portal account from the plugin's
   own credential store (import from CPA/Sub2API dumps, interactive browser
   login, or bare JSON), refreshed per request with single-use-rotation safety.
3. **OpenRouter implicit-fallback guard** (opt-in, default off) — the core
   resolves bare `custom`/`auto` requests to `https://openrouter.ai/api/v1` as
   a last resort and will silently spend an `OPENROUTER_API_KEY` sitting in
   your environment. The setting `block-custom-openrouter-fallback: true`
   turns that silent fallback into a loud refusal for any request not
   explicitly addressed to OpenRouter, so your configured fallback chain takes
   over instead.

Zero core patches: registration rides the official model-provider plugin seam
(a bridge module under `$HERMES_HOME/plugins/model-providers/`), transport
rides the profile's own `build_client_kwargs_extras` / `create_client` hooks,
and the OAuth dialect is a registered transport scheme.

## Core insulation contract (v0.3, important)

The plugin manages **only** its own `providers-advanced:` section. The core's
native `providers:` and legacy `custom_providers:` sections are completely
hands-off — never intercepted, wrapped, or proxied. A leftover `advanced:`
block inside `providers:` is ignored and logged as a warning (it no longer
routes anything). Every managed entry is registered under the dedicated
namespace `advanced:<key>` and shown to the agent as `[ADVANCED] <name>`, so
model/provider selection is always explicit.

```yaml
providers-advanced:
  deepseek-proxy:
    name: "DeepSeek via proxy"
    base_url: https://api.deepseek.com/v1
    key_env: DEEPSEEK_API_KEY
    advanced:
      proxy: "http://127.0.0.1:7890"   # only THIS provider goes through the proxy
      auth_mode: api                    # default

  codex-sub:
    name: "[OAUTH] Codex"
    base_url: "codex://you@example.com" # <platform>://<account slot>
    api_mode: advanced_oauth
    advanced:
      proxy: "http://127.0.0.1:7890"
      oauth_fingerprint: hermes         # hermes | cpa | sub2api (request identity)
```

Model lists: an entry may pin `models:` (dict or list) and
`discover_models: false` for a stable offline picker; OAuth entries fall back
to the account's own upstream catalog; everything else hits the endpoint's
`/models`.

Fingerprint resolution is a fallback chain: the requested scheme supplies the
identity when it covers that platform, otherwise `hermes` → `cpa` → `sub2api`
in order, with every swap logged. `cpa`/`sub2api` values are constants read
from those projects' sources (citations in each file's header; attribution in
the repository-root `NOTICE-hermes-provider-advanced.md`).

> **ToS note:** OAuth subscription channels and client-identity fingerprints
> may be restricted by each AI provider's terms of service. Evaluate and carry
> that responsibility yourself before enabling them.

## Layout

```
hermes-provider-advanced/
├── plugin.yaml            # agent-side half manifest (kind: standalone)
├── __init__.py            # register(ctx): CLI + standalone profiles + guard + bridge
├── cli.py                 # status / selfcheck / oauth login|import|list|signout|refresh|models
├── advlib/                # shared logic — imported by BOTH halves
│   ├── __init__.py        # load_advlib() fixed-name loader + client extras entry
│   ├── config_scan.py     # raw-config route table (providers-advanced:, api/oauth split)
│   ├── provider_profile.py# AdvancedProviderProfile + registration + bridge generator
│   ├── fallback_guard.py  # opt-in OpenRouter implicit-fallback refusal
│   ├── proxy_factory.py   # httpx proxy client mirroring core keepalive semantics
│   ├── oauth_client.py    # token context, Responses<->chat facade, streaming assembly
│   ├── oauth_tokens.py    # refresh-and-persist: peer-adopt + in-process single-flight
│   ├── oauth_login.py     # interactive browser logins (claude / antigravity)
│   ├── oauth_models.py    # account-scoped upstream model catalogs
│   ├── oauth_gate.py      # ADVANCED_DISABLE_OAUTH kill switch + single-flight primitive
│   ├── oplog.py           # operator-readable takeover/proxy log lines
│   ├── account_store.py   # $HERMES_HOME credential store (atomic, locked)
│   ├── adapters/          # codex / claude / antigravity / qwen — one file per platform
│   ├── fingerprints/      # hermes / cpa / sub2api request identities
│   ├── antigravity_*.py   # cloudcode-pa wire translators (CPA port)
│   └── gemini_schema_clean.py  # tool-schema engine (CPA port)
├── model-providers/
│   └── hermes-provider-advanced/   # discovery bridge (kind: model-provider)
│       ├── __init__.py             # loads advlib, registers advanced:<key> profiles
│       └── plugin.yaml
└── tests/                 # offline suite (fake backends, temp HOMEs) + live smoke scripts
```

## Install (two halves — both required)

From wherever you cloned this collection:

```bash
# 1. agent-side half — put it under plugins/ (flat, or in this one-level
#    category dir), then add `custom-plugins/hermes-provider-advanced` (or the
#    bare name for a flat install) to plugins.enabled:
cp -r custom-plugins/hermes-provider-advanced "$HERMES_HOME/plugins/custom-plugins/"

# 2. discovery bridge — provider discovery only reads $HERMES_HOME/plugins/model-providers/,
#    so the bridge must sit there. The plugin (re)generates it automatically on every
#    agent/CLI start; you can also copy it by hand up front:
mkdir -p "$HERMES_HOME/plugins/model-providers/"
cp -r hermes-provider-advanced/model-providers/hermes-provider-advanced \
      "$HERMES_HOME/plugins/model-providers/hermes-provider-advanced"
```

Restart Hermes afterwards (desktop: full app restart — model-provider dirs are
stat-polled, but the agent-side half and its seams load at process start).

The bridge locates `advlib` in this order: `$HERMES_HOME/plugins/hermes-provider-advanced/`,
`$HERMES_HOME/plugins/custom-plugins/hermes-provider-advanced/`,
then the sibling-of-model-providers layout (covers a repo clone used in place).
If none resolves, provider discovery keeps working with zero advanced
behaviour (fail-open); `hermes advanced-providers selfcheck` reports which
half is live.

## How registration works (zero core patches)

1. Model-provider discovery imports
   `$HERMES_HOME/plugins/model-providers/hermes-provider-advanced/` (user layer
   beats bundled on name collision).
2. The bridge loads `advlib` under the fixed module name `hermes_advlib` (both
   halves share one instance per process) and registers one
   `AdvancedProviderProfile` per `providers-advanced:` entry under
   `advanced:<key>` (+ its bare key as alias), plus an aggregate fallback
   profile named `advanced`. `hermes model` picker flows are wired for these
   slugs too.
3. Core client construction calls the profile's
   `build_client_kwargs_extras(base_url=…)` and `setdefault`s the result BEFORE
   its `if "http_client" not in client_kwargs` gate → our proxy `httpx.Client`
   wins. For `platform://account` refs the same profile's `create_client` hook
   materializes the OAuth client instead.
4. The proxy client mirrors the core keepalive client's proxy branch
   (`read=None` SSE lifeline included), fresh client per SDK client. NO_PROXY
   rides the core matcher, `ssl_verify`/`ssl_ca_cert` honoured per entry.

Fail-open at every layer: advlib missing → bridge no-op; hook raises →
profile falls back to a plain core client; factory fails → core keepalive path.

## Environment switches and settings

| Variable / setting | Effect |
|---|---|
| `ADVANCED_DISABLE_OAUTH=1` | Kill switch for the OAuth half: transport dialect not registered, `platform://account` refs refused with a clear message, refresh/login/listing dead-end. Proxy routing is unaffected. `list`/`signout` still work (local store hygiene). |
| `ADVANCED_ADAPTERS=codex,claude` | Comma list of platforms to DISABLE (empty string disables all OAuth adapters). |
| `HERMES_ADVANCED_LIB_DIR` | Override where `advlib` is imported from (dev/test). |
| `plugins.entries.<id>.settings.block-custom-openrouter-fallback: true` | Opt-in fallback guard (default off — core behaviour untouched). Reads from both the bare plugin name and the `custom-plugins/<id>` path key; re-read per resolution, so toggling needs no restart. |

## Account store

Credentials live in `$HERMES_HOME/.advanced-custom-providers.json`. Single-use
refresh rotation is handled with peer-adoption plus an in-process flight
deduplicator — two concurrent turns never replay the same refresh token.
Tokens are never written to logs; proxy URLs are credential-redacted in logs.

```
hermes advanced-providers oauth import --file dump.json   # CPA / Sub2API / bare JSON shapes
hermes advanced-providers oauth login --platform antigravity
hermes advanced-providers oauth list                      # slots + expiry, no secrets
hermes advanced-providers oauth refresh codex://you@example.com
```

## Diagnose

```
hermes advanced-providers status      # parsed entries, route match, proxy probe
hermes advanced-providers selfcheck   # upgrade-resilience probes: seams, bridge liveness
```

Agent-log lines (INFO, logger `hermes.advanced_providers`) name the exact fact
under investigation: 成功注入代理服务器 / 代理…未能注入（直连警示）/
OAuth实现已接管请求（实现=hermes原生|CPA移植|Sub2API移植，含回退链）/
OAuth客户端就绪 / OAuth令牌刷新成功。When the fallback guard fires it logs an
explicit refusal naming the requested provider and the resolved OpenRouter URL.

## Tests

Offline suite (no network, spend-free): run every `tests/test_*.py`.
`HERMES_CORE_ROOT` pointing at a hermes-agent checkout additionally enables
the core-surface and bridge E2E files (they skip without it).
`tests/live_*.py` are manual smokes that DO spend real quota — account and
paths come from arguments/env; see their docstrings.

## License

GPL-3.0 (repository-root `LICENSE`). Parts are ports from upstream projects
(CLIProxyAPI, Sub2API) — module-by-module attribution in the repository-root
`NOTICE-hermes-provider-advanced.md`; the upstream MIT text is preserved as
`LICENCE.cpa`.

## Known noise

Core may log a warn-once about the unknown top-level config key
`providers-advanced` until upstream learns it; it is cosmetic — the plugin
reads the raw config itself and governs that section. The credential-store
filename (`.advanced-custom-providers.json`) predates the v0.3 namespace
rename and is kept for compatibility.
