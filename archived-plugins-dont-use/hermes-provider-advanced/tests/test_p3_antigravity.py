"""P3.7 regression: antigravity inference wire (translate + parse + facade glue).

Offline only — no network. The live end-to-end (real account, gemini stream,
claude merge route, tool roundtrip) was verified manually 2026-10-29; these
lock the shapes that verification exercised.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advlib.antigravity_infer import (  # noqa: E402
    MODEL_CAPS, derive_session_id, model_family, translate_request, uses_stream_route,
)
from advlib.antigravity_client import strip_effort  # noqa: E402
from advlib.antigravity_resp import (  # noqa: E402
    StreamState, _Usage, iter_sse_json, merge_stream_to_nonstream, nonstream_to_chat,
)


def _kw():
    return {
        "model": "gemini-3-flash",
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "read a.txt"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"output":"ok"}'},
            {"role": "user", "content": "done"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "read_file", "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"]}}}],
        "max_tokens": 2048, "temperature": 0.2, "reasoning_effort": "high",
    }


class TestRequestTranslate(unittest.TestCase):
    def test_envelope_fields(self):
        env, nm = translate_request(_kw(), project="pa-x", model="gemini-3-flash",
                                    session_id="-123")
        self.assertEqual(env["project"], "pa-x")
        self.assertEqual(env["userAgent"], "antigravity")
        self.assertEqual(env["requestType"], "agent")
        self.assertTrue(env["requestId"].startswith("agent-"))
        self.assertEqual(env["request"]["sessionId"], "-123")
        self.assertEqual(nm, {"read_file": "read_file"})

    def test_gemini_family_rules(self):
        env, _ = translate_request(_kw(), project="p", model="gemini-3-flash",
                                   session_id="-1")
        req = env["request"]
        # maxOutputTokens deleted for gemini, kept for claude (spec §8)
        self.assertNotIn("maxOutputTokens", req.get("generationConfig", {}))
        self.assertEqual(req["generationConfig"]["thinkingConfig"]["thinkingLevel"], "high")
        roles = [c["role"] for c in req["contents"]]
        self.assertEqual(roles, ["user", "model", "user", "user"])
        decl = req["tools"][0]["functionDeclarations"][0]
        self.assertIn("parameters", decl)
        self.assertNotIn("parametersJsonSchema", decl)

    def test_claude_family_rules(self):
        env, _ = translate_request({**_kw(), "model": "claude-sonnet-4-6"},
                                   project="p", model="claude-sonnet-4-6", session_id="-1")
        req = env["request"]
        self.assertEqual(req["generationConfig"]["maxOutputTokens"], 2048)
        self.assertEqual(req["toolConfig"]["functionCallingConfig"]["mode"], "VALIDATED")
        self.assertTrue(uses_stream_route("claude-sonnet-4-6"))
        self.assertFalse(uses_stream_route("gemini-3-flash"))

    def test_system_instruction_role_user(self):
        env, _ = translate_request(_kw(), project="p", model="gemini-3-flash", session_id="-1")
        si = env["request"]["systemInstruction"]
        self.assertEqual(si["role"], "user")
        self.assertEqual(si["parts"][0]["text"], "Be terse.")

    def test_function_response_kept_as_string(self):
        env, _ = translate_request(_kw(), project="p", model="gemini-3-flash", session_id="-1")
        fr = [p for c in env["request"]["contents"] for p in c["parts"] if "functionResponse" in p]
        self.assertEqual(fr[0]["functionResponse"]["response"]["result"], '{"output":"ok"}')

    def test_registry_cap_claude(self):
        kw = {**_kw(), "model": "claude-opus-5-5-high", "max_tokens": 999999}
        env, _ = translate_request(kw, project="p", model="claude-opus-5-5-high", session_id="-1")
        self.assertEqual(env["request"]["generationConfig"]["maxOutputTokens"],
                         MODEL_CAPS["claude-opus-5-5-high"])


class TestSessionId(unittest.TestCase):
    def test_negative_int64_stable(self):
        a = derive_session_id("gemini-3-flash:hello")
        b = derive_session_id("gemini-3-flash:hello")
        c = derive_session_id("gemini-3-flash:other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("-"))
        self.assertLess(abs(int(a)), 2**63)


class TestEffortSuffix(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(strip_effort("gemini-3-flash(high)"), ("gemini-3-flash", "high"))
        self.assertEqual(strip_effort("gemini-3-flash"), ("gemini-3-flash", ""))


class TestSSEParser(unittest.TestCase):
    def test_multi_line_json_frame(self):
        frames = [b'data: {"response":{"candidates":[{"content":{"role":"model","parts":[{"text":" split\n',
                  b'across"}]}}]},"traceId":"t"}\n\n']
        envs = list(iter_sse_json(iter(frames)))
        self.assertEqual(len(envs), 1)
        text = envs[0]["response"]["candidates"][0]["content"]["parts"][0]["text"]
        self.assertEqual(text, " split\nacross")

    def test_read_boundary_split(self):
        line = b'data: {"a":1}\n\ndata: {"b":2}\n\n'
        half = len(line) // 2
        envs = list(iter_sse_json(iter([line[:half], line[half:]])))
        self.assertEqual(envs, [{"a": 1}, {"b": 2}])

    def test_control_chars_tolerated(self):
        # gjson-accepted, strict-json-rejected: raw control char inside string
        frames = [b'data: {"v":"x\ty"}\n\n']
        envs = list(iter_sse_json(iter(frames)))
        self.assertEqual(envs, [{"v": "x\ty"}])


class TestStreamState(unittest.TestCase):
    FRAMES = [
        {"response": {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}],
                      "modelVersion": "gemini-3.8-flash", "responseId": "resp-123"}},
        {"response": {"candidates": [{"content": {"parts": [
            {"text": "think", "thought": True, "thoughtSignature": "sig"}]}}]}},
        {"response": {"candidates": [{"content": {"parts": [
            {"functionCall": {"id": "", "name": "calculator", "args": {"expr": "2+2"}}}]}}]}},
        {"response": {"candidates": [{"content": {"parts": [{"thoughtSignature": "only"}]},
                                      "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                                        "thoughtsTokenCount": 3, "totalTokenCount": 18}}},
    ]

    def _collect(self):
        st = StreamState(model="gemini-3.8-flash", name_map={"calculator": "calculator"})
        chunks = []
        for env in self.FRAMES:
            chunks.extend(st.feed(env))
        chunks.extend(st.finalize())
        return chunks

    def test_delta_shapes(self):
        chunks = self._collect()
        self.assertEqual([c.choices[0].delta.content for c in chunks if c.choices[0].delta.content],
                         ["Hello"])
        self.assertEqual([c.choices[0].delta.reasoning_content for c in chunks
                          if c.choices[0].delta.reasoning_content], ["think"])

    def test_tool_call_synthesis(self):
        chunks = self._collect()
        tcs = [tc for c in chunks for tc in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(len(tcs), 1)
        self.assertGreater(len(tcs[0].id), 10)      # synthesized for empty upstream id
        self.assertEqual(tcs[0].function.name, "calculator")
        self.assertEqual(json.loads(tcs[0].function.arguments), {"expr": "2+2"})

    def test_finish_and_usage(self):
        chunks = self._collect()
        fin = [c for c in chunks if c.choices[0].finish_reason]
        self.assertEqual(fin[0].choices[0].finish_reason, "tool_calls")  # saw tool call
        u = fin[0].usage
        self.assertEqual(u.completion_tokens, 8)   # candidates + thoughts
        self.assertEqual(u.total_tokens, 18)
        self.assertEqual(u["prompt_tokens"], 10)   # dict-style access
        self.assertEqual(u.completion_tokens_details.reasoning_tokens, 3)

    def test_thought_signature_only_ignored(self):
        st = StreamState(model="m")
        frame = {"response": {"candidates": [{
            "content": {"parts": [{"thoughtSignature": "sig-only"}]}}]}}
        out = st.feed(frame)
        self.assertTrue(all(not (c.choices[0].delta.content or c.choices[0].delta.reasoning_content)
                            for c in out))

    def test_in_band_error_raises_with_status(self):
        st = StreamState(model="m")
        with self.assertRaises(Exception) as cm:
            st.feed({"error": {"code": 429, "message": "quota"}})
        self.assertEqual(cm.exception.status_code, 429)

    def test_empty_finalize_raises(self):
        st = StreamState(model="m")
        with self.assertRaises(RuntimeError):
            st.finalize()


class TestNonStream(unittest.TestCase):
    def test_gemini_nonstream(self):
        data = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2,
                                  "totalTokenCount": 3}}
        obj = nonstream_to_chat(data, model="gemini-3-flash")
        self.assertEqual(obj.choices[0].message.content, "hi")
        self.assertEqual(obj.usage.total_tokens, 3)

    def test_claude_merge(self):
        envs = [{"response": {"candidates": [{"content": {"parts": [{"text": "A"}]}}]}},
                {"response": {"candidates": [{"content": {"parts": [{"text": "B"}]},
                                              "finishReason": "STOP"}],
                              "usageMetadata": {"promptTokenCount": 1,
                                                "candidatesTokenCount": 2,
                                                "totalTokenCount": 3}}}]
        merged = merge_stream_to_nonstream(iter(envs), model="claude-x")
        self.assertEqual(merged["choices"][0]["message"]["content"], "AB")
        self.assertEqual(merged["usage"]["total_tokens"], 3)

    def test_name_map_restores_original(self):
        data = {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "my_tool", "args": {}}}]}}]}
        obj = nonstream_to_chat(data, model="m", name_map={"my_tool": "my.tool"})
        self.assertEqual(obj.choices[0].message.tool_calls[0].function.name, "my.tool")


class TestSchemaEngineIntegration(unittest.TestCase):
    """P3.5: the ported gemini_schema_clean engine replaced the stopgap cleaner.

    Every case goes through translate_request, so it proves the WIRING (which
    preset each family gets), not just the engine's own 139 unit tests.
    """

    def _tools(self, schema, model):
        kw = _kw()
        kw["model"] = model
        kw["tools"] = [{"type": "function",
                        "function": {"name": "t", "description": "d", "parameters": schema}}]
        env, _ = translate_request(kw, project="p", model=model, session_id="-1")
        return env["request"]["tools"][0]["functionDeclarations"][0]["parameters"]

    def test_claude_gets_reason_placeholder(self):
        params = self._tools({"type": "object", "properties": {}}, "claude-sonnet-4-6")
        self.assertEqual(params["properties"]["reason"]["description"],
                         "Brief explanation of why you are calling this tool")
        self.assertIn("reason", params["required"])

    def test_plain_gemini_gets_no_placeholder(self):
        params = self._tools({"type": "object", "properties": {}}, "gemini-3-flash")
        self.assertNotIn("reason", params.get("properties") or {})

    def test_gemini_3_pro_family_gets_placeholder(self):
        # useAntigravitySchema is true for gemini-3-pro / gemini-3.1-pro too
        params = self._tools({"type": "object", "properties": {}}, "gemini-3-pro")
        self.assertIn("reason", params.get("properties") or {})

    def test_local_ref_is_inlined(self):
        params = self._tools({
            "type": "object",
            "properties": {"p": {"$ref": "#/$defs/Path"}},
            "$defs": {"Path": {"type": "string", "description": "a path"}},
            "required": ["p"],
        }, "gemini-3-flash")
        self.assertNotIn("$ref", json.dumps(params))
        self.assertEqual(params["properties"]["p"]["type"], "string")

    def test_type_array_collapses_to_nullable(self):
        params = self._tools({
            "type": "object",
            "properties": {"p": {"type": ["string", "null"]}},
            "required": ["p"],
        }, "gemini-3-flash")
        prop = params["properties"]["p"]
        self.assertEqual(prop["type"], "string")
        self.assertTrue(prop.get("nullable"))

    def test_response_schema_keeps_union(self):
        kw = _kw()
        kw["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "x", "schema": {
                "type": "object",
                "properties": {"v": {"anyOf": [
                    {"type": "object", "properties": {"a": {"type": "string"}}},
                    {"type": "object", "properties": {"b": {"type": "string"}}}]}},
                "required": ["v"]}}}
        env, _ = translate_request(kw, project="p", model="gemini-3-flash", session_id="-1")
        schema = env["request"]["generationConfig"]["responseSchema"]
        self.assertIn("anyOf", json.dumps(schema),
                      "response-side schemas keep unions (CPA response preset)")

    def test_26_tool_corpus_is_clean(self):
        """The heavy real-world case: 26 declarations full of $refs, type
        arrays and enums must come out with no $ref / no type arrays."""
        tools = []
        props_tpl = {
            "path": {"$ref": "#/$defs/Path"},
            "mode": {"type": ["string", "null"], "enum": ["a", "b"]},
            "count": {"type": "integer", "minimum": 0},
            "nested": {"type": "object", "properties": {"x": {"type": "number"}}},
            "multi": {"anyOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"b": {"type": "number"}}}]},
        }
        for i in range(26):
            tools.append({"type": "function", "function": {
                "name": f"tool_{i}", "description": f"t{i}",
                "parameters": {"type": "object", "properties": dict(props_tpl),
                               "required": ["path"],
                               "$defs": {"Path": {"type": "string"}}}}})
        kw = _kw()
        kw["tools"] = tools
        for model in ("gemini-3-flash", "claude-sonnet-4-6"):
            env, _ = translate_request(kw, project="p", model=model, session_id="-1")
            decls = env["request"]["tools"][0]["functionDeclarations"]
            self.assertEqual(len(decls), 26)
            dumped = json.dumps(decls)
            self.assertNotIn("$ref", dumped)
            self.assertNotIn('"type": [', dumped)
            self.assertNotIn("enum", dumped)
            self.assertNotIn('"minimum"', dumped,
                             "constraints fold into description (CPA preset), keyword dropped")
            self.assertEqual("$defs" in dumped, False)
            # only the claude preset flattens MULTI-branch unions
            if "claude" in model:
                self.assertNotIn('"anyOf"', dumped,
                                 "claude parameter schemas flatten multi-branch unions")
            else:
                self.assertIn('"anyOf"', dumped,
                              "gemini parameter schemas keep multi-branch unions")


if __name__ == "__main__":
    unittest.main(verbosity=2)
