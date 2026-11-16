"""Operational log lines: the three facts you grep when a provider breaks.

- 成功注入代理服务器 …   (was a proxy actually used?)
- OAuth实现已接管请求 …  (which implementation, which server, which model?)
- OAuth客户端就绪 / OAuth令牌刷新成功

The point is not pretty logging: before this, a silently-dropped proxy or a
fingerprint fallback left no trace, and a "provider is broken" report had to be
diagnosed by reading source.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from advlib import oauth_client, oplog  # noqa: E402
from advlib.account_store import AccountStore  # noqa: E402

LOG = "hermes.advanced_providers"


def _jwt(exp: int) -> str:
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _store(tmpdir: str) -> AccountStore:
    os.environ["HERMES_HOME"] = tmpdir
    return AccountStore(Path(tmpdir) / ".advanced-custom-providers.json")


class _FakeClient:
    def __init__(self) -> None:
        self.event_hooks = {"request": []}

    def close(self) -> None:
        pass


class TestMessageFormat(unittest.TestCase):
    def test_takeover_line_names_implementation_and_server(self):
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.takeover(url="https://chatgpt.com/backend-api/codex/responses?x=1",
                           platform="codex", account="a@b.c", phase="inference",
                           requested="cpa", effective="cpa", model="gpt-6-luna",
                           stream=True)
        line = cap.output[0]
        self.assertIn("OAuth实现已接管请求", line)
        self.assertIn("「CPA移植」", line)
        self.assertIn("https://chatgpt.com", line)
        self.assertIn("模型=gpt-6-luna", line)
        self.assertIn("流式=是", line)
        self.assertIn("阶段=inference", line)

    def test_takeover_line_reports_fallback_reason(self):
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.takeover(url="https://cloudcode-pa.googleapis.com/v1internal:generateContent",
                           platform="antigravity", account="a@b.c", phase="inference",
                           requested="hermes", effective="cpa", model="gemini-3-flash",
                           stream=False, skipped=["hermes:no-identity"])
        line = cap.output[0]
        self.assertIn("「CPA移植」", line)
        self.assertIn("指纹=hermes→cpa", line)
        self.assertIn("回退原因：hermes:no-identity", line)
        self.assertIn("流式=否", line)

    def test_takeover_line_without_fallback_has_no_arrow(self):
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.takeover(url="https://api.anthropic.com/v1/messages", platform="claude",
                           account="a@b.c", phase="inference", requested="sub2api",
                           effective="sub2api", model="claude-fable-5-1", stream=True)
        line = cap.output[0]
        self.assertIn("指纹=sub2api ", line)
        self.assertNotIn("→", line.split("指纹=")[1])
        self.assertIn("「Sub2API移植」", line)

    def test_stream_inferred_from_gemini_endpoint(self):
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.takeover(url="https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent",
                           platform="antigravity", account="a@b.c", phase="inference",
                           requested="cpa", effective="cpa", model="gemini-3-flash")
        self.assertIn("流式=是", cap.output[0], "gemini signals streaming by endpoint")

    def test_proxy_line_redacts_credentials_and_logs_once(self):
        oplog._PROXY_LOGGED.clear()
        proxy = "http://user:secret@127.0.0.1:7890"
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.proxy_injected(proxy, platform="codex", account="a@b.c", scope="推理链路")
            oplog.proxy_injected(proxy, platform="codex", account="a@b.c", scope="推理链路")
        self.assertEqual(len(cap.output), 1, "proxy is a client-level fact: log once")
        line = cap.output[0]
        self.assertIn("成功注入代理服务器", line)
        self.assertIn("127.0.0.1:7890", line)
        self.assertNotIn("secret", line)
        self.assertIn("作用域=推理链路", line)

    def test_proxy_skipped_is_a_warning(self):
        oplog._PROXY_LOGGED.clear()
        with self.assertLogs(LOG, level="WARNING") as cap:
            oplog.proxy_skipped("http://127.0.0.1:7890", platform="claude",
                                scope="推理链路", reason="核心客户端不接受 proxy 参数")
        self.assertIn("未能注入", cap.output[0])
        self.assertIn("原因=核心客户端不接受 proxy 参数", cap.output[0])

    def test_client_ready_once(self):
        oplog._CLIENT_LOGGED.clear()
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.client_ready(platform="antigravity", account="a@b.c", requested="hermes",
                               effective="cpa", proxy="", timeout=300)
            oplog.client_ready(platform="antigravity", account="a@b.c", requested="hermes",
                               effective="cpa", proxy="", timeout=300)
        self.assertEqual(len(cap.output), 1)
        self.assertIn("OAuth客户端就绪", cap.output[0])
        self.assertIn("指纹=hermes→cpa", cap.output[0])
        self.assertIn("代理=直连", cap.output[0])

    def test_refresh_ok_line(self):
        with self.assertLogs(LOG, level="INFO") as cap:
            oplog.refresh_ok(platform="antigravity", account="a@b.c",
                             proxy="http://127.0.0.1:7890")
        self.assertIn("OAuth令牌刷新成功", cap.output[0])
        self.assertIn("经代理=http://127.0.0.1:7890", cap.output[0])


class TestHookWiring(unittest.TestCase):
    """The format is worthless if the request path never calls it."""

    def test_proxy_log_and_takeover_fire_on_a_real_request(self):
        oplog._PROXY_LOGGED.clear()
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("antigravity", "me@x.com",
                      {"access_token": _jwt(9999999999), "refresh_token": "RT"})
            ctx = oauth_client.OAuthTokenContext("antigravity", "me@x.com", store)
            with self.assertLogs(LOG, level="INFO") as cap:
                client = oauth_client.build_oauth_http_client(
                    ctx, platform="antigravity", fingerprint="hermes",
                    proxy="http://127.0.0.1:7890",
                    base_client_factory=lambda **kw: _FakeClient())
                hook = client.event_hooks["request"][0]
                hook(httpx.Request("POST",
                                   "https://cloudcode-pa.googleapis.com/v1internal:generateContent",
                                   json={"model": "gemini-3-flash", "stream": True},
                                   headers={"Authorization": "Bearer stale"}))
        joined = "\n".join(cap.output)
        self.assertIn("成功注入代理服务器", joined)
        self.assertIn("OAuth客户端就绪", joined)
        self.assertIn("OAuth实现已接管请求", joined)
        self.assertIn("https://cloudcode-pa.googleapis.com", joined)
        self.assertIn("「CPA移植」", joined, "hermes has no antigravity identity → CPA")
        self.assertIn("模型=gemini-3-flash", joined)

    def test_garbage_body_still_logs_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("antigravity", "me@x.com",
                      {"access_token": _jwt(9999999999), "refresh_token": "RT"})
            ctx = oauth_client.OAuthTokenContext("antigravity", "me@x.com", store)
            client = oauth_client.build_oauth_http_client(
                ctx, platform="antigravity", fingerprint="cpa", proxy="",
                base_client_factory=lambda **kw: _FakeClient())
            hook = client.event_hooks["request"][0]
            with self.assertLogs(LOG, level="INFO"):
                hook(httpx.Request("POST", "https://example.invalid/x",
                                   content=b"not json",
                                   headers={"Authorization": "Bearer stale"}))

    def test_peek_json_body_ignores_oversized_payloads(self):
        big = httpx.Request("POST", "https://x/y", content=b"{" + b"a" * (5 * 1024 * 1024) + b"}")
        self.assertEqual(oauth_client._peek_json_body(big), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
