# better-pricing — 自定义模型报价表 / 独立计费台

Hermes 内置计费只认 OpenRouter、内置快照表和端点自报价格三套来源——如果你用的是**阿里云百炼、Token Plan、或任何自定义 OpenAI 兼容端点**，`hermes insights` 和桌面端的费用列会全部显示 `$0.00 / unknown`。本插件让你**手工誊一份价目表**，即按此价格实时算账。

> 设计契约：独立旁路工具，**不改写** Hermes 的 `usage_pricing.py` 源码——只在内存里包装 `estimate_usage_cost`（可通过插件卸载恢复，见「已知边界」）；对 `state.db` 只读，不落任何衍生状态；改配置文件即时生效（mtime 热载），无需重启。

## 快速开始

1. 把本目录放进 `$HERMES_HOME/plugins/`（与其它插件平级，或套一层分类目录如 `plugins/custom-plugins/better-pricing/`，Hermes 支持一级递归）。
2. `config.yaml` → `plugins.enabled` 加一行 `better-pricing`（嵌套放置时写 `custom-plugins/better-pricing`）。
3. 写价目表 `$HERMES_HOME/better-pricing.yaml`（见下节）。
4. 重启 Hermes（钩子随插件在进程启动时装载）。

## 价目表格式（`better-pricing.yaml`）

```yaml
native_report: USD        # 向 Hermes 报账的方式：USD | native | disable
price_list:
  custom:dashscope-token-plan:     # key = state.db 里的 billing_provider 原文
    calculates: token              # token | calls | free
    currency: CNY
    multiply: 7.2                  # 1 USD = 7.2 CNY；0 = 独立体系（永不折成美元）
    models:
      # 单位：元 / 百万 tokens，直接照抄百炼价格表即可
      qwen3.8-flash: { input: 1.2, output: 7.2, cache: 0.24 }
      qwen3.8-max:   { input: 12, output: 36, cache: 1.5 }
  custom:credits-provider:         # 例：按次计费的"XX粒"体系
    calculates: calls
    currency: 魔粒
    multiply: 0
    models:
      model-1: 1                   # 每次 API 调用 1 粒
  custom:free-tier:
    calculates: free               # 免费额度通道，记账为 0
```

字段语义：

| 字段 | 说明 |
|---|---|
| `native_report` | `USD`：按 `multiply` 折算成美元写进 Hermes 费用列（桌面端 `$` 符号正确）；`native`：原始币种数值直接写入（桌面端仍显示 `$`，已知取舍）；`disable`：完全不向 Hermes 报账，只用本插件自己的报表 |
| `calculates` | `token` 按 token 计费 / `calls` 按 API 调用次数 / `free` 恒为 0 |
| `multiply` | 该币种对美元汇率。`0` 表示独立体系（积分/魔粒类），**从美元合计中排除**，避免汇率瞎猜 |
| provider key | 与 `state.db` 的 `billing_provider` 完全一致（`hermes pricing-plus` 报表第一列就是它，照抄即可）。匹配不上时会按模型名全局唯一兜底 |
| 缺模型的语义 | **提供商条目存在、但 `models:` 里没有这个模型 → 计为「无价目」，不按 0 元记账，也不借别家同名模型的价**。全局模型名兜底只服务于「提供商 key 完全对不上」的场景（如换过网关名） |

token 计费公式：`(input×输入 + output×输出 + cache×缓存读 + cache_write×缓存写) ÷ 100万`，缓存写单价缺省沿用 `cache`。

## 使用

```
hermes pricing-plus [--days 7] [--provider dashscope] [--aux] [--config-check]
```

- 报表**永远按你配置的币种显示**（CNY 显示 CNY、魔粒显示魔粒，分币种小计），折美元仅用于报账口径。
- `--days` 窗口天数（0=全部历史），`--provider` 按 billing_provider 子串过滤，`--aux` 包含辅助调用（压缩/摘要等），`--config-check` 校验价目表可读。
- 会话内也可以用斜杠命令 `/pricing-plus`，参数同上。

## 与 `hermes insights` 的关系

插件的回报钩子会在**每个回合结束时**把 `estimated_cost_usd / cost_status / cost_source` 写进 `state.db`，此后 `hermes insights`、桌面端会话列表的费用列都能正常显示（它们读库，不需要插件在场）。注意两点：

- 装完插件必须**重启 Hermes 后端进程**，且只对**新会话**生效；
- 历史会话不会自动补账（行已定型为 unknown），需要的话请自行按价目表回填。

## 已知边界

- 桌面端费用列硬编码 `$` 符号（前端 TSX，Python 插件改不动），`native_report: native` 时显示会是 `¥数值+$符号` 的怪相，推荐用 `USD` 模式。
- 汇率 `multiply` 是手工静态值，请定期自己更新。
- 按次计费（`calls`）以 `session_model_usage.api_call_count` 为准。
- **「无价目」不是 0 元**：价目表缺条目时报表给 `无价目` 并按缺口计数，不计入「有价」——这是刻意的，静默按 0 计会把真实开销藏起来。补齐 `better-pricing.yaml` 对应模型条目即可恢复有价（本机首轮实测：66 行里 7 行原本被按 0 元记账，修后显式列为缺价）。
- 配置查找顺序：`$BETTER_PRICING_CONFIG` → `$HERMES_HOME/better-pricing.yaml` → 插件目录 → 插件父目录（开发态）。**刻意不读当前工作目录**——`Path.cwd()` 兜底等于谁在哪个目录起进程谁就能塞一份价目表。
- 报账钩子是全局 monkey-patch，但可卸载：注册时挂了 `ctx.on_unload(uninstall_report_hook)`，进程退出另有 `atexit` 兜底，禁用/重载插件会把 `estimate_usage_cost` 换回原函数（幂等，热重载不会套娃）。
