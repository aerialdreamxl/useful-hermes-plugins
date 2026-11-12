"""paperhermes v0.5 离线单测（PROMPT.md 验收 6 + A9-A27 打桩集）。

运行（在本仓根）：python tests/test_paperhermes.py -v
注意：路径含非 ASCII 时 unittest discover 可能失败，直接执行本文件即可。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))   # tests/ -> paperhermes/ -> 仓根（包名的父目录）
sys.path.insert(0, ROOT)
import paperhermes as mod  # noqa: E402
engine = mod.engine

LONG_DESC = ("Search the web for information. Use when you need current events, "
             "facts, or any information beyond your knowledge cutoff. " + "细节补充。" * 40)
ZH_DESC = "搜索网络获取信息。用于查询时事、事实与知识截止后的内容。" + "补充细节。" * 40
ZH_NOTICE = "部分工具描述已折叠以节省上下文；需要完整说明时用 tool_search 搜索、tool_describe 读取。"


class TestFold(unittest.TestCase):
    def test_fold_first_sentence(self):
        out = engine.fold_description(LONG_DESC, 120)
        self.assertTrue(out.startswith("Search the web for information."))
        self.assertLessEqual(len(out), 120)

    def test_fold_cjk_boundary_no_space(self):
        # v0.4.1：中文句号无尾随空白也要折叠
        self.assertEqual(engine.fold_description(ZH_DESC, 120), "搜索网络获取信息。")

    def test_fold_ascii_no_space_untouched(self):
        self.assertEqual(engine.fold_description("No whitespace.Next sentence", 120),
                         "No whitespace.Next sentence")

    def test_fold_newline_boundary(self):
        self.assertEqual(engine.fold_description("Line one\nLine two", 120), "Line one")

    def test_fold_idempotent(self):
        a = engine.fold_description(LONG_DESC, 120)
        self.assertEqual(engine.fold_description(a, 120), a)

    def test_short_desc_untouched(self):
        self.assertEqual(engine.fold_description("Short.", 120), "Short.")

    def test_lang_table_ordered_first_match(self):
        self.assertEqual(engine.pick_notice_lang("qwen3.8-flash"), "zh")
        self.assertEqual(engine.pick_notice_lang("GPT-4o"), "en")
        self.assertEqual(engine.pick_notice_lang("Qwen-DeepSeek-X"), "zh")
        self.assertEqual(engine.pick_notice_lang("unknown-llm"), "en")
        self.assertEqual(engine.pick_notice_lang(""), "en")


class TestGate(unittest.TestCase):
    def test_net_gate_both_sides(self):
        self.assertFalse(engine.net_gate(2047, 0, 2048))
        self.assertTrue(engine.net_gate(2049, 0, 2048))
        self.assertTrue(engine.net_gate(2048 + len(ZH_NOTICE), len(ZH_NOTICE), 2048))
        self.assertFalse(engine.net_gate(2047 + len(ZH_NOTICE), len(ZH_NOTICE), 2048))


class TestFoldTools(unittest.TestCase):
    def _tools(self):
        return [
            {"type": "function", "function": {"name": "web_search", "description": LONG_DESC,
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "clarify", "description": LONG_DESC}},
            {"type": "function", "function": {"name": "unregistered_mcp",
                                              "description": LONG_DESC}},
        ]

    def test_only_exempt_kept(self):
        tools, n, saved = engine.fold_tools(self._tools(), "chat_completions", 120, ())
        self.assertEqual(n, 2)
        self.assertGreater(saved, 100)
        self.assertEqual(tools[1]["function"]["description"], LONG_DESC)
        self.assertEqual(tools[0]["function"]["parameters"], {"type": "object"})
        self.assertEqual(self._tools()[0]["function"]["description"], LONG_DESC)

    def test_anthropic_shape(self):
        td = {"name": "web_search", "description": LONG_DESC, "input_schema": {"type": "object"}}
        tools, n, _ = engine.fold_tools([td], "anthropic_messages", 120, ())
        self.assertEqual(n, 1)
        self.assertIn("input_schema", tools[0])

    def test_80pct_no_value(self):
        tools, n, _ = engine.fold_tools(
            [{"function": {"name": "web_search", "description": "x" * 100}}],
            "chat_completions", 120, ())
        self.assertEqual(n, 0)

    def test_user_keep_eager(self):
        tools, n, _ = engine.fold_tools(self._tools(), "chat_completions", 120,
                                        ("web_search",))
        self.assertEqual(n, 1)

    def test_rule_bearing_tools_are_never_folded(self):
        # 2026-11-10 实机审查：安全/纪律规则写在首句之后，一律豁免
        tools = [{"type": "function", "function": {"name": nm, "description": LONG_DESC}}
                 for nm in ("browser_vault", "browser_exec", "delegate_task",
                            "skill_manage", "manage_connections", "memory")]
        _, n, saved = engine.fold_tools(tools, "chat_completions", 120, ())
        self.assertEqual((n, saved), (0, 0))


class TestParamFolds(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["query"],
              "properties": {
                  "query": {"type": "string",
                            "description": "搜索关键词。" + "要具体。" * 30},
                  "opts": {"type": "object",
                           "description": "可选参数集合。" + "控制行为。" * 30,
                           "properties": {
                               "limit": {"type": "integer", "default": 5,
                                         "description": "返回条数上限。" + "越大越全。" * 30}}}}}

    def test_collect_and_apply_structure_preserved(self):
        folds, saved = engine.collect_param_folds(self.SCHEMA, 120)
        self.assertIn("query", folds)
        self.assertIn("opts", folds)
        self.assertIn("limit", folds)
        self.assertGreater(saved, 100)
        new = engine.apply_param_folds(self.SCHEMA, folds)
        # 结构守恒：剔掉 description 后逐字节相等
        def strip_d(node):
            if isinstance(node, dict):
                return {k: strip_d(v) for k, v in node.items() if k != "description"}
            if isinstance(node, list):
                return [strip_d(x) for x in node]
            return node
        self.assertEqual(strip_d(new), strip_d(self.SCHEMA))
        self.assertEqual(new["required"], ["query"])
        self.assertEqual(new["properties"]["opts"]["properties"]["limit"]["default"], 5)
        self.assertLess(len(new["properties"]["query"]["description"]),
                        len(self.SCHEMA["properties"]["query"]["description"]))
        # 原对象未改
        self.assertGreater(len(self.SCHEMA["properties"]["query"]["description"]), 100)

    def test_keep_param_desc(self):
        folds, _ = engine.collect_param_folds(self.SCHEMA, 120, frozenset({"query"}))
        self.assertNotIn("query", folds)
        self.assertIn("limit", folds)

    def test_split_entries(self):
        per = engine.split_keep_param_entries(("web_search.query", "bad", "a.b.c"))
        self.assertEqual(per, {"web_search": {"query"}, "a": {"b.c"}})


class TestSkillsBlock(unittest.TestCase):
    TEXT = ("intro\n## Skills\n\n- solo-skill: desc one\n  catX:\n    - a: keep me long enough\n"
            "    - b: keep me too\n  catY:\n    - c: fold me if not kept\n"
            "## Other\n- not: in skills\n")

    def test_name_only_mode(self):
        out, n, saved = engine.fold_skills_block(self.TEXT, frozenset({"a", "b", "solo-skill"}))
        self.assertGreater(n, 0)
        self.assertIn("- solo-skill: desc one", out)
        self.assertIn("- a: keep me long enough", out)
        self.assertIn("- c", out)
        self.assertNotIn("- c: fold me if not kept", out)
        self.assertIn("- not: in skills", out)

    def test_trigger_only_mode(self):
        # M7：保留触发句（首句），省量少于 name-only 但召回线索保留
        text = "## Skills\n\n  cat:\n    - s: " + ("触发语在这里。" + "长尾描述。" * 30) + "\n"
        out, n, _ = engine.fold_skills_block(text, frozenset(), trigger_only=True)
        self.assertEqual(n, 1)
        self.assertIn("- s: 触发语在这里。", out)
        self.assertNotIn("长尾描述", out)

    def test_idempotent_and_noop(self):
        out1, n1, _ = engine.fold_skills_block(self.TEXT, frozenset())
        out2, n2, _ = engine.fold_skills_block(out1, frozenset())
        self.assertEqual(out1, out2)
        self.assertEqual(n2, 0)
        no_head, n3, _s = engine.fold_skills_block("plain prompt", frozenset())
        self.assertEqual((no_head, n3), ("plain prompt", 0))


class TestNoticeInjection(unittest.TestCase):
    def test_chat_completions_newline_no_glue(self):
        # v0.4.1：告知句另起一行，不黏连正文
        req = {"messages": [{"role": "system", "content": "base。"},
                            {"role": "user", "content": "hi"}]}
        out, added = engine.system_slot_apply("chat_completions", req, "TOOLS NOTICE", None)
        self.assertIn("\nTOOLS NOTICE", out["messages"][0]["content"])
        self.assertGreater(added, 0)
        out2, added2 = engine.system_slot_apply("chat_completions", out, "TOOLS NOTICE", None)
        self.assertEqual(added2, 0)

    def test_anthropic_str(self):
        out, added = engine.system_slot_apply("anthropic_messages", {"system": "base"},
                                              "T-N", "S-N")
        self.assertIn("\nT-N", out["system"])
        self.assertIn("S-N", out["system"])

    def test_anthropic_block_list_lands_last_plain_block(self):
        blocks = [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": "tail no marker"}]
        out, added = engine.system_slot_apply("anthropic_messages", {"system": blocks},
                                              None, "S-N")
        self.assertIn("S-N", out["system"][1]["text"])
        self.assertNotIn("S-N", out["system"][0]["text"])
        self.assertIn("cache_control", out["system"][0])
        only = [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]
        out2, added2 = engine.system_slot_apply("anthropic_messages", {"system": only},
                                                None, "S-N")
        self.assertEqual(added2, 0)
        self.assertEqual(out2["system"], only)

    def test_empty_system_untouched(self):
        out, added = engine.system_slot_apply("anthropic_messages", {"system": None}, "T", "S")
        self.assertEqual(added, 0)


class TestSnapshot(unittest.TestCase):
    ST = {"fold_tool_desc": True, "fold_tool_param_desc": True, "fold_skills_index": True,
          "fold_skills_trigger_only": False, "keep_eager": (), "keep_param_desc": (),
          "fold_first_sentence_max_chars": 120, "min_savings_chars": 16}

    def _request(self):
        return {
            "system": "core prompt\n## Skills\n\n  cooking:\n"
                      "    - recipe-x: long description here for folding test purposes only, "
                      "kept deliberately verbose so the fold saves more than the notice line "
                      "costs and the net-gain gate keeps it\n",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "web_search", "description": LONG_DESC,
                       "input_schema": TestParamFolds.SCHEMA}],
        }

    def test_plan_apply_byte_stable_and_new_tool_untouched(self):
        req = self._request()
        plan = engine.plan_session(req, "anthropic_messages", self.ST, "qwen3.8-flash",
                                   frozenset())
        self.assertTrue(plan["act"])
        self.assertIn("web_search", plan["folded_tools"])
        self.assertIn("query", plan["folded_params"]["web_search"])
        self.assertIn("recipe-x", plan["folded_skills"])
        snap = engine.snapshot_from_plan("s1", "anthropic_messages", self.ST, plan)
        out1, c1, _ = engine.apply_snapshot(req, snap, "anthropic_messages")
        out2, c2, _ = engine.apply_snapshot(self._request(), snap, "anthropic_messages")
        self.assertEqual(json.dumps(out1, ensure_ascii=False, sort_keys=True),
                         json.dumps(out2, ensure_ascii=False, sort_keys=True))  # 字节稳定
        self.assertGreater(c1["tools"], 0)
        self.assertGreater(c1["params"], 0)
        self.assertGreater(c1["skills"], 0)
        # 新工具不加入折叠集；消失的工具不补回
        req2 = self._request()
        req2["tools"].append({"name": "brand_new", "description": LONG_DESC,
                              "input_schema": {"type": "object"}})
        out3, c3, _ = engine.apply_snapshot(req2, snap, "anthropic_messages")
        new_td = [t for t in out3["tools"] if t["name"] == "brand_new"][0]
        self.assertEqual(new_td["description"], LONG_DESC)
        # 源描述改变也用快照里的旧文本（不重折）
        req3 = self._request()
        req3["tools"][0]["description"] = LONG_DESC + "CHANGED " + "改动。" * 50
        out4, _, _ = engine.apply_snapshot(req3, snap, "anthropic_messages")
        self.assertEqual(out4["tools"][0]["description"], out1["tools"][0]["description"])

    def test_protocol_mismatch_bypass(self):
        plan = engine.plan_session(self._request(), "anthropic_messages", self.ST, "qwen",
                                   frozenset())
        snap = engine.snapshot_from_plan("s2", "anthropic_messages", self.ST, plan)
        out, counts, _ = engine.apply_snapshot(self._request(), snap, "chat_completions")
        self.assertIsNone(out)
        self.assertTrue(counts["protocol_mismatch"])

    def test_inert_and_melted_never_fold(self):
        snap = engine.inert_snapshot("s3", "anthropic_messages", self.ST, "qwen")
        out, _, _ = engine.apply_snapshot(self._request(), snap, "anthropic_messages")
        self.assertIsNone(out)
        plan = engine.plan_session(self._request(), "anthropic_messages", self.ST, "qwen",
                                   frozenset())
        snap2 = engine.snapshot_from_plan("s4", "anthropic_messages", self.ST, plan)
        snap2["melted"] = True
        out2, _, _ = engine.apply_snapshot(self._request(), snap2, "anthropic_messages")
        self.assertIsNone(out2)

    def test_sanitize_bad_snapshot(self):
        self.assertIsNone(engine.sanitize_snapshot("junk"))
        self.assertIsNone(engine.sanitize_snapshot({"no_decision": 1}))
        ok = engine.sanitize_snapshot({"session_id": "s", "decision": {"act": False}})
        self.assertIsNotNone(ok)


class FakeCtx:
    """打桩 PluginContext 最小面。"""
    def __init__(self, settings):
        self._s = dict(settings)
        self.seed_sink = {}
        self.logs = []
        self.hooks = {}
        self.mw = None
        self.cmd_handler = None
        self.cli_commands = {}
        self.aux_tasks = {}
        self.llm = None
    def get_config(self, key, default=None):
        return self._s.get(key, default)
    def set_config(self, key, value):
        self.seed_sink[key] = value
        self._s[key] = value
    def register_middleware(self, kind, cb):
        self.mw = (kind, cb)
    def register_hook(self, name, cb):
        self.hooks[name] = cb
    def register_command(self, name, handler=None, **kw):
        self.cmd_handler = handler
    def register_cli_command(self, name, help="", setup_fn=None, handler_fn=None, **kw):
        self.cli_commands[name] = (setup_fn, handler_fn)
    def register_auxiliary_task(self, key, **kw):
        self.aux_tasks[key] = kw
    def log(self, level, msg):
        self.logs.append((level, msg))


class FakeLlmResult:
    def __init__(self, text):
        self.text = text


class FakeLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
    def complete(self, messages=None, **kw):
        self.calls.append((messages, kw))
        return FakeLlmResult(self.replies.pop(0) if self.replies else "{}")


class WiringBase(unittest.TestCase):
    REQUEST = {
        "model": "qwen3.8-flash",
        "system": "core prompt\n## Skills\n\n  cooking:\n"
                  "    - recipe-x: long description here for folding test purposes only\n",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "web_search", "description": LONG_DESC,
                   "input_schema": TestParamFolds.SCHEMA}],
        "max_tokens": 1000,
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ph_v5_")
        os.environ["HERMES_HOME"] = self.tmp
        mod._observer = None
        mod._snapshots = {}
        mod._snapshot_bad = set()
        mod._session_signals = {}
        mod._last_payload_tools = []
        mod._freezes_loaded = False
        mod._tool_originals = {}
        mod._compact_store = None
        mod._compact_loaded = False
        mod._rearm_pending = {}
        mod._switch_counts = {}
        # 默认不让 backfill 真写（面板/请求路径不应写配置；backfill 有专测）
        self._orig_probe = mod._settings_keys_present
        mod._settings_keys_present = lambda: set(mod.DEFAULTS)

    def tearDown(self):
        mod._settings_keys_present = self._orig_probe

    def _mk(self, settings):
        ctx = FakeCtx(settings)
        mod.register(ctx)
        return ctx

    def _req(self):
        return json.loads(json.dumps(self.REQUEST))

    def _call(self, ctx, sid="s1", req=None, model="qwen3.8-flash",
              mode="anthropic_messages", arid="r1"):
        return ctx.mw[1](kind="llm_request", request=req or self._req(), api_mode=mode,
                         session_id=sid, model=model, provider="p", api_request_id=arid)


class TestFreezeContract(WiringBase):
    ON = {"observe": False, "fold_tool_desc": True, "fold_tool_param_desc": True,
          "fold_skills_index": True, "min_savings_chars": 16}

    def test_all_off_zero_touch_and_inert_snapshot(self):
        ctx = self._mk({"observe": False})
        self.assertIsNone(self._call(ctx))
        snap = mod._snapshots.get("s1")
        self.assertIsNotNone(snap)                 # OFF 状态也冻结
        self.assertFalse(snap["decision"]["act"])
        self.assertTrue(os.path.exists(mod._freezes_path()))
        # 全关无"介入自检行"（tools↓），但要有可诊断的 OFF 冻结告警
        self.assertFalse(any("tools↓" in m for _l, m in ctx.logs))
        self.assertTrue(any("frozen OFF" in m for _l, m in ctx.logs))

    def test_fold_freeze_and_config_immunity(self):
        ctx = self._mk(dict(self.ON))
        res = self._call(ctx)
        self.assertIsInstance(res, dict)
        new = res["request"]
        self.assertLess(len(new["tools"][0]["description"]), len(LONG_DESC))
        self.assertIn("部分工具描述已折叠", new["system"])
        sch = new["tools"][0]["input_schema"]
        self.assertLess(len(sch["properties"]["query"]["description"]), 120)
        self.assertEqual(sch["required"], ["query"])           # 结构守恒
        self.assertTrue(any("[PAPERHERMES]" in m for _l, m in ctx.logs))  # 自检行
        # 第二请求同字节（冻结）
        res2 = self._call(ctx, arid="r2")
        self.assertEqual(json.dumps(res2["request"], ensure_ascii=False, sort_keys=True),
                         json.dumps(new, ensure_ascii=False, sort_keys=True))
        # 会话中途改配置（双向切换纪律）：输出不得变
        ctx._s["fold_tool_desc"] = False
        ctx._s["fold_first_sentence_max_chars"] = 40
        res3 = self._call(ctx, arid="r3")
        self.assertEqual(json.dumps(res3["request"], ensure_ascii=False, sort_keys=True),
                         json.dumps(new, ensure_ascii=False, sort_keys=True))

    def test_commit_on_post_api_request_and_reload_restore(self):
        ctx = self._mk(dict(self.ON))
        res = self._call(ctx)
        snap = mod._snapshots["s1"]
        self.assertFalse(snap["committed"])
        ctx.hooks["post_api_request"](session_id="s1", api_request_id="r1",
                                      model="qwen3.8-flash", usage={"input_tokens": 10})
        self.assertTrue(snap["committed"])
        # 模拟热重载：清内存、关配置，从盘上恢复快照 → 同形态
        mod._snapshots = {}
        mod._freezes_loaded = False
        ctx._s["fold_tool_desc"] = False
        res2 = self._call(ctx, arid="r9")
        self.assertIsNotNone(res2)
        self.assertEqual(json.dumps(res2["request"], ensure_ascii=False, sort_keys=True),
                         json.dumps(res["request"], ensure_ascii=False, sort_keys=True))
        self.assertTrue(mod._snapshots["s1"]["committed"])

    def test_gate_blocks_tiny(self):
        ctx = self._mk({"observe": False, "fold_tool_desc": True, "min_savings_chars": 2048})
        req = {"system": "tiny", "messages": [{"role": "user", "content": "x"}],
               "tools": [{"name": "web_search", "description": "one two three four five six "
                          "seven eight nine ten.", "input_schema": {}}]}
        self.assertIsNone(self._call(ctx, sid="s4", req=req, model="gpt-4o"))
        self.assertNotIn("notice", json.dumps(req))

    def test_bypass_unknown_mode(self):
        ctx = self._mk(dict(self.ON))
        self.assertIsNone(self._call(ctx, sid="s5", mode="codex_responses"))

    def test_error_breaker_suggests_next_session_only(self):
        ctx = self._mk(dict(self.ON))
        self._call(ctx)
        snap = mod._snapshots["s1"]
        for _ in range(5):
            mod._record_error("s1", RuntimeError("x"))
        self.assertTrue(snap["suggest_disable_next"])
        res = self._call(ctx, arid="r6")   # 形态不变（本会话不熔断）
        self.assertIsNotNone(res)


class TestMeltdown(WiringBase):
    ON = {"observe": False, "fold_tool_desc": True, "min_savings_chars": 16}

    def _meltdowned(self, ctx, sid="s1"):
        return ctx.cmd_handler("meltdown", session_id=sid)

    def test_meltdown_passthrough_and_persistence(self):
        ctx = self._mk(dict(self.ON))
        self.assertIsNotNone(self._call(ctx))           # 先冻结 ON 形态
        out = self._meltdowned(ctx)
        self.assertIn("急停", out)
        self.assertTrue(mod._snapshots["s1"]["melted"])
        self.assertTrue(any("meltdown engaged" in m for _l, m in ctx.logs))
        # 熔断后原样透传
        self.assertIsNone(self._call(ctx, arid="r2"))
        # 不可撤销：再次触发只报告状态
        out2 = self._meltdowned(ctx)
        self.assertIn("已处于", out2)
        # 模拟热重载：从盘恢复仍熔断
        mod._snapshots = {}
        mod._freezes_loaded = False
        self.assertIsNone(self._call(ctx, arid="r3"))
        self.assertTrue(mod._snapshots["s1"]["melted"])
        # settings 零写入
        self.assertEqual(ctx.seed_sink, {})

    def test_melted_session_does_not_affect_others(self):
        ctx = self._mk(dict(self.ON))
        self._meltdowned(ctx, "sA")
        res = self._call(ctx, sid="sB", arid="r5")      # 新会话正常折叠
        self.assertIsNotNone(res)

    def test_model_cannot_trigger(self):
        # 无任何工具注册面：register 只挂 middleware/hooks/command
        ctx = self._mk(dict(self.ON))
        self.assertEqual(sorted(ctx.hooks),
                         ["on_session_end", "post_api_request", "post_tool_call",
                          "transform_tool_result"])


class TestResultProcessing(WiringBase):
    def _tool_transform(self, ctx, sid, tool, result):
        return ctx.hooks["transform_tool_result"](tool_name=tool, args={}, result=result,
                                                  session_id=sid)

    def test_melted_and_missing_snapshot_passthrough(self):
        ctx = self._mk({"observe": False, "minify_json_tool_results": True})
        self.assertIsNone(self._tool_transform(ctx, "no-such", "web_search",
                                               json.dumps({"a": 1}, indent=4)))

    def test_minify_and_noise(self):
        ctx = self._mk({"observe": False, "fold_tool_desc": True,
                        "minify_json_tool_results": True,
                        "strip_terminal_progress_noise": True})
        self._call(ctx)  # 定型快照（settings 入快照）
        pretty = json.dumps({"items": [1, 2, 3]}, indent=4)
        out = self._tool_transform(ctx, "s1", "web_search", pretty)
        self.assertEqual(json.loads(out), {"items": [1, 2, 3]})
        self.assertLess(len(out), len(pretty))
        noisy = "\r10%\r50%\r100% done\nsame\nsame\nlast"
        out2 = self._tool_transform(ctx, "s1", "browser", noisy)
        self.assertEqual(out2, "100% done\nsame\nlast")

    def test_terminal_never_touched(self):
        ctx = self._mk({"observe": False, "minify_json_tool_results": True})
        self._call(ctx)
        pretty = json.dumps({"a": [1, 2]}, indent=4)
        self.assertIsNone(self._tool_transform(ctx, "s1", "terminal", pretty))

    def test_spill_byte_recoverable(self):
        ctx = self._mk({"observe": False, "fold_tool_desc": True, "min_savings_chars": 16,
                        "spill_large_tool_results": True, "spill_min_chars": 1024,
                        "spill_head_chars": 100, "spill_tail_chars": 100})
        self._call(ctx)
        big = "H" * 100 + "M" * 5000 + "T" * 100
        out = self._tool_transform(ctx, "s1", "web_extract", big)
        self.assertIn("paperhermes", out)
        self.assertTrue(out.startswith("H" * 100))
        self.assertTrue(out.endswith("T" * 100))
        import re as _re
        m = _re.search(r"外置到 (.*?)[，,]", out)
        self.assertIsNotNone(m)
        with open(m.group(1).strip(), encoding="utf-8") as f:
            self.assertEqual(f.read(), big)   # 逐字节取回
        # 排除表内工具不外置
        ctx2 = self._mk({"observe": False, "spill_large_tool_results": True,
                         "spill_min_chars": 1024, "spill_exclude_tools": ["web_extract"]})
        self._call(ctx2, sid="s9")
        self.assertIsNone(self._tool_transform(ctx2, "s9", "web_extract", big))

    def test_path_sanitized(self):
        self.assertEqual(engine.safe_filename_piece("../../etc/passwd"), "etc_passwd")
        self.assertEqual(engine.safe_filename_piece(""), "x")


class TestBackfill(WiringBase):
    def test_only_missing_keys_written(self):
        mod._settings_keys_present = lambda: {"observe", "fold_tool_desc", "min_savings_chars"}
        ctx = FakeCtx({"observe": False, "fold_tool_desc": False, "min_savings_chars": 2048})
        mod._backfill_defaults(ctx)
        self.assertEqual(set(ctx.seed_sink), set(mod.DEFAULTS) -
                         {"observe", "fold_tool_desc", "min_savings_chars"})
        self.assertNotIn("observe", ctx.seed_sink)     # 显式 false 不被覆盖
        self.assertNotIn("fold_tool_desc", ctx.seed_sink)

    def test_probe_failure_no_write(self):
        mod._settings_keys_present = lambda: None
        ctx = FakeCtx({})
        mod._backfill_defaults(ctx)
        self.assertEqual(ctx.seed_sink, {})


class TestSwitchMatrix(WiringBase):
    def test_bool_coercion_matrix(self):
        for raw, expect in ((True, True), (False, False), ("true", True), ("off", False),
                            ("banana", False), (None, False), (1, True), (0, False)):
            ctx = FakeCtx({"fold_tool_desc": raw})
            self.assertEqual(mod._get(ctx, "fold_tool_desc"), expect, f"raw={raw!r}")

    def test_defaults_and_clamps(self):
        ctx = FakeCtx({})
        self.assertTrue(mod._get(ctx, "observe"))
        self.assertFalse(mod._get(ctx, "fold_tool_param_desc"))
        self.assertEqual(mod._get(ctx, "min_savings_chars"), 2048)
        self.assertEqual(mod._get(ctx, "spill_min_chars"), 20000)
        ctx2 = FakeCtx({"min_savings_chars": "abc"})
        self.assertEqual(mod._get(ctx2, "min_savings_chars"), 2048)
        ctx3 = FakeCtx({"keep_eager": "notalist"})
        self.assertEqual(mod._get(ctx3, "keep_eager"), ())


class TestObserveLog(WiringBase):
    def test_request_usage_and_notice_accounting(self):
        ctx = self._mk({"observe": True, "observe_composition": True,
                        "fold_tool_desc": True, "fold_tool_param_desc": True,
                        "min_savings_chars": 16})
        res = self._call(ctx)
        ctx.hooks["post_api_request"](session_id="s1", api_request_id="r1",
                                      model="qwen3.8-flash", usage={"input_tokens": 123})
        log = os.path.join(self.tmp, "paperhermes", "observe.log")
        with open(log, encoding="utf-8") as f:
            recs = [json.loads(x) for x in f if x.strip()]
        req = [r for r in recs if r["kind"] == "request"][-1]
        self.assertEqual(req["api_request_id"], "r1")
        self.assertIn("composition", req)
        self.assertGreater(req["saved_chars"], 0)
        # notice 实际入账（v0.4 已知欠账已修）
        if res and req.get("notice_chars", 0) == 0:
            # notice 由 apply 返回并传入 record
            pass
        self.assertGreater(req["notice_chars"], 0)
        self.assertGreater(req["params_folded"], 0)
        usage = [r for r in recs if r["kind"] == "usage"]
        self.assertEqual(usage[-1]["api_request_id"], "r1")

    def test_panel_and_stats_do_not_write(self):
        ctx = self._mk({"observe": True, "fold_tool_desc": True, "min_savings_chars": 16})
        self._call(ctx)
        out = ctx.cmd_handler("", session_id="s1")
        self.assertIn("冻结快照", out)
        st = ctx.cmd_handler("stats", session_id="s1")
        self.assertIn("账本", st)
        self.assertEqual(ctx.seed_sink, {})


class TestExceptionFailOpen(WiringBase):
    def test_bogus_request_untouched(self):
        ctx = self._mk({"observe": False, "fold_tool_desc": True})
        bogus = {"tools": [{"function": None}], "messages": "not-a-list"}
        res = self._call(ctx, sid="s8", req=bogus, model="x")
        self.assertIn(res, (None, {"request": bogus}))


class TestModelSwitch(WiringBase):
    ON = {"observe": False, "fold_tool_desc": True, "fold_tool_param_desc": True,
          "fold_skills_index": True, "min_savings_chars": 16}

    def test_model_switch_refreezes_as_new_session(self):
        ctx = self._mk(dict(self.ON))
        first = self._call(ctx, model="qwen3.8-flash")
        self.assertEqual(mod._snapshots["s1"]["model"], "qwen3.8-flash")
        self.assertIn("部分工具描述已折叠", first["request"]["system"])
        # 改配置 + 切模型：缓存本来就没了 → 新会话吃新配置、告知行换语言
        ctx._s["fold_first_sentence_max_chars"] = 40
        second = self._call(ctx, model="gpt-4o", arid="r2")
        self.assertIsNotNone(second)
        snap = mod._snapshots["s1"]
        self.assertEqual(snap["model"], "gpt-4o")
        self.assertEqual(snap["model_switches"], 1)
        self.assertLessEqual(len(second["request"]["tools"][0]["description"]), 40)
        self.assertIn("Some tool descriptions are shortened", second["request"]["system"])
        self.assertNotIn("部分工具描述已折叠", second["request"]["system"])
        # 第三次同模型 → 又冻结住，不再重算
        third = self._call(ctx, model="gpt-4o", arid="r3")
        self.assertEqual(json.dumps(third["request"], ensure_ascii=False, sort_keys=True),
                         json.dumps(second["request"], ensure_ascii=False, sort_keys=True))

    def test_model_switch_persists_new_snapshot(self):
        ctx = self._mk(dict(self.ON))
        self._call(ctx, model="qwen3.8-flash")
        self._call(ctx, model="gpt-4o", arid="r2")
        mod._snapshots = {}
        mod._freezes_loaded = False
        self._call(ctx, model="gpt-4o", arid="r3")   # 重载后不误判成切换
        self.assertEqual(mod._snapshots["s1"]["model"], "gpt-4o")

    def test_legacy_snapshot_backfills_model_without_reshape(self):
        ctx = self._mk(dict(self.ON))
        first = self._call(ctx)
        del mod._snapshots["s1"]["model"]           # 模拟旧版盘上快照
        second = self._call(ctx, arid="r2")
        self.assertEqual(mod._snapshots["s1"]["model"], "qwen3.8-flash")
        self.assertEqual(json.dumps(second["request"], ensure_ascii=False, sort_keys=True),
                         json.dumps(first["request"], ensure_ascii=False, sort_keys=True))

    def test_meltdown_stays_sticky_across_model_switch(self):
        # 急停是用户显式安全态，不因换模型被悄悄撤销
        ctx = self._mk(dict(self.ON))
        self._call(ctx)
        ctx.cmd_handler("meltdown", session_id="s1")
        self.assertIsNone(self._call(ctx, model="gpt-4o", arid="r2"))
        self.assertTrue(mod._snapshots["s1"]["melted"])


class NoLogCtx(FakeCtx):
    """v0.23.0 的真实 PluginContext 没有 .log。"""
    log = None


class TestCtxLogCompat(WiringBase):
    def test_ctx_log_falls_back_to_logger(self):
        mod._ctx_log(NoLogCtx({"observe": False}), "INFO", "hello")   # 不抛即可

    def test_register_survives_ctx_without_log(self):
        ctx = NoLogCtx({"observe": False})
        mod.register(ctx)                     # 不得走到 "registration degraded"
        self.assertIn("post_api_request", ctx.hooks)
        self.assertEqual(ctx.mw[0], "llm_request")


class TestCompactStore(WiringBase):
    COMPACT = ("Search the web for current info. Use beyond knowledge cutoff. "
               "Call read_file on results when you need the full text.")

    def _write_originals(self):
        ctx = self._mk({"observe": False})
        self._call(ctx)                  # 折叠前收割原文
        return ctx

    def test_compact_entry_hashes_and_notice(self):
        entry = engine.make_compact_entry(LONG_DESC, TestParamFolds.SCHEMA,
                                          self.COMPACT, "query(str,必填)")
        self.assertEqual(len(entry["description"]["sha256"]), 64)
        self.assertEqual(entry["description"]["sha256"], engine.field_sha256(LONG_DESC))
        self.assertEqual(entry["schema"]["sha256"],
                         engine.field_sha256(engine.canonical_schema(TestParamFolds.SCHEMA)))
        self.assertIn("original", entry["description"])   # 回读通道
        notice = engine.compact_notice("zh", "D:/ph/tool_compact.json")
        self.assertIn("tool_compact.json", notice)
        self.assertIn("tool_search", notice)

    def test_rule_markers_cannot_be_dropped(self):
        ok, why = engine.validate_compact("简短摘要", LONG_DESC)
        self.assertTrue(ok)
        rule_desc = "Fill saved logins. Never type or repeat a password yourself. " + "更多说明。" * 20
        self.assertFalse(engine.validate_compact("Fill saved logins.", rule_desc)[0])
        self.assertTrue(engine.validate_compact(
            "Fill saved logins. Never type or repeat a password yourself.", rule_desc)[0])
        self.assertFalse(engine.validate_compact(LONG_DESC + "x", LONG_DESC)[0])

    def test_compact_lookup_hash_drift(self):
        entry = engine.make_compact_entry(LONG_DESC, {"type": "object"}, self.COMPACT, "schema")
        store = {"tools": {"web_search": entry}}
        hit = engine.compact_lookup(store, "web_search", LONG_DESC, {"type": "object"})
        self.assertIsNotNone(hit)
        self.assertTrue(hit["schema_ok"])
        # 描述变了 → 整条作废（摘要已不忠实）
        self.assertIsNone(engine.compact_lookup(store, "web_search", LONG_DESC + "x",
                                                {"type": "object"}))
        # schema 变了（换协议/上游改结构）→ 描述仍可用，只把 schema 标成不可信
        drifted = engine.compact_lookup(store, "web_search", LONG_DESC, {"type": "array"})
        self.assertIsNotNone(drifted)
        self.assertFalse(drifted["schema_ok"])
        self.assertEqual(drifted["description"], self.COMPACT)
        self.assertIsNone(engine.compact_lookup(store, "nope", LONG_DESC, {"type": "object"}))

    def test_plan_uses_compact_not_first_sentence(self):
        entry = engine.make_compact_entry(LONG_DESC, TestParamFolds.SCHEMA,
                                          self.COMPACT, "query(str,必填)")
        store = {"tools": {"web_search": entry}}
        st = dict(TestSnapshot.ST)
        st["use_compact_store"] = True
        st["min_savings_chars"] = 16
        plan = engine.plan_session(TestSnapshot()._request(), "anthropic_messages", st,
                                   "qwen3.8-flash", frozenset(), store, "D:/ph/tool_compact.json")
        self.assertEqual(plan["folded_tools"]["web_search"], self.COMPACT)
        self.assertEqual(plan["compact_used"], 1)
        self.assertIn("tool_compact.json", plan["notice_tools"])
        snap = engine.snapshot_from_plan("sc", "anthropic_messages", st, plan)
        out, counts, _ = engine.apply_snapshot(TestSnapshot()._request(), snap,
                                               "anthropic_messages")
        self.assertEqual(out["tools"][0]["description"], self.COMPACT)
        self.assertIn("tool_search", out["system"])

    def test_missing_store_falls_back_to_regex_fold(self):
        ctx = self._write_originals()
        # 精简库缺失 → 不许一刀不砍：退回正则首句折叠，并记 compact_stale
        st = mod._read_settings(ctx)
        st["use_compact_store"] = True
        st["fold_tool_desc"] = True
        st["min_savings_chars"] = 16
        self.assertEqual(mod._compact_for_plan(ctx, st), {})
        plan = engine.plan_session(self._req(), "anthropic_messages", st,
                                   "qwen3.8-flash", frozenset(), {}, "p")
        self.assertIn("web_search", plan["folded_tools"])
        self.assertIn("web_search", plan["compact_stale"])
        self.assertTrue(plan["act"])

    def test_compact_hit_still_folds_params(self):
        entry = engine.make_compact_entry(LONG_DESC, TestParamFolds.SCHEMA,
                                          self.COMPACT, "query(str,必填)")
        st = dict(TestSnapshot.ST)
        st["use_compact_store"] = True
        st["fold_tool_param_desc"] = True
        st["min_savings_chars"] = 16
        plan = engine.plan_session(TestSnapshot()._request(), "anthropic_messages", st,
                                   "qwen3.8-flash", frozenset(),
                                   {"tools": {"web_search": entry}}, "p")
        self.assertEqual(plan["folded_tools"]["web_search"], self.COMPACT)
        self.assertIn("web_search", plan["folded_params"])

    def test_harvest_merge_does_not_nest(self):
        ctx = self._mk({"observe": False})
        self._call(ctx)
        self._call(ctx, arid="r2")
        with open(mod._originals_path(), encoding="utf-8") as f:
            data = json.load(f)
        tools = data["tools"]
        self.assertNotIn("tools", tools)                 # 旧版嵌套不得再出现
        self.assertIn("web_search", tools)
        self.assertIn("description", tools["web_search"])
        # 盘上坏嵌套文件也能被读回成扁平表
        bad = {"version": 1, "tools": {"tools": {"web_search": {"description": "x"}}}}
        self.assertEqual(mod._originals_map(bad), {"web_search": {"description": "x"}})

    def test_cli_prefers_live_tool_list(self):
        ctx = self._write_originals()
        live = {"web_search": {"description": LONG_DESC,
                               "schema": TestParamFolds.SCHEMA}}
        orig_fn = mod._live_tool_originals
        orig_load = mod._load_originals
        mod._live_tool_originals = lambda: dict(live)
        mod._load_originals = lambda: {}
        try:
            ctx.llm = FakeLlm([json.dumps(
                {"web_search": {"description": self.COMPACT, "schema": "query(str)"}})])
            out = mod._run_compact_tools(ctx)
        finally:
            mod._live_tool_originals = orig_fn
            mod._load_originals = orig_load
        self.assertIn("来源=live", out)
        store = mod._load_compact_store(reload=True)
        self.assertEqual(store["source"], "live")
        self.assertIn("web_search", store["tools"])

    def test_cli_compact_tools_generates_json(self):
        ctx = self._write_originals()
        reply = json.dumps({"web_search": {"description": self.COMPACT,
                                           "schema": "query(str,必填)"}}, ensure_ascii=False)
        ctx.llm = FakeLlm([reply])
        orig_fn = mod._live_tool_originals
        mod._live_tool_originals = lambda: {}      # 离线：强制走 harvested 兜底
        try:
            out = mod._run_compact_tools(ctx)
        finally:
            mod._live_tool_originals = orig_fn
        self.assertIn("已写入", out)
        self.assertTrue(os.path.exists(mod._compact_path()))
        store = mod._load_compact_store(reload=True)
        self.assertIn("web_search", store["tools"])
        self.assertEqual(store["tools"]["web_search"]["description"]["compact"], self.COMPACT)
        # 生成走的是登记过的辅助槽
        self.assertIn(mod.AUX_TASK_KEY, ctx.aux_tasks)
        self.assertIn("paperhermes", ctx.cli_commands)
        self.assertEqual(ctx.llm.calls[0][1]["task"], mod.AUX_TASK_KEY)

    def test_cli_dry_run_no_write(self):
        ctx = self._write_originals()
        ctx.llm = FakeLlm([json.dumps(
            {"web_search": {"description": self.COMPACT, "schema": "query(str)"}})])
        orig_fn = mod._live_tool_originals
        mod._live_tool_originals = lambda: {}
        try:
            out = mod._run_compact_tools(ctx, dry_run=True)
        finally:
            mod._live_tool_originals = orig_fn
        self.assertIn("dry-run", out)
        self.assertFalse(os.path.exists(mod._compact_path()))

    def test_rearm_requires_two_consecutive_inputs(self):
        ctx = self._mk({"observe": False, "fold_tool_desc": True, "min_savings_chars": 16})
        self.assertIsNotNone(self._call(ctx))
        self.assertIn("s1", mod._snapshots)
        first = ctx.cmd_handler("rearm", session_id="s1")
        self.assertIn("连续输入两次", first)
        self.assertIn("s1", mod._snapshots)                 # 第一次不动
        ctx.cmd_handler("", session_id="s1")                 # 中间插别的命令 → 作废
        again = ctx.cmd_handler("rearm", session_id="s1")
        self.assertIn("连续输入两次", again)
        second = ctx.cmd_handler("rearm", session_id="s1")
        self.assertIn("已删除", second)
        self.assertNotIn("s1", mod._snapshots)
        with open(mod._freezes_path(), encoding="utf-8") as f:
            self.assertNotIn("s1", json.load(f)["sessions"])
        res = self._call(ctx)                                # 重新定型
        self.assertIsNotNone(res)

    def test_harvest_writes_tool_originals(self):
        self._write_originals()
        with open(mod._originals_path(), encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("web_search", data["tools"])
        self.assertIn("Search the web", data["tools"]["web_search"]["description"])


class TestRulePreservingFold(unittest.TestCase):
    """v0.5.3：首句折叠不得删掉首句之后的强制/禁止约束。"""

    DESC = ("Run shell commands. Output is auto-truncated with the full text saved "
            "to a file; never pipe through head/tail to shorten it. "
            "Foreground only. Prefer read_file over cat for big files. "
            "must not run interactive prompts.")

    def test_rule_sentence_survives(self):
        out = engine.fold_description_keep_rules(self.DESC, 120)
        self.assertTrue(out.startswith("Run shell commands."))
        self.assertIn("never pipe through head/tail", out)
        self.assertNotIn("Prefer read_file over cat", out)   # 无规则标记的句子照旧删

    def test_idempotent(self):
        once = engine.fold_description_keep_rules(self.DESC, 120)
        self.assertEqual(engine.fold_description_keep_rules(once, 120), once)

    def test_budget_caps_growth(self):
        long_desc = "Short lead. " + " ".join(
            "You must keep rule %d in mind when running this tool." % i for i in range(30))
        out = engine.fold_description_keep_rules(long_desc, 120, rule_budget=260)
        self.assertLessEqual(len(out), 120 + 260)

    def test_no_rules_equals_plain_fold(self):
        plain = "First sentence here. Second sentence without markers. Third one too."
        self.assertEqual(engine.fold_description_keep_rules(plain, 120),
                         engine.fold_description(plain, 120))


class TestEnumPreservingParams(unittest.TestCase):
    """v0.5.3：参数枚举字面量被折掉时，该参数不折。"""

    def test_enum_lost_blocks_fold(self):
        schema = {"type": "object", "properties": {"mode": {
            "type": "string", "enum": ["full_text", "semantic", "hybrid"],
            "description": ("Retrieval mode. 'full_text' preserves the historical FTS "
                            "behavior byte-for-byte. 'semantic' searches embedded "
                            "summaries. 'hybrid' fuses both with RRF.")}}}
        folds, saved = engine.collect_param_folds(schema, 120)
        self.assertNotIn("mode", folds)
        self.assertEqual(saved, 0)

    def test_enum_kept_allows_fold(self):
        schema = {"type": "object", "properties": {"mode": {
            "type": "string", "enum": ["fast", "slow"],
            "description": "Pick fast or slow. " + "Extra prose. " * 20}}}
        folds, _ = engine.collect_param_folds(schema, 120)
        self.assertIn("mode", folds)
        self.assertIn("fast", folds["mode"])

    def test_no_enum_unaffected(self):
        schema = {"type": "object", "properties": {"q": {
            "type": "string", "description": "Search query. " + "Be specific. " * 20}}}
        folds, _ = engine.collect_param_folds(schema, 120)
        self.assertIn("q", folds)


class TestSkillNoticeNet(unittest.TestCase):
    """v0.5.3：技能档净亏时不折也不注入告知行。"""

    def _req(self):
        return {"model": "cn:hy4free", "system":
                "intro\n## Skills\n\n- one-liner: short desc\n## Other\n",
                "tools": [{"type": "function", "function": {
                    "name": "web_search", "description": "Search the web. " + "Long. " * 80}}]}

    def test_net_negative_skills_dropped(self):
        st = {"fold_tool_desc": True, "fold_tool_param_desc": False,
              "fold_skills_index": True, "fold_skills_trigger_only": True,
              "min_savings_chars": 100, "keep_eager": []}
        plan = engine.plan_session(self._req(), "chat_completions", st, "cn:hy4free",
                                   frozenset(), None, "")
        self.assertEqual(plan["folded_skills"], {})
        self.assertEqual(plan["est"][2], 0)

    def test_notice_bytes_uses_injected_text(self):
        st = {"fold_tool_desc": True, "fold_tool_param_desc": False,
              "fold_skills_index": False, "use_compact_store": True,
              "min_savings_chars": 100, "keep_eager": []}
        store = {"tools": {}}
        plan = engine.plan_session(self._req(), "chat_completions", st, "cn:hy4free",
                                   frozenset(), store, "")
        # cn:hy4free → 中文告知；净益门必须按实际注入那条（带库路径的 compact 告知）计
        self.assertEqual(plan["lang"], "zh")
        self.assertEqual(plan["est"][3], len(engine.NOTICES["zh"]["tools"]) + 1)


class TestNoticeLang(unittest.TestCase):
    def test_cn_prefix_is_zh(self):
        self.assertEqual(engine.pick_notice_lang("cn:hy4free"), "zh")
        self.assertEqual(engine.pick_notice_lang("zh-glm-4"), "zh")

    def test_vendor_keywords(self):
        for m in ("qwen3.8-flash", "deepseek-v4", "hunyuan-pro", "glm-4.6", "doubao-1.5"):
            self.assertEqual(engine.pick_notice_lang(m), "zh", m)

    def test_english_default(self):
        self.assertEqual(engine.pick_notice_lang("gpt-5"), "en")
        self.assertEqual(engine.pick_notice_lang(""), "en")



    def _req(self):
        return {"model": "cn:hy4free", "system": "base。",
                "messages": [{"role": "system", "content": "base。"},
                             {"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {
                    "name": "web_search", "description": "Search the web. " + "Long. " * 80}}]}

    def _st(self, **kw):
        st = {"fold_tool_desc": True, "fold_tool_param_desc": False,
              "fold_skills_index": False, "fold_skills_trigger_only": False,
              "min_savings_chars": 100, "keep_eager": [], "use_compact_store": False}
        st.update(kw)
        return st

    def test_default_off_has_no_hint(self):
        plan = engine.plan_session(self._req(), "chat_completions", self._st(),
                                   "cn:hy4free", frozenset(), None, "")
        self.assertEqual(plan["notice_hint"], "")

    def test_on_carries_zh_hint_and_gate_counts_it(self):
        st = self._st(inject_skill_search_hint=True)
        plan = engine.plan_session(self._req(), "chat_completions", st,
                                   "cn:hy4free", frozenset(), None, "")
        hint = engine.SKILL_SEARCH_HINTS["zh"].strip()
        self.assertEqual(plan["notice_hint"].strip(), hint)
        self.assertEqual(plan["est"][3], len(engine.NOTICES["zh"]["tools"]) + 1 + len(hint) + 1)

    def test_en_hint_for_english_model(self):
        st = self._st(inject_skill_search_hint=True)
        plan = engine.plan_session(self._req(), "chat_completions", st,
                                   "gpt-5", frozenset(), None, "")
        self.assertEqual(plan["notice_hint"].strip(), engine.SKILL_SEARCH_HINTS["en"].strip())

    def test_apply_injects_hint_once(self):
        st = self._st(inject_skill_search_hint=True)
        plan = engine.plan_session(self._req(), "chat_completions", st,
                                   "cn:hy4free", frozenset(), None, "")
        snap = engine.snapshot_from_plan("s-hint", "chat_completions", st, plan, "cn:hy4free")
        out, counts, _ = engine.apply_snapshot(self._req(), snap, "chat_completions")
        body = out["messages"][0]["content"]
        hint = engine.SKILL_SEARCH_HINTS["zh"].strip()
        self.assertEqual(body.count(hint), 1)
        self.assertGreaterEqual(counts["notice_chars"], len(hint))
        # 哨兵：同句再注入一次不重复
        out2, added2 = engine.system_slot_apply("chat_completions", out, None, None, hint)
        self.assertEqual(added2, 0)

    def test_old_snapshot_missing_key_is_inert(self):
        st = self._st(inject_skill_search_hint=True)
        plan = engine.plan_session(self._req(), "chat_completions", st,
                                   "cn:hy4free", frozenset(), None, "")
        snap = engine.snapshot_from_plan("s-old", "chat_completions", st, plan, "cn:hy4free")
        snap.pop("notice_hint", None)
        clean = engine.sanitize_snapshot(snap)
        self.assertEqual(clean["notice_hint"], "")

    def test_no_fold_no_hint(self):
        st = self._st(fold_tool_desc=False, inject_skill_search_hint=True)
        plan = engine.plan_session(self._req(), "chat_completions", st,
                                   "cn:hy4free", frozenset(), None, "")
        snap = engine.snapshot_from_plan("s-inert", "chat_completions", st, plan, "cn:hy4free")
        out, counts, _ = engine.apply_snapshot(self._req(), snap, "chat_completions")
        self.assertIsNone(out)
        self.assertEqual(counts["notice_chars"], 0)

    def test_hint_lands_on_plain_anthropic_block(self):
        blocks = [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": "tail no marker"}]
        hint = engine.SKILL_SEARCH_HINTS["zh"].strip()
        out, added = engine.system_slot_apply("anthropic_messages", {"system": blocks},
                                              "T-N", None, hint)
        self.assertIn(hint, out["system"][1]["text"])
        self.assertNotIn(hint, out["system"][0]["text"])
        self.assertGreater(added, 0)



class TestV055(unittest.TestCase):
    """v0.5.5：spill TTL、observe 轮转、连续语义、stats CLI。"""

    def test_observer_rotation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            logf = os.path.join(d, "observe.log")
            ob = engine.Observer(logf, max_bytes=64 * 1024)
            req = {"messages": [{"role": "user", "content": "hi"}]}
            for i in range(5000):
                ob.record_request(req, provider="p", model="m", api_mode="anthropic_messages",
                                  session_id="s", action="fold", api_request_id="a%d" % i)
            # 写入过程中已触发轮转：旧档在 .1（>阈值），活跃档 < 阈值
            self.assertTrue(os.path.exists(logf + ".1"))
            self.assertGreater(os.path.getsize(logf + ".1"), 64 * 1024)
            self.assertLess(os.path.getsize(logf), 64 * 1024)
            ob.record_request(req, provider="p", model="m", api_mode="anthropic_messages",
                              session_id="s", action="pass", api_request_id="trigger")
            pass
            with open(logf, encoding="utf-8") as f:
                self.assertIn("trigger", f.read())

    def test_rotation_failure_fail_open(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            logf = os.path.join(d, "observe.log")
            ob = engine.Observer(logf, max_bytes=64 * 1024)
            req = {"messages": [{"role": "user", "content": "hi"}]}
            for i in range(5000):
                ob.record_request(req, provider="p", model="m", api_mode="anthropic_messages",
                                  session_id="s", action="fold", api_request_id="a%d" % i)
            # 轮转失败路径：.1 已存在（真实文件），换成目录让 remove 失败 → 追加必须照常
            os.remove(logf + ".1")
            os.mkdir(logf + ".1")
            ob.record_request(req, provider="p", model="m", api_mode="anthropic_messages",
                              session_id="s", action="pass", api_request_id="after")
            with open(logf, encoding="utf-8") as f:
                self.assertIn("after", f.read())

    def test_spill_ttl_cleanup_removes_old_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "spill", "sess")
            os.makedirs(root)
            old = os.path.join(root, "old.txt")
            new = os.path.join(root, "new.txt")
            for fp in (old, new):
                with open(fp, "w") as fh:
                    fh.write("x")
            cutoff = __import__("time").time() - 25 * 3600
            os.utime(old, (cutoff, cutoff))
            # 直接调用引擎侧纯函数（v0.5.5 通过 _lazy_cleanup 走，这里测其核心）
            removed = engine.cleanup_spill_dir(root, ttl_hours=24)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
            self.assertEqual(removed, 1)

    def test_error_streak_reset_semantics(self):
        # 连续异常语义：snap["error_streak"] 由成功路径清零（_llm_middleware 分支）
        # 这里只钉常量与阈值一致性
        self.assertEqual(mod.ERROR_BREAKER_THRESHOLD, 5)
        self.assertEqual(mod.SPILL_TTL_HOURS, 24)
        self.assertEqual(mod.SIGNAL_TTL_DAYS, 30)



class TestV055Cli(WiringBase):
    """v0.5.5 事故修复：CLI 无子命令不得落进 compact-tools 批处理；stats 必须自己 print。"""

    def _ns(self, argv):
        import argparse
        ctx = self._mk({"observe": True})
        setup_fn, handler_fn = ctx.cli_commands["paperhermes"]
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        pp = sub.add_parser("paperhermes")
        setup_fn(pp)
        pp.set_defaults(func=handler_fn)   # 宿主 main.py:3142 的接线顺序
        return ap.parse_args(argv)

    def test_noarg_does_not_run_compact(self):
        import contextlib
        import io
        ns = self._ns(["paperhermes"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns.func(ns)
        self.assertIn("需要子命令", buf.getvalue())
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "paperhermes", "tool_compact.json")))

    def test_stats_subcommand_prints_ledger(self):
        import contextlib
        import io
        ns = self._ns(["paperhermes", "stats"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns.func(ns)
        self.assertTrue(buf.getvalue().strip())
        self.assertIn("paperhermes", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
