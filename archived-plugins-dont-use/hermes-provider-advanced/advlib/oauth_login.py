"""Interactive OAuth login flows — code-for-code ports of CPA's auth packages.

Sources (CLIProxyAPI, https://github.com/router-for-me/CLIProxyAPI):

- claude       ``internal/auth/claude/anthropic_auth.go`` (URL/params/scope,
  JSON exchange with the native key order, axios-shaped headers, profile +
  roles companion lookups), ``oauth_server.go`` (loopback callback),
  ``pkce.go`` (96-byte verifier), ``identity.go`` (1x32-byte device id).
- antigravity  ``internal/auth/antigravity/{auth.go,constants.go}`` (Google
  public desktop client, 5 scopes, form exchange, userinfo, loadCodeAssist
  with ``{"metadata":{"ideType":"ANTIGRAVITY"}}`` then onboardUser polling).
- qwen         deliberately NOT implemented: CPA ships no qwen login line, and
  the owner retired qwen login on 2026-10-29.

Both flows are authorization-code + PKCE with a loopback redirect, so they run
in a terminal: print the URL, wait for the browser hit on a stdlib
``http.server`` thread, exchange, then return a store-shaped account dict.

Printed text is ASCII-only on purpose — a Windows console running cp936 raises
UnicodeEncodeError on CJK, and a login that dies mid-flow is worse than plain
English.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from .public_client import (GOOGLE_INSTALLED_CLIENT_ID,
                            GOOGLE_INSTALLED_CLIENT_SECRET)

DEFAULT_TIMEOUT = 300.0


class LoginError(RuntimeError):
    """A login step failed with a user-actionable reason."""


# --------------------------------------------------------------- primitives

def pkce_pair() -> "tuple[str, str]":
    """(verifier, challenge) — CPA pkce.go: 96 random bytes, base64url, no pad."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(32)


def device_id() -> str:
    """CPA identity.go: one 32-byte hex device id per credential."""
    return secrets.token_hex(32)


def _client(proxy: str, timeout: float = 30.0):
    import httpx

    from . import oplog

    kw: "dict[str, Any]" = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        kw["proxy"] = proxy
    oplog.proxy_injected(proxy, scope="登录流程")
    return httpx.Client(**kw)


_SUCCEEDED_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Login complete</title>"
    "</head><body style='font-family:sans-serif;padding:3rem'>"
    "<h2>Login complete</h2><p>You can close this tab and return to the terminal.</p>"
    "</body></html>"
)


class _Collector:
    """Loopback callback server (or manual paste) for one authorize response."""

    def __init__(self, port: int, paths: "tuple[str, ...]", state: str) -> None:
        self.port = port
        self.paths = paths
        self.state = state
        self.result: "dict[str, str]" = {}
        self._event = threading.Event()
        self._server: "Optional[HTTPServer]" = None

    # -- server mode ------------------------------------------------------
    def _start(self) -> bool:
        holder = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib naming)
                parts = urlsplit(self.path)
                if parts.path not in holder.paths:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                q = parse_qs(parts.query)
                holder.result = {
                    "code": (q.get("code") or [""])[0],
                    "state": (q.get("state") or [""])[0],
                    "error": (q.get("error") or [""])[0],
                    "error_description": (q.get("error_description") or [""])[0],
                }
                body = _SUCCEEDED_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                holder._event.set()

            def log_message(self, *a):  # silence request logging
                pass

        try:
            self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            return False
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return True

    def _stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    # -- both modes -------------------------------------------------------
    def collect(self, timeout: float = DEFAULT_TIMEOUT) -> "tuple[str, str]":
        """(code, state_from_fragment). Password-free: only the code is read."""
        if self._start():
            print(f"[login] waiting for the browser callback on port {self.port} "
                  f"({', '.join(self.paths)}) ...")
            try:
                if not self._event.wait(timeout):
                    raise LoginError(f"no callback within {int(timeout)}s "
                                     f"(port {self.port} never received it)")
            finally:
                self._stop()
            if self.result.get("error"):
                raise LoginError(f"authorization failed: {self.result['error']} "
                                 f"{self.result.get('error_description') or ''}".strip())
            raw = (self.result.get("code") or "").strip()
            frag = ""
            if "#" in raw:  # CPA parseCodeAndState: #state wins over the cookie
                raw, _, frag = raw.partition("#")
            if not raw:
                raise LoginError("callback carried no authorization code")
            if self.result.get("state") and self.result["state"] != self.state:
                print("[login] WARNING: state mismatch in callback (continuing; "
                      "the exchange is still PKCE-bound)")
            return raw, frag

        print(f"[login] port {self.port} is busy - paste the full redirect URL instead.")
        print("[login] (open the URL above, then copy the address bar of the page "
              "your browser lands on)")
        try:
            pasted = input("[login] redirect URL: ").strip()
        except EOFError:
            raise LoginError("no callback URL pasted (port was busy)") from None
        parts = urlsplit(pasted)
        q = parse_qs(parts.query)
        if q.get("error"):
            raise LoginError(f"authorization failed: {q['error'][0]}")
        raw = (q.get("code") or [""])[0].strip()
        frag = ""
        if "#" in raw:
            raw, _, frag = raw.partition("#")
        if not raw:
            raise LoginError("pasted URL carried no authorization code")
        return raw, frag


# ------------------------------------------------------------------- claude

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_REDIRECT = "http://localhost:54545/callback"
CLAUDE_SCOPE = ("user:profile user:inference user:sessions:claude_code "
                "user:mcp_servers user:file_upload")
CLAUDE_AUTH_URL = "https://claude.ai/oauth/authorize"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_ROLES_URL = "https://api.anthropic.com/api/oauth/claude_cli/roles"

# applyClaudeOAuthAxiosHeaders (anthropic_auth.go:215-225)
CLAUDE_AXIOS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "axios/1.15.2",
    "Accept-Encoding": "gzip, compress, deflate, br",
    "Connection": "close",
}


def claude_login(proxy: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run the Claude Code OAuth flow; returns a store-shaped account dict."""
    verifier, challenge = pkce_pair()
    state = new_state()
    params = {
        "code": "true",
        "client_id": CLAUDE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT,
        "scope": CLAUDE_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = CLAUDE_AUTH_URL + "?" + urlencode(params)
    print("\n[login] open this URL in a browser and approve:\n")
    print("  " + url + "\n")

    code, frag_state = _Collector(54545, ("/callback",), state).collect(timeout)

    # CPA authorizationCodeExchangeRequest: field ORDER is the wire contract.
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_REDIRECT,
        "client_id": CLAUDE_CLIENT_ID,
        "code_verifier": verifier,
        "state": frag_state or state,
    }
    headers = dict(CLAUDE_AXIOS_HEADERS)
    headers["Content-Type"] = "application/json"
    with _client(proxy) as c:
        r = c.post(CLAUDE_TOKEN_URL, content=json.dumps(body).encode(), headers=headers)
        if r.status_code != 200:
            text = (r.text or "")[:300]
            raise LoginError(f"token exchange failed with status {r.status_code}: {text}")
        try:
            tok = r.json()
        except Exception:
            raise LoginError("token endpoint returned non-JSON") from None

        at = str(tok.get("access_token") or "").strip()
        rt = str(tok.get("refresh_token") or "").strip()
        if not at:
            raise LoginError("token response missing access_token")

        acct: "dict[str, Any]" = {
            "access_token": at,
            "refresh_token": rt,
            "email": str(((tok.get("account") or {}) if isinstance(tok.get("account"), dict)
                          else {}).get("email_address") or "").strip(),
            "account_uuid": str(((tok.get("account") or {}) if isinstance(tok.get("account"), dict)
                                 else {}).get("uuid") or "").strip(),
            "organization_uuid": str(((tok.get("organization") or {})
                                      if isinstance(tok.get("organization"), dict) else {}
                                      ).get("uuid") or "").strip(),
            "organization_name": str(((tok.get("organization") or {})
                                      if isinstance(tok.get("organization"), dict) else {}
                                      ).get("name") or "").strip(),
            "expired": str(int(time.time() + int(tok.get("expires_in") or 0))),
            "type": "oauth-token",
            "device_ids": [device_id()],
            "_imported_from": "login:claude",
        }

        # Native companion lookups (advisory): profile identity wins over the
        # exchange body where it carries something; roles is a no-op token check.
        ph = dict(CLAUDE_AXIOS_HEADERS)
        ph["Authorization"] = f"Bearer {at}"
        ph["Cache-Control"] = "no-cache"
        try:
            pr = c.get(CLAUDE_PROFILE_URL, headers=ph, timeout=15)
            if pr.status_code == 200:
                prof = pr.json() or {}
                acc = prof.get("account") if isinstance(prof.get("account"), dict) else {}
                org = prof.get("organization") if isinstance(prof.get("organization"), dict) else {}
                acct["account_uuid"] = str(acc.get("uuid") or acct["account_uuid"])
                acct["email"] = str(acc.get("email") or acct["email"])
                acct["organization_uuid"] = str(org.get("uuid") or acct["organization_uuid"])
                acct["organization_name"] = str(org.get("name") or acct["organization_name"])
        except Exception:
            print("[login] note: profile lookup failed (identity from token body only)")
        try:
            c.get(CLAUDE_ROLES_URL, headers=ph, timeout=15)
        except Exception:
            pass
    if not acct["email"]:
        raise LoginError("could not determine the account email - pass --name explicitly")
    return acct


# -------------------------------------------------------------- antigravity

AG_CLIENT_ID = GOOGLE_INSTALLED_CLIENT_ID
AG_CLIENT_SECRET = GOOGLE_INSTALLED_CLIENT_SECRET
AG_REDIRECT = "http://localhost:51121/oauth-callback"
AG_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
AG_TOKEN_URL = "https://oauth2.googleapis.com/token"
AG_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo?alt=json"
AG_DAILY = "https://daily-cloudcode-pa.googleapis.com/v1internal"
AG_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)
AG_VERSION = "2.24.0"
AG_CHANGELISTS = {"2.23.0": "998057262", "2.24.0": "1000335682"}


def ag_user_agent(version: str = AG_VERSION) -> str:
    """misc.AntigravityUserAgent(): Hub app UA incl. changelist when known."""
    cl = AG_CHANGELISTS.get(version)
    return (f"antigravity/hub/{version} (aidev_client; os_type=darwin; arch=arm64"
            f"{'; cl=' + cl if cl else ''})")


def _ag_project(at: str, proxy: str) -> "tuple[str, str]":
    """(project_id, tier_id) via loadCodeAssist, onboarding when absent.

    CPA auth.go:248-316: loadCodeAssist carries ONLY ideType (adding
    pluginType is a 400); a project-less answer triggers onboardUser, polled
    5x with a 2s gap. Any failure here is soft: the account still stores, and
    the inference facade resolves the project lazily.
    """
    tier = "free-tier"
    project = ""
    ua = ag_user_agent()
    headers = {"Authorization": f"Bearer {at}", "Content-Type": "application/json",
               "User-Agent": ua}
    with _client(proxy) as c:
        try:
            r = c.post(AG_DAILY + ":loadCodeAssist",
                       content=json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode(),
                       headers=headers, timeout=20)
            data = r.json() if r.status_code == 200 else {}
        except Exception as exc:
            print(f"[login] note: loadCodeAssist failed ({type(exc).__name__}) - "
                  f"project will be resolved at first request")
            return "", tier
        if isinstance(data, dict):
            for key in ("cloudaicompanionProject", "projectId", "project"):
                v = data.get(key)
                if isinstance(v, dict):
                    v = v.get("id")
                if isinstance(v, str) and v.strip():
                    project = v.strip()
                    break
            for t in data.get("allowedTiers") or []:
                if isinstance(t, dict) and t.get("isDefault") and t.get("id"):
                    tier = str(t["id"]).strip()
                    break
            else:
                cur = data.get("currentTier")
                if isinstance(cur, dict) and cur.get("id"):
                    tier = str(cur["id"]).strip()
        if project:
            return project, tier

        # onboardUser: node-shaped UA + X-Goog-Api-Client, poll until done.
        ob_headers = {"Authorization": f"Bearer {at}", "Accept": "*/*",
                      "Content-Type": "application/json",
                      "User-Agent": ua + " google-api-nodejs-client/10.3.0",
                      "X-Goog-Api-Client": "gl-node/22.21.1"}
        ob_body = {"tier_id": tier,
                   "metadata": {"ide_type": "ANTIGRAVITY", "ide_version": AG_VERSION,
                                "ide_name": "antigravity"}}
        print(f"[login] onboarding the account (tier {tier}) ...")
        for attempt in range(1, 6):
            try:
                r = c.post(AG_DAILY + ":onboardUser",
                           content=json.dumps(ob_body).encode(),
                           headers=ob_headers, timeout=30)
            except Exception as exc:
                print(f"[login] note: onboardUser attempt {attempt} failed "
                      f"({type(exc).__name__})")
                break
            if r.status_code != 200:
                print(f"[login] note: onboardUser -> {r.status_code}")
                break
            data = r.json() or {}
            if data.get("done"):
                resp = data.get("response") if isinstance(data.get("response"), dict) else {}
                v = resp.get("cloudaicompanionProject")
                if isinstance(v, dict):
                    v = v.get("id")
                project = str(v or "").strip()
                break
            time.sleep(2)
    return project, tier


def antigravity_login(proxy: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run Google OAuth for Antigravity; returns a store-shaped account dict."""
    state = new_state()
    params = {
        "access_type": "offline",
        "client_id": AG_CLIENT_ID,
        "prompt": "consent",
        "redirect_uri": AG_REDIRECT,
        "response_type": "code",
        "scope": " ".join(AG_SCOPES),
        "state": state,
    }
    print("\n[login] open this URL in a browser and approve:\n")
    print("  " + AG_AUTH_URL + "?" + urlencode(params) + "\n")

    code, _ = _Collector(51121, ("/oauth-callback",), state).collect(timeout)

    form = urlencode({
        "code": code,
        "client_id": AG_CLIENT_ID,
        "client_secret": AG_CLIENT_SECRET,
        "redirect_uri": AG_REDIRECT,
        "grant_type": "authorization_code",
    })
    with _client(proxy) as c:
        r = c.post(AG_TOKEN_URL, content=form.encode(),
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise LoginError(f"token exchange failed with status {r.status_code}: "
                             f"{(r.text or '')[:300]}")
        tok = r.json() or {}
        at = str(tok.get("access_token") or "").strip()
        if not at:
            raise LoginError("Google token response missing access_token")
        email = ""
        try:
            ur = c.get(AG_USERINFO_URL, headers={"Authorization": f"Bearer {at}"}, timeout=15)
            if ur.status_code == 200:
                email = str((ur.json() or {}).get("email") or "").strip()
        except Exception:
            print("[login] note: userinfo lookup failed")

    if not email:
        raise LoginError("could not determine the Google account email - pass --name")

    project, tier = _ag_project(at, proxy)
    acct = {
        "access_token": at,
        "refresh_token": str(tok.get("refresh_token") or "").strip(),
        "email": email,
        "expired": str(int(time.time() + int(tok.get("expires_in") or 0))),
        "token_type": str(tok.get("token_type") or "Bearer"),
        "client_id": AG_CLIENT_ID,
        "client_secret": AG_CLIENT_SECRET,
        "project_id": project,
        "tier_id": tier,
        "antigravity_version": AG_VERSION,
        "type": "antigravity",
        "_imported_from": "login:antigravity",
    }
    if not project:
        print("[login] note: no project id yet - the inference facade will "
              "resolve it on the first request")
    return acct


def slot_name(account_name: str, email: str, platform: str) -> str:
    """Store slot for a completed login: explicit --name wins, else the email.

    The CLI's placeholder (``__login__``) is never a valid slot — two accounts
    logging in that way would overwrite each other.
    """
    name = str(account_name or "").strip()
    if name and not name.startswith("__"):
        return name
    return str(email or "").strip() or f"{platform}-account"
