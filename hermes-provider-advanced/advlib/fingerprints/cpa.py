"""Fingerprint ``cpa``: imitate CLIProxyAPI's outbound identity (P3.5).

Values below are copied from CPA's own executors, not from its HTTP front
door — the front door is a *proxy*, so its inbound header handling says
nothing about what a subscription endpoint sees. Each platform therefore has
its own executor identity:

- codex  ``internal/runtime/executor/codex_executor_request.go:32-33`` —
  ``codex-tui`` TUI pair (UA + Originator) plus ``Connection: Keep-Alive``,
  ``Chatgpt-Account-Id``. The old ``codex_cli_rs`` value came from
  ``server_routes.go`` alpha-search routing and is NOT the inference path.
- claude ``internal/runtime/executor/claude_executor_request.go:1361-1382`` +
  ``helps/claude_device_profile.go`` — Claude Code 2.1.280 / SDK 0.112.1
  device profile and the ``X-Stainless-*`` / ``X-App`` identity set.
- antigravity ``internal/misc/antigravity_version.go`` — the Hub app UA
  ``antigravity/hub/<ver> (aidev_client; os_type=darwin; arch=arm64; cl=…)``;
  the executor adds nothing else beyond Content-Type/Authorization.

Two deliberate omissions, both transport-owned by the SDK on our path and
therefore left untouched: ``Accept`` (would break SSE vs JSON negotiation per
request) and the ``Version``/``Session-Id`` family (CPA forwards those from
its caller; we have no caller, so CPA itself would send nothing).

Per-account overrides kept for drift: ``user_agent`` and
``fingerprint_version`` on the stored account record.

Removal path: delete this file — the loader falls back to ``hermes``.
"""

from __future__ import annotations

from typing import Any

NAME = "cpa"

#: Platforms the CPA port supplies identities for (codex / claude / antigravity).
SUPPORTS = ("codex", "claude", "antigravity")

# --- codex -----------------------------------------------------------------
# codex_executor_request.go:32-33 (const codexUserAgent / codexOriginator),
# also re-asserted by applyCodexCloakingHeaders (same file, :552-558).
CODEX_UA = ("codex-tui/0.154.0 (Mac OS 26.5.2; arm64) "
            "iTerm.app/3.6.11 (codex-tui; 0.154.0)")
CODEX_ORIGINATOR = "codex-tui"

# --- claude ----------------------------------------------------------------
# claude_executor_request.go:1361-1382 + helps/claude_device_profile.go:23-28.
CLAUDE_UA = "claude-cli/2.1.280 (external, cli)"
CLAUDE_HEADERS = {
    "Anthropic-Version": "2023-06-01",
    "Anthropic-Dangerous-Direct-Browser-Access": "true",
    "X-App": "cli",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Lang": "js",
    "X-Stainless-Timeout": "600",
    "X-Stainless-Package-Version": "0.112.1",
    "X-Stainless-Runtime-Version": "v26.3.0",
    "X-Stainless-Os": "MacOS",
    "X-Stainless-Arch": "arm64",
}

# --- antigravity -----------------------------------------------------------
# internal/misc/antigravity_version.go:20-38 (fallback version + platform +
# changelist table) and AntigravityUserAgent().
ANTIGRAVITY_UA_TEMPLATE = ("antigravity/hub/{version} "
                          "(aidev_client; os_type=darwin; arch=arm64{changelist})")
ANTIGRAVITY_FALLBACK_VERSION = "2.24.0"
ANTIGRAVITY_CHANGELISTS = {"2.23.0": "998057262", "2.24.0": "1000335682"}


def _codex(headers: Any, account: dict) -> None:
    version = str(account.get("fingerprint_version") or "").strip()
    headers["Originator"] = CODEX_ORIGINATOR
    headers["User-Agent"] = str(account.get("user_agent") or "").strip() or (
        CODEX_UA if not version
        else f"codex-tui/{version} (Mac OS 26.5.2; arm64) "
             f"iTerm.app/3.6.11 (codex-tui; {version})")
    headers["Connection"] = "Keep-Alive"
    acct_id = str(account.get("account_id") or "")
    if acct_id:
        headers["Chatgpt-Account-Id"] = acct_id


def _claude(headers: Any, account: dict) -> None:
    for name, value in CLAUDE_HEADERS.items():
        headers[name] = value
    headers["User-Agent"] = str(account.get("user_agent") or "").strip() or CLAUDE_UA


def _antigravity(headers: Any, account: dict) -> None:
    version = str(account.get("antigravity_version") or account.get("fingerprint_version")
                  or "").strip() or ANTIGRAVITY_FALLBACK_VERSION
    cl = ANTIGRAVITY_CHANGELISTS.get(version)
    headers["User-Agent"] = str(account.get("user_agent") or "").strip() or \
        ANTIGRAVITY_UA_TEMPLATE.format(
            version=version, changelist=f"; cl={cl}" if cl else "")


def apply(request: Any, *, platform: str, account: dict, phase: str,
          access_token: str = "") -> None:
    headers = request.headers
    if platform == "codex":
        _codex(headers, account)
    elif platform == "claude":
        _claude(headers, account)
    elif platform == "antigravity":
        _antigravity(headers, account)
