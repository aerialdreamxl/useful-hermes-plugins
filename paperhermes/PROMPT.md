# paperhermes 需求书

版本：v0.4（v0.3→v0.4：用户 2026-11-09 二轮裁定，9.1-9.7 全部冻结为第 9 节裁定记录；D11 observe 默认 true；9.7 推翻"单语言常量"建议改按模型关键词选语；9.2 推翻"首会话全量"建议，M2 首会话即折叠）
日期：2026-11-09
状态：**v0.4 已全部实现并实机验收（M0 观测与 M1/M2 折叠均有实测数据，见文末坑记录）**
上游审查件：《Hermes 省 Token 介入点审查》（v0.23.0 源码取证 + 固定面实测，本地调查档案，未随仓库发布）

## 1. 一句话定位

一个 Hermes Agent 侧插件：在 `llm_request` 中间件这一个官方接缝上，对**外发载荷**做会话级字节稳定的瘦身——eager 工具 schema 的 description 折叠为一行摘要（全量可经 bridge 按需回读）、skills 索引低频类目降为名字行（描述可经 skills_list 按需回读）。它是官方 `tools.tool_search` defer（管"哪些工具整体延迟"）之下的**第二层**：管"留下的工具描述有多重"。不改安装树、不改核心配置语义、不删任何工具/技能实体。

**它不是什么**：不是压缩器（不动历史消息体，那是 proactive_prune/ContextEngine 的地盘）；不是新的 defer 列表（与 `tools.tool_search.defer` 正交共存）；不是系统提示词重写器（guidance 散文一字不动，只动 `## Skills` 块内行）。

## 2. 已冻结的决策

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 形态 | agent-side 插件（`register(ctx)`），主体 = `llm_request` 中间件 + `pre_api_request` 观测钩；无 dashboard/desktop 半体 |
| D2 | 能力承诺分档 | tool schema 描述折叠 = M1（高置信路径，验收门可写死）；skills 索引降档 = M2（默认关、opt-in、A/B 门裁定）——**需求书上不允许把两者打包成"无损"承诺** |
| D3 | 只削 description，params JSON Schema 永不改动 | 实测依据见 3.1：description 占 schema 字节 55%；回程三闸门（intake 校验/registry dispatch/bridge 全量）全都不读 payload 里的 description |
| D4 | 会话级字节稳定优先于任何单请求最优 | 折叠结果按 (工具名+描述原文 hash) 记忆化，会话内一次定型；绝不逐请求漂移（Anthropic cache_control 断点骑在 system 块与 tools 上，漂移=缓存全毁，倒亏） |
| D5 | 白名单永不折叠 | `clarify`（上游 A/B 判例：defer 后结构化澄清 18/18→7/18）、用户在 `keep_eager` 配置里点名的工具、以及全部 INLINE_TOOL_EXECUTORS 面的工具 |
| D6 | 与官方 defer 的关系 | 不读取不覆写 `tools.tool_search.defer`；官方已 defer 的工具本就不在 payload 里，paperhermes 只处理幸存者 |
| D7 | 观测先行 | M0 = 纯观测（pre/post 字节计数进插件日志与 `/paperhermes` 命令面板），采 ≥20 个真实请求的直方图后才开 M1 折叠 |
| D8 | **开关面（用户 2026-11-09 裁定，v0.3 修订版）**：每个功能一个独立 bool 开关，全部在 `plugins.entries.paperhermes.settings`，**修改类行为默认 false，用户置 true 才介入**；**首启播种**：插件首会话若未读到 settings 子树，`ctx.set_config` 一次性写入全套默认值（seed-once），让面板有真实值可显示 | 机制已核验：`ctx.get_config(key, default)`（`hermes_cli/plugins.py:266`）只读 settings、缺键回退代码默认；写入走 `plugins_state.save_plugin_setting`（原子，与 Settings→Plugins 面板同一 writer，`plugins_settings.py` 模块头实证）。硬规约：①代码内每个开关缺省按 False 处理（读不到=不介入，播种失败同样按 false 运行）；②**seed-once 语义**：播种条件=settings 子树缺失或为空，且一个进程至多一次；播种之后插件永不写 settings——用户**删单键=永久缺失，插件不补种**（防重播种循环与用户删除打架）；用户整段删空 settings 后重启会再播种一次（等价重置，README 写明）；③运行态/统计一律走进程内 dict 与 6 节 hint 文件，绝不进 settings；④plugin.yaml 的 `config_schema` 的 `default` 键只供面板显示，不作为行为默认；⑤`plugins.enabled` 与功能开关两级——enable 后所有功能缺省关，插件等效不存在（除播种外零行为） |
| D9 | **README 说明义务（用户 2026-11-09）**：每个选项必须写清"改什么、实测省多少 token、可能引发什么问题、怎么回滚"，见 7.1 | 省量数字必须由 M0/M1 实测填充，不许预填估计值 |
| D10 | **模型侧告知行（用户 2026-11-09）**：折叠介入时向 system 槽注入固定告知句（如"技能描述已折叠以节省上下文，名字全在场，按需 skills_list 搜索、skill_view 加载"） | 文案规格见 4.2；字节稳定 + 幂等哨兵检查；告知与折叠同生命周期（开关全关=零注入） |
| D11 | 观测开关 `observe` 的默认值 | **已裁定（11-09 #6）**：只读项播种默认 true——它只记日志不改载荷，"最大的问题只可能是 .log 大一点"；三个修改类开关（fold_tool_desc / fold_skills_index / fold_skills_dynamic）仍全 false |

## 3. 现实约束（实测，不是推测）

### 3.1 固定面构成（2026-11-09，本机会话，v0.23.0）

进程内 `model_tools.get_tool_definitions()` 实取 22 eager 工具：

| 指标 | 值 |
|---|---|
| schema 总字节 | 45,417 chars（≈11.3K tok） |
| 其中 description | **25,310 chars（55.7%）** |
| 其中 params+结构 | 20,107 chars |
| description 占比最高 | tool_search 5487/6059（90%）、execute_code 82%、browser_exec 79%、manage_connections 66% |
| description 占比低（不值得动） | skill_manage 24%、search_files 24%、set_theme 28%、terminal 31% |
| skills 索引 | 15,236 chars：178 条目行 13,685 + 头尾 guidance 1,551（≈5.1K tok，CJK 混排按 3.5-4 折算） |
| 官方 tier-1 桥已延迟 | 62 工具 ≈11,978 tok（与本插件正交） |

**推论**：M1 若把 description 折叠为"首句 ≤120 chars"（对齐官方 catalog clip 规则），可回收约 15-18K chars ≈ **3.8-4.5K tok/请求**；M2 若把低频技能类目降名字行，约再回收 10-12K chars。两笔都是**每请求**成本（未计缓存折扣；线性注意力模型则是每遍全扫的注意力成本，两边都省）。

### 3.2 接缝硬事实（源码锚点，v0.23.0 逐处实读）

- `llm_request` 中间件 fire 点：`agent/turn_api_request.py:156-158`，位于 `_build_api_kwargs` 与 transport `preflight_kwargs` **之后**——拿到的是**当前协议最终 wire 形态**的 request dict；返回 `{"request": {...}}` 即整体替换。deepcopy 遇 clients/callbacks 降级浅拷（`hermes_cli/middleware.py:_safe_copy`），**改写前先判形状再动**。
- 各 api_mode 的 system/tools 槽位（插件作者规范笔记 2026-09-29 实测表）：chat_completions=`messages[0]`+`tools[{type:function,…}]`；anthropic_messages=`system`（str 或 cache_control 块列表）+`tools[{name,description,input_schema}]`；codex_responses=`instructions`(str)+扁平 `tools[]`，且 `prompt_cache_key` 由**原始** instructions+tools 内容寻址（改写字节不改 key 计算路径——服务端前缀匹配语义须 M0 实测，不许拍脑袋）；bedrock_converse=cachePoint 标记**骑在** system/toolConfig.tools 内部——**首版对 bedrock 直接旁路**。
- 回程三闸门全部不读外发 payload（折叠安全性来源）：①intake 校验对 `agent.valid_tool_names`（agent 快照）；②dispatch 走 registry（原始 schema 永久在场）；③`tool_describe`/bridge 从 registry 取全量（`tools/tool_search.py:dispatch_tool_describe`）。**推论：payload 里删 description 不伤可调用性**——但注意 `tool_search` 三桥工具自身 description 90% 占比最高，折叠它=模型不知道桥的存在意义，D5 白名单覆盖。
- skills 索引注入路径：`agent/system_prompt.py::_skills_prompt` → `prompt_builder.build_skills_system_prompt`，落在系统提示尾部（byte-stable per session，快照 `.skills_prompt_snapshot.json`）。索引**不在**独立 tools 槽位——M2 的改写目标是 system 文本里的 `## Skills` 区块，必须用**块边界正则**定位、绝不误伤相邻 guidance。
- 官方"降档不隐身"判例（风险来源，写死在验收里）：`prompt_builder.py:1303` "NEVER drop entries"、`coding_context.py:368` "Demoted, never hidden: pruning caused silent capability loss"。M2 保留**全部技能名**（与 focus 同级），只折叠描述——这是唯一官方立场内的懒加载形态。
- `pre_api_request` 是**观测契约**（无替换权），kwargs 自带 `approx_input_tokens`/`request_char_count`/`tool_count`——M0 直方图免费拿。
- 中间件回调异常 = fail-open 原样发送（`apply_llm_request_middleware` 外层 try/except + WARNING），插件死不了会话，但**静默失效**要靠自检日志治理（见 5.4）。

### 3.3 环境事实

- 解释器：桌面 gateway 进程 = `$HERMES_HOME/tools/python-3.14.x/python.exe -m hermes_cli.main`（无 `-I`，PM venv 包在 site-packages）；测试解释器 = `$HERMES_HOME/installs/*/environments/*/venv/Scripts/python.exe`（判定法：`import httpx` 冒烟定位）。
- 插件加载时机：常驻桌面进程**进程启动时**加载，函数级改动需全量重启；`hermes chat --oneshot` 新进程可绕开常驻干扰做验收。
- 配置面：`plugins.entries.paperhermes.settings`（plugin.yaml `config_schema` 驱动 Settings→Plugins 面板），密钥不涉及。
- 本机 api_mode 实况：主模型槽 custom:dashscope-token-plan `base_url .../apps/anthropic`——**anthropic_messages 路径是首战场**，chat_completions 次之，其余 mode 一律旁路。

## 4. 数据模型

### 4.0 开关面（D8 具体化：settings 字段全表）

全部位于 `plugins.entries.paperhermes.settings`。首会话播种（D8 seed-once）后这些键**真实存在于 config**；行为读不到键时按各自的代码缺省处理（`observe`→true、三个修改类开关→false；播种失败=同样缺省运行，零载荷介入）。数值/列表参数只在对应开关为 true 后被读：

| 键 | 类型 | 缺省 | 为 true 时介入的行为 |
|---|---|---|---|
| `observe` | bool | **true**（只读项默认开，D11） | 挂 `pre_api_request`/`post_api_request` 观测钩，记账 pre/post 字节进日志与面板（只读，不改载荷；false 时连钩都不挂） |
| `fold_tool_desc` | bool | false | M1：tools 槽 description 折叠（白名单 D5 与旁路 5.1 仍生效） |
| `fold_skills_index` | bool | false | M2：system 槽 `## Skills` 区块低频类目降名字行 |
| `fold_skills_dynamic` | bool | false | M2 增强：技能使用后回补全量（依赖 `fold_skills_index=true`，否则本键无效并 WARN 一次——**WARN 不自动改配置**） |
| `keep_eager` | list[str] | `[]`（仅 fold_tool_desc=true 时被读） | 用户点名永不折叠的工具 |
| `fold_first_sentence_max_chars` | int | `120`（仅 fold_tool_desc=true 时被读） | 折叠长度参数（9.1 裁定：可调，低风险默认值见第 9 节尾注） |
| `min_savings_chars` | int | `2048` | 净益门（裁定 #1）：本轮预估回收低于此值则完全不介入——"量少开了也没用" |

验收钉死（并入 11 节）：**插件 enable 且全部开关为 false（或键缺失）时，外发 payload 与未装插件逐字节一致**；config.yaml 的写入只允许发生**首启播种一次**（11 节用"第二次会话起 mtime/sha 恒定"断言），此外任何运行路径零 config 写入。

### 4.1 运行时状态

无新存储（hint 文件除外，见 6 节）。运行时状态两份，进程内、会话粒度：

```
fold_cache:  { (tool_name, sha1(desc原文)[:12]) -> folded_desc }     # D4 字节稳定锚
session_state: { session_key -> {mode, requests_seen, bytes_saved_cumulative, enabled_tiers} }
```

折叠函数 `fold_description(text) -> str`：取首句（到第一个"。/! ?/换行"），≤`fold_first_sentence_max_chars` 原样保留，超长截断 + `…`；**首句里保留触发词**（"Use when…/触发："类前缀不裁——它们是使用判据）。函数纯 stdlib、幂等、对 CJK 与 EN 混合文案实测过 20 个真实工具描述（取自 registry，不编样本）。

### 4.2 告知行规格（D10，v0.4 按裁定 #7 双语化）

折叠介入时向 **system 槽**追加固定告知句（tools 数组没有自由文本槽，两侧告知统一走 system 尾行），未介入的块不加。**每侧两套常量（EN/中文各一），会话首次折叠时按模型名选定一套并冻结到会话尾**——模型切换本就重建请求，不新增缓存变量：

- **语言选择规则（用户 11-09 裁定）**：模型名小写化后从左到右扫关键词表，**先匹配者生效**：`qwen/deepseek/glm/ernie/step/minimax/kimi/spark` → 中文；`gpt/claude/gemini/grok/mistral` 或全不匹配 → 英文。实现=有序列表（非 dict，保"先匹配"语义），匹配用 `in` 子串。
- 工具面（`fold_tool_desc=true` 且本轮实际折叠 ≥1 个工具）：EN `Some tool descriptions are shortened to save context; search tool names with tool_search and read the full schema with tool_describe.` / 中「部分工具描述已折叠以节省上下文；需要完整说明时用 tool_search 搜索、tool_describe 读取。」
- skills 面（`fold_skills_index=true` 且确有类目降档）：EN `(Skill descriptions are folded here because the list is long — every name is listed; search with skills_list and load with skill_view(name) as usual.)` / 中「技能列表较长，此处仅列名称、描述已折叠——按需自行搜索：用 skills_list 查找，用 skill_view(名字) 加载，功能不受影响。」
- 四条硬规则：①**两套皆为常量字符串、零运行时插值**，选定后逐字节复用（D4）；②注入前**哨兵检查**——system 文本已含本句（或另一语言同位句）即跳过，防重复/双份；③文案点名找回路径且直给"自己去搜"的行动指令（裁定 #2）；④anthropic mode 下 system 若为 cache_control 块列表，告知句追加进**最后一个无断点块**，绝不新增/移动断点。
- 开关回 false 后告知行与折叠同时消失（无残留），payload 回归原始字节。

## 5. 接口 / 折叠 / 调度

### 5.1 主路径（每请求）

0. 开关判定（D8/4.0）：`fold_tool_desc` 与 `fold_skills_index` 均非 true → 原样返回（观测开关独立，见 4.0）。**开关值会话首请求读取一次、冻结到会话结束（5.2 缓存铁律同源）**——会话中途在面板改开关不影响本会话，下一会话生效，变更提示写进 `/paperhermes` 输出；读失败=按缺省处理。
1. 旁路判定：api_mode ∈ {codex_responses, bedrock_converse} → 原样返回（9.3/9.4 裁定：不实测、永久旁路）；会话折叠形态未冻结 → 先走 5.2 冻结流程。
2. 定位 tools 槽（按 mode 表 3.2），逐工具：`name ∈ keep_set(D5 白名单∪用户配置)` → 跳过；否则 `folded = fold_cache 命中 or fold_description(desc)`；**折叠后字节数 ≥ 原文的 80% → 判定不值得，回退原文并钉进缓存**（防负优化抖动）。M2 开启时：在 system 槽定位 `## Skills` 区块，按会话冻结的降档集（5.2）把条目行降为纯名字行——区块注记**不重复写**，统一由步骤 4 告知行承担。
3. **净益门（裁定 #1"量少的话开了也没用"）**：本轮预估总回收（tools+skills 折叠字节）**减去**告知行注入字节 < `min_savings_chars`（缺省 2048）→ 本轮完全不介入、原样返回（连告知也不注入；门在会话首请求测一次即冻结，与 5.2 同律）。
4. **告知行注入（D10/4.2）**：本轮确有折叠/降档 → 按 mode 定位 system 载体（chat_completions=`messages[0].content`；anthropic=system 字段，块列表形态按 4.2 规则④落最后一个无断点块），哨兵检查后追加常量句；本轮无任何介入 → 不注入也不清理（payload 本就无句）。
5. 返回 `{"request": {...}}`；同轮把 pre/post 字节计数记账（M0 直方图数据源）。

### 5.2 低频集与会话冻结（M2 的核心机制；v0.4 按裁定 #2 重写：一切折叠会话首请求定型）

**缓存铁律（用户 11-09 原话："会话起来了就不好改了，毕竟有缓存"）**：折叠形态（语言、降档类目集、告知行）在**每会话第一个请求**定型，冻结到会话结束——中途变化=重写已缓存前缀，倒亏比省的多。因此：

- **首会话即折叠**：不设"首会话全量观察期"（推翻 v0.2 的 9.2 建议）。降档集初值=hint 文件（`$HERMES_HOME/paperhermes/session_hints.json`，唯一落盘件，损坏即删重建）；无 hint 时按静态规则：非编码类目全降（对齐官方 focus 集），编码相关全量。
- **使用信号只记账、不当场生效**：`post_tool_call` 钩观测 `skill_view(name)` 成功加载 → 写入本会话 signal 集；**会话结束（on_session_end）时**把 signal 合并进 hint——下会话该技能升全量。键 `fold_skills_dynamic` 语义=是否启用这套跨会话升降（依赖 `fold_skills_index=true`，否则无效 WARN 一次，**WARN 不自动改配置**）。
- 名字永远在场，降档只折描述——与官方 "Demoted, never hidden" 同形。

### 5.3 时序与关键路径

全部折叠是纯字符串操作（regex + 截断 + dict 查找），单请求预算 **<5ms**（M0 实测门槛：22 工具 + 178 行索引全量折叠的 CPU 时间，用 PM venv 解释器计时 100 次取中位数）。零网络、零 LLM 调用、零磁盘 IO（hint 文件只在会话首尾读写）。

### 5.4 降级链

L1 折叠异常（任何形状意外）→ 该请求原样发送 + 计数 WARN（同因一次）；L2 连续 5 次 L1 → 本会话自动关闭 M1（**运行态熔断，D8 规约：不回写 settings，用户开关值原样尊重；熔断同时撤掉告知行注入**；日志说明原因）；L3 registry import 失败 → 插件不注册，doctor 可见；**永不因折叠失败让请求失败**。自检指纹：热路径每会话首请求打一行 `[PAPERHERMES] folded N tools, saved ~K chars (mode=…, tier=M1/M2)`——agent.log 里缺这行 = 插件没吃到新代码或已静默降级。

## 6. 写入与并发

- **config.yaml 写入仅一处**：D8 seed-once 播种（首会话发现 settings 子树缺失/为空时，经 `ctx.set_config` 逐键写 false/默认参数——`save_plugin_setting` 原子写、与面板同一 writer，天然不顶掉别的键）。播种守卫=进程内 flag + 写前复读（子树仍空才写），两个进程并发首启时后至者复读发现非空即弃播，不重试。此后整份插件生命周期零 config 写。
- `fold_cache`/`session_state` 进程内 dict，读写点在中间件调用栈（每会话单线程到 provider），无跨线程写；hint 文件原子写（tmp + `os.replace`），只写者=会话结束/里程碑（每 20 请求）。
- 多会话并发共写 hint：文件级 `O_EXCL` 锁 + 超时 2s 放弃本轮写入（丢 hint 不丢正确性）。
- **最坏丢失边界**：播种被用户整段删空 → 下次重启重播一次（=重置，可预期）；hint 文件全丢 → 下会话低频集退回初始全量降档（M2 关时零影响）；fold_cache 丢 → 重折叠（同输入同输出，D4 稳定）。

## 7. 对外工具面

| 工具/命令 | 用途 |
|---|---|
| `/paperhermes`（register_command） | 状态面板：当前开关读取值、本会话 pre/post 字节、累计回收、M2 动态集。**只读**：开关变更不代写 settings（D8 规约②），而是原样打印应写入 `plugins.entries.paperhermes.settings` 的 YAML 行，由用户本人提交 |
| `hermes paperhermes stats`（register_cli_command） | 跨会话累计账（读日志聚合，不另建 DB） |

无写入型工具，无注入面（折叠内容全部来自 registry/config 既有文本，不吃外部输入进 prompt）。

### 7.1 README 说明义务（D9，交付门槛）

README 必含一张**逐选项四列表**，每个 settings 键一行：**改什么**（一句话机制）/ **实测省多少**（M0/M1 数据回填：`observe=true` 采到的本会话 pre/post 直方图折算 tok/请求，标注测量条件与样本数，禁止预填估计值）/ **可能引发什么问题**（对应已知判例：M1=模型偶尔因薄描述误选工具→用 keep_eager 点名回补；M2=冷门技能想不起来→上游 18/18→7/18 判例风险、fold_skills_dynamic 缓解；告知行自身占用的字节同样实测入账）/ **怎么关**（键置 false 或删键 + 重启，及熔断自动关的识别日志行）。表外另附：seed-once 播种行为说明（何时写 config、删整段会重播、删单键不补种）与 `## Skills`/tools 两处告知行原文样例，让人一眼知道模型会被额外告知什么。

## 8. 迁移与切换门禁

| 阶段 | 内容 | 过门指标 |
|---|---|---|
| M0 | 只观测：pre_api_request 直方图 + 折叠 dry-run 计数（不替换载荷） | ≥20 真实请求；折叠 dry-run 估算回收 ≥8K tok/请求；bedrock/codex 旁路判定 0 误入 |
| M1 | anthropic_messages + chat_completions 的 tools description 折叠，白名单在位 | **字节稳定断言**：连续 3 请求同一工具折叠输出 sha1 相同；Anthropic 缓存读率不降（用 post_api_request 的 cache stats 对照 M0 基线 ±3% 内）；工具调用错误率（"does not exist"/参数校验失败）与基线持平；A/B：同一 10 任务集（含必须用 defer 桥/execute_code 的任务）折叠 on/off 完成率不劣化 |
| M2 | skills 索引动态降档（默认关） | A/B：5 个"必须靠索引描述才能想起某技能"的任务（技能名冷门、无口头提示），折叠组 skill_view 触发率 ≥非折叠组 -1 次；用户口头裁决体感 |
| 回滚 | settings 三个功能开关改 false 或直接删键 + 重启即全灭（缺省=false=不介入）；插件目录移出扫描根 | 5 分钟内回到零干预态，且删开关不需要动 `plugins.enabled` 之外的任何配置 |

改 `plugins.enabled` / config 由用户本人执行（运维偏好），插件交付时给准确 YAML 块与命令。

## 9. 开放问题裁定记录（用户 2026-11-09 二轮，全部冻结）

| 编号 | 问题 | 裁定 |
|---|---|---|
| 9.1 | 折叠阈值/是否可调 | **可以设计成"量少的话开了也没用"**→ 净益门 `min_savings_chars`（5.1 步骤 3），`fold_first_sentence_max_chars` 同步开放为 settings 参数 |
| 9.2 | M2 初始低频集要不要首会话降 | **首会话就折叠**——"会话起来了就不好改了，毕竟有缓存"：一切折叠形态在会话首请求定型冻结（5.2 缓存铁律），不做中途升档；跨会话调整走 hint 文件 |
| 9.3 | codex_responses 的 prompt_cache_key 与改写关系 | **无 Codex 付费账号，没法测**→ 未实测的协议一律旁路：codex mode 永久直通，不冒险（3.2 该行降级为历史记录） |
| 9.4 | bedrock 要不要做 | 作者无 AWS/Bedrock 使用场景 → **永久旁路**（与 9.3 同一原则：本机不可测=不实现） |
| 9.5 | 观测数据进桌面 UI？ | **纯 CLI**——UI 设计太麻烦；面板只 `/paperhermes` 命令 + `hermes paperhermes stats` 两个面 |
| 9.6 | `observe` 默认值 | **只读项可以默认 true**——"最大的问题只可能是 .log 大一点"（D11） |
| 9.7 | 告知行语言 | **按模型决定**：模型名从左到右匹配关键词先匹配生效、大小写不敏感；`Qwen/DeepSeek/GLM/ERNIE/Step/Minimax/Kimi/Spark`→中文，`GPT/Claude/Gemini/Grok/Mistral` 或未匹配→英文（4.2 语言选择规则） |

遗留的低风险默认值（不再询问，M0 数据回来后如明显不合理再向用户提请改判）：净益门 2048 chars、首句长度 120 chars、无 hint 时的静态降档集=官方 focus 类目表。

## 10. 非目标（明确不做）

- 不动历史消息体 / 不做摘要压缩（ContextEngine、proactive_prune 地盘，且每轮破坏缓存）。
- 不删工具、不隐藏技能名（"隐身"类改动一律拒绝，官方 discovery-regression 立场）。
- 不覆写 `tools.tool_search.defer`、不 patch 核心函数、不注册 transport。
- 不做跨模型商用的通用产品化（这是本机个人插件；泛化前置条件是 M1 验收数据）。

## 11. 验收标准

1. `hermes plugins validate <src-dir>` 与 doctor 零 WARN（provides_hooks 齐、无私有 core import——registry 取数用公开函数）。
2. **D8 缺省即无害 + 播种纪律**：全新安装 enable 后首会话——config.yaml 恰好出现**一次** settings 播种（全键可见；observe=true、三个修改类开关=false）；此后连续 3 会话 mtime/sha 恒定（零再播种、零自写）；全开关 false 时外发 payload 与未装插件**逐字节一致**（dump 对照，含告知行零残留；observe=true 只加日志行，载荷不动）；删整段 settings → 重启重播一次；只删 `fold_tool_desc` 单键 → 重启后该键**保持缺失**且行为=false（不补种断言）；`/paperhermes` 面板只读、任何 verb 都不产生 config 写入。
3. 临时 HERMES_HOME 冷启动 + `hermes chat --oneshot` 真跑（开关置 true 后）：自检行出现、折叠计数>0、回程 200、告知行按 4.2 出现在 system 槽且同会话二次请求不重复注入（哨兵断言）。
4. 字节稳定断言（M1 表）连续 3 请求过。**会话冻结断言（5.2 缓存铁律）**：折叠形态按 (会话×模型) 冻结——同一会话同一模型的第 2..N 请求形态逐字节一致（中途改面板开关、删 hint 均不改变本会话）；会话内切换模型 = 按新模型名重选语言并重冻（缓存装饰本就逐 provider 重建，无损失），用两个模型连发断言各自形态。
5. Anthropic 缓存读率对照基线不降（M1 表）。
6. 单元测试打桩集覆盖：四协议形状定位（anthropic block-list 带 cache_control 的形态是重点假样本）、fold 幂等、80% 回退、**净益门两侧（回收 2047 不介入 / 2049 介入，告知字节计入净额）**、**语言选择表（有序先匹配：`qwen3.8-flash`→中、`gpt-4o`→英、未匹配兜底英、大小写混拼、两个表都命中的名字取先者）**、异常 fail-open、M2 区块正则不误伤 guidance（假样本：相邻块含 "## " 的行）、开关读取矩阵（缺键/true/false/非法值四种 × 每开关，observe 缺键=true）。
7. 断网/无 hint 文件/hint 损坏三场景：全部静默降级，零异常进会话。
8. 全开关回 false（或删键）+ 重启后：agent.log 无任何 `[PAPERHERMES]` 介入行、payload 与未装插件时逐字节一致（hermes dump 对照）。

## 12. 坑位记录

- **热重载重置冻结器（实测）**：`hermes plugins disable/enable` 会重新 import 插件模块，进程级 `Freezer` 清空 → 各 (会话×模型) 在下一次请求按**当前 settings** 重新定型。表现为"本会话本不该生效的开关在重载后生效了"（2026-11-09 实机：用户开满激进档后桌面会话 skills_folded 0→41）。这是缓存铁律在插件重载点的合法例外（重载本身就打断会话前缀），README 的行为冻结说明以此为准。
- **record_outcome 不回写日志行**：观测器先写 pre 行、再更新内存 last_folded，saved_chars 永远缺账。修法=把替换后的 request 作 `post=` 参数传入 record_request 一次成行（已修，10:14:46 起 observe.log 出现 saved_chars）。教训：append-only 日志的字段必须在**落盘那一刻**齐，事后更新内存等于没记。
- **notice_chars 恒 0（已知欠账）**：告知行字节没单独入账，只在 pre−post 净值里混合体现；不影响折叠正确性，回填 est 三元组时顺手拆。
- **validate 抓出两处静默违规**：middleware 注册未声明 `provides_middleware`（✗ FAIL）；`agent.coding_context` 不在 `_PUBLIC` 允许面（⚠ WARN，私有 import 会在上游重构断裂）。修法=manifest 补声明 + 18 类目表抄录为镜像常量（漂移后果有界：折错类目描述，名字恒在场）。
- **诊断归因要按 session_id/payload 大小分离两条线**：桌面会话（pre≈1M）与 oneshot（pre≈140K）的 skills_folded 计数各属各进程——桌面 0 的原因是"本会话冻结发生在开关拨 true 之前"（缓存铁律，正常），oneshot 的 41 才是真折档数。中途一度误猜"oneshot 没有 ## Skills 索引"，被后续日志证伪：`hermes -z` 载荷里索引存在且被正常折叠。教训：多进程混写一份 observe.log 时，先按 session_id 分组再解释计数。
- **execute_code 内核 import 核心模块可能缺依赖**（ruamel 不在 tools/ 解释器，PM venv 也踩自举重跑）——量核心渲染函数的脚本优先用当前内核（有环境继承），别绕 venv 探测两连败。
- **registry.get_all_tool_names() 覆盖面不可作折叠参照（实测翻案）**：该名单只含"已注册"工具（本解释器实测 52 名，delegate_task/skill_manage/memory/web_search/clarify 全部 False——built-in 与 inline executor 因进程/时机差异不保证在内）。拿它做 fail-closed 白名单会把一半核心工具漏折（17K→2.7K 实测差）。**正确参照=外发 tools 数组自身**：它就是 host 组装的本会话准入面，成员必可 dispatch，而可调用性与 payload description 无关（回程三闸门背书）。v0.4 据此把豁免收敛为 BUILTIN_KEEP∪keep_eager 两张表。附带教训：逐工具 try/except 会把逻辑错（`x in None` TypeError）吞成"零折叠"静默——计数为 0 且无 WARN 先怀疑吞异常。
- **热重载重冻副作用（实测）**：`plugins disable/enable` 重 import 插件 → 进程级 Freezer 清空，**已冻结的活跃会话会在下一次请求按当前 settings 重定型**（"本会话不受开关影响"的边界=无重载）。本轮验证链因此提前让桌面会话吃满了激进档，等于全开 A/B 真跑。验收 3/4 的"逐字节一致"断言须在无重载窗口测。
- **实测净省（2026-11-09 终审，saved_chars 直接载荷相减）**：oneshot 32,876 chars/请求（63 工具+41 技能行）、桌面 33,600 chars/请求（64+41）≈ **8.2–8.4K tok**，占原固定面（16K 级）的一半以上。冷召回 A/B（禁调 skills_list，两批发 7 问）全对：powerpoint/course-group-presentation/ffmpeg/maps/kotlin-basics/github-releases-download/bilibili-search + 天气负例判定，无一漏。

---

## 脱敏记录（公开版 v0.4，2026-11-10）

本版由私有母版脱敏生成，除下列各项外与母版逐行一致：

- 称呼统一：「主人」→「用户/作者」（全部；母版为用户对助手的称呼，无技术含义）。
- 上游审查件的本机绝对路径（头部）→ 调查件标题 + 「本地调查档案，未随仓库发布」。
- 作者技能库引用（3.1 节实测表来源、6 节解释器判定法）→ 「插件作者规范笔记」。
- 解释器路径中的开发机目录写法（6 节）→ `$HERMES_HOME` 相对表述。
- 「特批」表述（头部/D11）→ 「默认」；D11 的裁定语义不变。
- 各开关的最终默认值、D8 播种纪律、净益门数字均为设计契约本体，保留原样。
