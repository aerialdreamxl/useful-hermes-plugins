"""Fingerprint ``sub2api``: imitate the Sub2API relay's outbound identity.

Values are the canonical identity pair read from the sub2api source
(``backend/internal/service/openai_codex_identity.go`` +
``openai_gateway_service.go``):

- ``Originator: codex_cli_rs`` — the official Codex CLI originator
- ``Version: <codex CLI version>`` — inference surface only (auth surface
  carries no version, per their openai_codex_identity.go:88-90 note)
- ``User-Agent: codex_cli_rs/<ver> (Ubuntu 22.4.0; x86_64) xterm-256color``
- Chatgpt-Account-Id passes through from the token's JWT (core helper
  already stamps it when the hermes path would; here we set it explicitly
  when the account record carries one).

The version is pinned to their compiled constant at recon time; override via
account field ``fingerprint_version`` when upstream raises the floor
(codexUpstreamMinVersion = 0.144.0 in their source, issue #3901).
"""

from __future__ import annotations

from typing import Any

NAME = "sub2api"

#: Platforms this port imitates. Codex from ``openai_codex_identity.go``,
#: claude from ``internal/pkg/claude/constants.go`` (DefaultHeaders), antigravity
#: from ``internal/pkg/antigravity/oauth.go`` (BuildUserAgent).
SUPPORTS = ("codex", "claude", "antigravity")

DEFAULT_VERSION = "0.154.0"
UA_TEMPLATE = "codex_cli_rs/{version} (Ubuntu 22.4.0; x86_64) xterm-256color"

#: internal/pkg/claude/constants.go:90 (CLICurrentVersion) + cli_version_runtime.go
#: DefaultUserAgent().
CLAUDE_VERSION = "2.1.258"
CLAUDE_UA_TEMPLATE = "claude-cli/{version} (external, cli)"
CLAUDE_HEADERS = {
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "0.94.0",
    "X-Stainless-OS": "Linux",
    "X-Stainless-Arch": "arm64",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v24.3.0",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Timeout": "600",
    "X-App": "cli",
    # Their DefaultHeaders sets this for OAuth traffic; Claude-scoped
    # subscriptions tolerate a browser-ish client, a bare SDK does not.
    "Anthropic-Dangerous-Direct-Browser-Access": "true",
}

#: internal/pkg/antigravity/oauth.go:37 (DefaultUserAgentVersion) +
#: BuildUserAgent() — note their template is the plain ``windows/amd64`` form,
#: unlike CPA's ``(aidev_client; os_type=... )`` variant.
ANTIGRAVITY_VERSION = "2.23.0"
ANTIGRAVITY_UA_TEMPLATE = "antigravity/hub/{version} windows/amd64"


def _version(account: dict, platform: str, default: str) -> str:
    """Per-platform override first, then the shared generic one."""
    for key in (f"fingerprint_version_{platform}", "fingerprint_version"):
        val = str(account.get(key) or "").strip()
        if val:
            return val
    return default


def _claude(headers: Any, account: dict) -> None:
    version = _version(account, "claude", CLAUDE_VERSION)
    headers["User-Agent"] = CLAUDE_UA_TEMPLATE.format(version=version)
    for name, value in CLAUDE_HEADERS.items():
        headers[name] = value


def _antigravity(headers: Any, account: dict) -> None:
    version = _version(account, "antigravity", ANTIGRAVITY_VERSION)
    headers["User-Agent"] = ANTIGRAVITY_UA_TEMPLATE.format(version=version)


def _jwt_claim(token: str, claim: str) -> str:
    """Top-level claim, then the nested ChatGPT auth dict (where real Codex
    access tokens carry chatgpt_account_id — same location the core's
    codex_headers helper reads)."""
    import base64
    import json

    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        val = data.get(claim)
        if not val:
            auth = data.get("https://api.openai.com/auth")
            if isinstance(auth, dict):
                val = auth.get(claim)
        return str(val or "")
    except Exception:
        return ""


def apply(request: Any, *, platform: str, account: dict, phase: str,
          access_token: str = "") -> None:
    headers = request.headers
    if platform == "claude":
        _claude(headers, account)
        return
    if platform == "antigravity":
        _antigravity(headers, account)
        return
    if platform != "codex":
        return
    version = _version(account, "codex", DEFAULT_VERSION)
    headers["Originator"] = "codex_cli_rs"
    headers["User-Agent"] = UA_TEMPLATE.format(version=version)
    if phase == "inference":
        headers["Version"] = version
    acct_id = (str(account.get("account_id") or "")
               or _jwt_claim(access_token, "chatgpt_account_id"))
    if acct_id:
        headers["Chatgpt-Account-Id"] = acct_id
