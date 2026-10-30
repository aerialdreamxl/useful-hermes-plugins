# hermes-provider-advanced

Per-provider `advanced:` settings for Hermes custom providers — two features
in one plugin pair:

1. **Proxy routing** — `advanced.proxy` sends ONE provider's traffic through an
   HTTP proxy (the rest of the session stays direct). NO_PROXY, TLS verify
   options and the core's keepalive semantics are honoured.
2. **OAuth subscription binding** — `api_mode: advanced_oauth` plus a
   `codex://<account>`-style base_url connects a ChatGPT-Codex / Claude
   subscription / Google-Antigravity account from the plugin's own credential
   store (import from CPA/Sub2API dumps, interactive browser login, or bare
   JSON), refreshed per request with single-use-rotation safety.

Zero core patches: everything rides official plugin seams (user-layer provider
profile, registered transport dialect, `create_client` hook).

```yaml
providers:
  my-proxy-only-provider:
    base_url: https://api.example.com/v1
    key_env: MY_KEY
    advanced:
      proxy: "http://127.0.0.1:7890"   # only THIS provider goes through the proxy
      auth_mode: api                    # default

  my-subscription-account:
    name: "[OAUTH] Codex"
    base_url: "codex://you@example.com" # <platform>://<account slot>
    api_mode: advanced_oauth
    advanced:
      proxy: "http://127.0.0.1:7890"
      oauth_fingerprint: hermes         # hermes | cpa | sub2api (request identity)
```

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
├── __init__.py            # register(ctx): `hermes advanced-providers` CLI
├── cli.py                 # status / selfcheck / oauth login|import|list|signout|refresh|models
├── advlib/                # shared logic — imported by BOTH halves
│   ├── __init__.py        # load_advlib() fixed-name loader + client extras entry
│   ├── config_scan.py     # raw-config route table (api/oauth split)
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
│   └── hermes-advanced-custom/   # user-layer overlay of the bundled `custom` profile
│       ├── __init__.py           # extends CustomProfile: client extras + create_client
│       └── plugin.yaml           # kind: model-provider
└── tests/                 # offline suite (fake backends, temp HOMEs) + live smoke scripts
```

## Install (two halves — both required)

From wherever you cloned this collection:

```bash
# 1. agent-side half — enable as `custom-plugins/hermes-provider-advanced` in plugins.enabled
cp -r custom-plugins/hermes-provider-advanced "$HERMES_HOME/plugins/custom-plugins/"

# 2. overlay half — provider discovery only reads $HERMES_HOME/plugins/model-providers/,
#    so COPY it out of the plugin dir (nested placement there is not scanned):
cp -r hermes-provider-advanced/model-providers/hermes-advanced-custom \
      "$HERMES_HOME/plugins/model-providers/hermes-advanced-custom"
```

Restart Hermes afterwards (desktop: full app restart — model-provider dirs are
stat-polled, but the agent-side half and its seams load at process start).
Proxy-only usage needs both halves too: the overlay is where the proxy client
gets injected into the `custom` provider path.

## How injection works (zero core patches)

1. provider discovery imports
   `$HERMES_HOME/plugins/model-providers/hermes-advanced-custom/` (user layer
   beats bundled on name collision).
2. The overlay clones the registered bundled `CustomProfile` (dataclass field
   copy — core behaviour like reasoning-effort clamps survives), subclasses it,
   and re-registers under the same name `custom`.
3. Core client construction calls `profile.build_client_kwargs_extras(base_url=…)`
   and `setdefault`s the result BEFORE its `if "http_client" not in client_kwargs`
   gate → our proxy `httpx.Client` wins. For `platform://account` refs the same
   profile's `create_client` hook materializes the OAuth client instead.
4. The proxy client mirrors the core keepalive client's proxy branch
   (`read=None` SSE lifeline included), fresh client per SDK client. NO_PROXY
   rides the core matcher, `ssl_verify`/`ssl_ca_cert` honoured per entry.

Fail-open at every layer: advlib missing → overlay not installed; hook raises →
bundled extras only; factory fails → core keepalive path.

## Environment switches

| Variable | Effect |
|---|---|
| `ADVANCED_DISABLE_OAUTH=1` | Kill switch for the OAuth half: transport dialect not registered, `platform://account` refs refused with a clear message, refresh/login/listing dead-end. Proxy routing is unaffected. `list`/`signout` still work (local store hygiene). |
| `ADVANCED_ADAPTERS=codex,claude` | Comma list of platforms to DISABLE (empty string disables all OAuth adapters). |
| `HERMES_ADVANCED_LIB_DIR` | Override where `advlib` is imported from (dev/test). |

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
hermes advanced-providers selfcheck   # upgrade-resilience probes: seams, overlay liveness
```

Agent-log lines (INFO, logger `hermes.advanced_providers`) name the exact fact
under investigation: 成功注入代理服务器 / 代理…未能注入（直连警示）/
OAuth实现已接管请求（实现=hermes原生|CPA移植|Sub2API移植，含回退链）/
OAuth客户端就绪 / OAuth令牌刷新成功。

## Tests

Offline suite (no network, spend-free): run every `tests/test_*.py`.
`HERMES_CORE_ROOT` pointing at a hermes-agent checkout additionally enables
the core-surface and overlay E2E files (they skip without it).
`tests/live_*.py` are manual smokes that DO spend real quota — account and
paths come from arguments/env; see their docstrings.

## License

GPL-3.0 (repository-root `LICENSE`). Parts are ports from upstream projects
(CLIProxyAPI, Sub2API) — module-by-module attribution in the repository-root
`NOTICE-hermes-provider-advanced.md`; the upstream MIT text is preserved as
`LICENCE.cpa`.

## Known noise

Core logs a warn-once `unknown config keys ignored: advanced` per entry —
expected until the upstream FR lands (`advanced`/`proxy` into
`_KNOWN_PROVIDER_KEYS`); `selfcheck` reports adoption status.
