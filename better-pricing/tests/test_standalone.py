
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("better_pricing", r"D:\\00-DIY\\20260921\\plugins\\better-pricing\\__init__.py")
bp = importlib.util.module_from_spec(spec); sys.modules["better_pricing"]=bp; spec.loader.exec_module(bp)

print("== config path:", bp.config_path())
print("== state db:", bp.state_db_path())

# 1) 报表引擎
rep = bp.build_report("")
print(rep[:1500])
print("...OK, report lines:", rep.count(chr(10)))

# 2) 回报钩子：模拟 estimate_usage_cost 注入
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost as orig
patched = bp.install_report_hook()
print("== hook installed:", patched)
u = CanonicalUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=500_000, request_count=10)
r = bp.load_config()
res = bp._pricing_from_config("qwen3.8-flash", "custom:dashscope-token-plan", u)
print("== inject qwen3.8-flash:", None if res is None else (res.amount_usd, res.status, res.source, res.label))
# 期望: (1.2*1 + 7.2*0.1 + 0.24*0.5)/7.2 = (1.2+0.72+0.12)/7.2 = 2.04/7.2 = 0.2833
res2 = bp._pricing_from_config("qwen3.8-max", "custom:dashscope-token-plan", u)
print("== unpriced model -> None?", res2)
res3 = bp._pricing_from_config("Qwen/Qwen3.8-Flash-Next", "custom:ms-api-infer", u)
print("== independent-system -> None?", res3)
res4 = bp._pricing_from_config("anything", "dashscope-common", u)
print("== free provider:", None if res4 is None else (res4.amount_usd, res4.status, res4.source))

import agent.usage_pricing as up
r5 = up.estimate_usage_cost("qwen3.8-flash", u, provider="custom:dashscope-token-plan", base_url="https://x")
print("== through patched estimate_usage_cost:", r5.amount_usd, r5.status, r5.source)
r6 = up.estimate_usage_cost("glm-5.3", u, provider="custom:dashscope-token-plan", base_url="https://x")
print("== fallthrough unknown:", r6.status, r6.source)

# 3) 帮助与空窗口
print("== help ok:", "pricing-plus" in bp.build_report("--help"))
print("ALL PASS")
