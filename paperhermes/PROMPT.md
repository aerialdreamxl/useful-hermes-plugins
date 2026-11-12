# paperhermes 需求书

版本：v0.5.5（v0.4 已验收基线 + 2026-11-10 增量：首次 LLM 调用完成后的全会话绝对冻结（例外=`/paperhermes meltdown` 用户急停、模型切换视为新会话）、M0.5 构成账、M3 参数说明折叠、M4 defer 审计、M5/M6 结果处理、M7 技能触发句模式、M8 忠实摘要库；v0.5.3 追加：折叠保留规则句、参数折叠保留枚举字面量、技能告知行按净益取舍、告知语言认 `cn:` 前缀、净益门按实际告知行计字节、外置阈值降到宿主上限之下）
日期：2026-11-10
状态：**v0.4 已实现并验收；v0.5 为待实现的增量需求，旧实测不得当作新功能验收证据**
上游审查件：《Hermes 省 Token 介入点审查》（v0.23.0 源码取证 + 固定面实测，本地调查档案，未随仓库发布）

## 1. 一句话定位

一个 Hermes Agent 侧插件：以 `llm_request` 中间件为主接缝，对**外发载荷**做会话级字节稳定的瘦身；v0.5 新增的工具结果处理只使用官方 `transform_tool_result` 接缝——eager 工具 schema 的 description 折叠为一行摘要（全量可经 bridge 按需回读）、skills 索引低频类目降为名字行（描述可经 skills_list 按需回读）。它是官方 `tools.tool_search` defer（管"哪些工具整体延迟"）之下的**第二层**：管"留下的工具描述有多重"。不改安装树、不改核心配置语义、不删任何工具/技能实体。

**它不是什么**：不是压缩器（不动历史消息体，那是 proactive_prune/ContextEngine 的地盘）；不是新的 defer 列表（与 `tools.tool_search.defer` 正交共存）；不是系统提示词重写器（guidance 散文一字不动，只动 `## Skills` 块内行）。

## 2. 已冻结的决策

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 形态 | agent-side 插件（`register(ctx)`），主体 = `llm_request` 中间件 + `pre/post_api_request` 观测钩；v0.5 的 M5/M6 增加官方 `transform_tool_result` 接缝；无 dashboard/desktop 半体 |
| D2 | 能力承诺分档 | tool schema 描述折叠 = M1（高置信路径，验收门可写死）；skills 索引降档 = M2（默认关、opt-in、A/B 门裁定）——**需求书上不允许把两者打包成"无损"承诺** |
| D3 | ~~只削 description，params JSON Schema 永不改动~~ **v0.5 重裁：Schema 结构与语义不动，允许只折叠其中的 description 字符串（13.1/13.5）** | 实测依据见 3.1：description 占 schema 字节 55%；回程三闸门（intake 校验/registry dispatch/bridge 全量）全都不读 payload 里的 description |
| D4 | ~~会话级字节稳定优先于任何单请求最优~~ **v0.5 升级为首次 LLM 调用完成后的 session_id 级绝对冻结（13.2）** | ~~按描述 hash 在会话内重算记忆化~~ **v0.5：首请求生成完整快照，之后按名字复用同一文本；源描述变化、热重载、进程重启均不重冻。v0.5.1 例外：模型切换时前缀缓存已失效，视作新会话边界，允许按当前配置重冻（13.2 第 1/7 点）** |
| D5 | 白名单永不折叠 | `clarify`（上游 A/B 判例：defer 后结构化澄清 18/18→7/18）、用户在 `keep_eager` 配置里点名的工具、以及全部 INLINE_TOOL_EXECUTORS 面的工具 |
| D6 | 与官方 defer 的关系 | ~~不读取~~不覆写 `tools.tool_search.defer`；**v0.5 允许 M4 只读审计（13.6）**；官方已 defer 的工具本就不在 payload 里，paperhermes 只处理幸存者 |
| D7 | 观测先行 | M0 = 纯观测（pre/post 字节计数进插件日志与 `/paperhermes` 命令面板），采 ≥20 个真实请求的直方图后才开 M1 折叠 |
| D8 | **开关面（用户 2026-11-09 裁定，v0.3 修订版）**：每个功能一个独立 bool 开关，全部在 `plugins.entries.paperhermes.settings`，修改类行为默认 false；~~首启播种（seed-once）~~ **v0.5 重裁为逐键补齐（13.1/13.3）** | 旧 seed-once 细节仅作历史。现行规则：只补不存在的 key，显式 false/0/空字符串/空列表不覆盖；写配置走官方 writer，运行态/统计/快照不进 settings |
| D9 | **README 说明义务（用户 2026-11-09）**：每个选项必须写清"改什么、实测省多少 token、可能引发什么问题、怎么回滚"，见 7.1 | 省量数字必须由 M0/M1 实测填充，不许预填估计值 |
| D10 | **模型侧告知行（用户 2026-11-09）**：折叠介入时向 system 槽注入固定告知句（如"技能描述已折叠以节省上下文，名字全在场，按需 skills_list 搜索、skill_view 加载"） | 文案规格见 4.2；字节稳定 + 幂等哨兵检查；告知与折叠同生命周期（开关全关=零注入） |
| D11 | 观测开关 `observe` 的默认值 | **已裁定（11-09 #6）**：只读项默认 true——它只记日志不改载荷，"最大的问题只可能是 .log 大一点"；三个修改类开关（fold_tool_desc / fold_skills_index / fold_skills_dynamic）仍全 false |

## 3. 现实约束（实测，不是推测）

### 3.1 固定面构成（2026-11-09，参考会话，v0.23.0）

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
- `pre_api_request` 是**观测契约**（无替换权），kwargs 自带 `approx_input_tokens`/`request_char_count`/`tool_count`。`post_api_request` 带 `api_request_id` 与实际 `usage`，v0.5 用它关联真实 token 账，并作为首次 LLM 调用结束的冻结提交点（`agent/turn_response_intake.py:66-106`）。
- 中间件回调异常 = fail-open 原样发送（`apply_llm_request_middleware` 外层 try/except + WARNING），插件死不了会话，但**静默失效**要靠自检日志治理（见 5.4）。

### 3.3 环境事实

- 解释器：桌面 gateway 进程 = `$HERMES_HOME/tools/python-3.14.x/python.exe -m hermes_cli.main`（无 `-I`，PM venv 包在 site-packages）；测试解释器 = `$HERMES_HOME/installs/*/environments/*/venv/Scripts/python.exe`（判定法：`import httpx` 冒烟定位）。
- 插件加载时机：常驻桌面进程**进程启动时**加载，函数级改动需全量重启；`hermes chat --oneshot` 新进程可绕开常驻干扰做验收。
- 配置面：`plugins.entries.paperhermes.settings`（plugin.yaml `config_schema` 驱动 Settings→Plugins 面板），密钥不涉及。
- 已验证的协议范围：**anthropic_messages 优先**，chat_completions 次之，其余 mode 一律旁路。

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

v0.4 无新存储（hint 文件除外）；v0.5 新增冻结快照与可选 spill 文件，见 13.2/13.7。运行时状态两份，进程内、会话粒度：

```
fold_cache:  { (tool_name, sha1(desc原文)[:12]) -> folded_desc }     # D4 字节稳定锚
session_state: { session_key -> {mode, requests_seen, bytes_saved_cumulative, enabled_tiers} }
```

折叠函数 `fold_description(text) -> str`：取首句（到第一个"。/! ?/换行"），≤`fold_first_sentence_max_chars` 原样保留，超长截断 + `…`；**首句里保留触发词**（"Use when…/触发："类前缀不裁——它们是使用判据）。函数纯 stdlib、幂等、对 CJK 与 EN 混合文案实测过 20 个真实工具描述（取自 registry，不编样本）。

### 4.2 告知行规格（D10，v0.4 按裁定 #7 双语化）

折叠介入时向 **system 槽**追加固定告知句（tools 数组没有自由文本槽，两侧告知统一走 system 尾行），未介入的块不加。**每侧两套常量（EN/中文各一），会话首次折叠时按模型名选定一套并冻结到该模型段**——模型切换本就重建请求、前缀缓存失效，v0.5.1 起该段结束、按新模型重选并重新冻结；换个模型不算破坏缓存：

- **语言选择规则（用户 11-09 裁定）**：模型名小写化后从左到右扫关键词表，**先匹配者生效**：`qwen/deepseek/glm/ernie/step/minimax/kimi/spark` → 中文；`gpt/claude/gemini/grok/mistral` 或全不匹配 → 英文。实现=有序列表（非 dict，保"先匹配"语义），匹配用 `in` 子串。
- 工具面（`fold_tool_desc=true` 且本轮实际折叠 ≥1 个工具）：EN `Some tool descriptions are shortened to save context; search tool names with tool_search and read the full schema with tool_describe.` / 中「部分工具描述已折叠以节省上下文；需要完整说明时用 tool_search 搜索、tool_describe 读取。」
- skills 面（`fold_skills_index=true` 且确有类目降档）：EN `(Skill descriptions are folded here because the list is long — every name is listed; search with skills_list and load with skill_view(name) as usual.)` / 中「技能列表较长，此处仅列名称、描述已折叠——按需自行搜索：用 skills_list 查找，用 skill_view(名字) 加载，功能不受影响。」
- 四条硬规则：①**两套皆为常量字符串、零运行时插值**，选定后逐字节复用（D4）；②注入前**哨兵检查**——system 文本已含本句（或另一语言同位句）即跳过，防重复/双份；③文案点名找回路径且直给"自己去搜"的行动指令（裁定 #2）；④anthropic mode 下 system 若为 cache_control 块列表，告知句追加进**最后一个无断点块**，绝不新增/移动断点。
- ~~开关回 false 后告知行与折叠同时消失（无残留），payload 回归原始字节。~~ **v0.5：仅限首次调用提交前；冻结后只能下一会话改变（13.2）。**

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

全部折叠是纯字符串操作（regex + 截断 + dict 查找），单请求预算 **<5ms**（M0 实测门槛：22 工具 + 178 行索引全量折叠的 CPU 时间，用 PM venv 解释器计时 100 次取中位数）。零网络、零 LLM 调用；v0.4 除 hint 外零磁盘 IO，v0.5 允许首请求快照持久化与 M6 spill 写入，仍不得在每次请求重写冻结内容。

### 5.4 降级链

L1 折叠异常（任何形状意外）→ 该请求原样发送 + 计数 WARN（同因一次）；~~L2 连续 5 次后本会话自动关闭折叠并撤掉告知行~~ **v0.5：连续异常只记录与提示下一会话停用，不改变本会话冻结形态**；L3 registry import 失败 → 插件不注册，doctor 可见；永不因折叠失败让请求失败。自检指纹要求见 13.9。

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

README 必含一张**逐选项四列表**，每个 settings 键一行：**改什么**（一句话机制）/ **实测省多少**（M0/M1 数据回填：`observe=true` 采到的本会话 pre/post 直方图折算 tok/请求，标注测量条件与样本数，禁止预填估计值）/ **可能引发什么问题**（对应已知判例：M1=模型偶尔因薄描述误选工具→用 keep_eager 点名回补；M2=冷门技能想不起来→上游 18/18→7/18 判例风险、fold_skills_dynamic 缓解；告知行自身占用的字节同样实测入账）/ **怎么关**（键置 false 或删键并创建新会话，及熔断自动关的识别日志行）。表外另附：seed-once 播种行为说明（何时写 config、删整段会重播、删单键不补种）与 `## Skills`/tools 两处告知行原文样例，让人一眼知道模型会被额外告知什么。

## 8. 迁移与切换门禁

| 阶段 | 内容 | 过门指标 |
|---|---|---|
| M0 | 只观测：pre_api_request 直方图 + 折叠 dry-run 计数（不替换载荷） | ≥20 真实请求；折叠 dry-run 估算回收 ≥8K tok/请求；bedrock/codex 旁路判定 0 误入 |
| M1 | anthropic_messages + chat_completions 的 tools description 折叠，白名单在位 | **字节稳定断言**：连续 3 请求同一工具折叠输出 sha1 相同；Anthropic 缓存读率不降（用 post_api_request 的 cache stats 对照 M0 基线 ±3% 内）；工具调用错误率（"does not exist"/参数校验失败）与基线持平；A/B：同一 10 任务集（含必须用 defer 桥/execute_code 的任务）折叠 on/off 完成率不劣化 |
| M2 | skills 索引动态降档（默认关） | A/B：5 个"必须靠索引描述才能想起某技能"的任务（技能名冷门、无口头提示），折叠组 skill_view 触发率 ≥非折叠组 -1 次；用户口头裁决体感 |
| 回滚 | 本会话急停 = `/paperhermes meltdown`（立即原样透传，13.2 第 13 点）；永久回滚 = settings 功能开关改 false 或直接删键并创建新会话即全灭（缺省=false=不介入）；插件目录移出扫描根 | meltdown 后 payload 与原文逐字节一致（A27）；永久回滚 5 分钟内回到零干预态，且删开关不需要动 `plugins.enabled` 之外的任何配置 |

改 `plugins.enabled` / config 由用户本人执行（运维偏好），插件交付时给准确 YAML 块与命令。

## 9. 开放问题裁定记录（用户 2026-11-09 二轮，全部冻结）

| 编号 | 问题 | 裁定 |
|---|---|---|
| 9.1 | 折叠阈值/是否可调 | **可以设计成"量少的话开了也没用"**→ 净益门 `min_savings_chars`（5.1 步骤 3），`fold_first_sentence_max_chars` 同步开放为 settings 参数 |
| 9.2 | M2 初始低频集要不要首会话降 | **首会话就折叠**——"会话起来了就不好改了，毕竟有缓存"：一切折叠形态在会话首请求定型冻结（5.2 缓存铁律），不做中途升档；跨会话调整走 hint 文件 |
| 9.3 | codex_responses 的 prompt_cache_key 与改写关系 | **没有可验证该协议的测试环境**→ 未实测的协议一律旁路：codex mode 永久直通，不冒险（3.2 该行降级为历史记录） |
| 9.4 | bedrock 要不要做 | 没有可验证该协议的测试环境 → **永久旁路**（与 9.3 同一原则：无法实测=不实现） |
| 9.5 | 观测数据进桌面 UI？ | **纯 CLI**——UI 设计太麻烦；面板只 `/paperhermes` 命令 + `hermes paperhermes stats` 两个面 |
| 9.6 | `observe` 默认值 | **只读项可以默认 true**——"最大的问题只可能是 .log 大一点"（D11） |
| 9.7 | 告知行语言 | **按模型决定**：模型名从左到右匹配关键词先匹配生效、大小写不敏感；`Qwen/DeepSeek/GLM/ERNIE/Step/Minimax/Kimi/Spark`→中文，`GPT/Claude/Gemini/Grok/Mistral` 或未匹配→英文（4.2 语言选择规则） |

遗留的低风险默认值（不再询问，M0 数据回来后如明显不合理再向用户提请改判）：净益门 2048 chars、首句长度 120 chars、无 hint 时的静态降档集=官方 focus 类目表。

## 10. 非目标（明确不做）

- 不动历史消息体 / 不做摘要压缩（ContextEngine、proactive_prune 地盘，且每轮破坏缓存）。
- 不删工具、不隐藏技能名（"隐身"类改动一律拒绝，官方 discovery-regression 立场）。
- 不覆写 `tools.tool_search.defer`、不 patch 核心函数、不注册 transport。
- 不做跨模型商用的通用产品化（这是轻量单用户插件；泛化前置条件是 M1 验收数据）。

## 11. 验收标准

1. `hermes plugins validate <src-dir>` 与 doctor 零 WARN（provides_hooks 齐、无私有 core import——registry 取数用公开函数）。
2. **D8 缺省即无害**：插件 enable 且全部修改类开关为 false（或键缺失）时，外发 payload 与未装插件逐字节一致（含告知行零残留；observe=true 只加日志）；`/paperhermes` 不产生配置写入。~~旧版 seed-once、删单键永不补种等验收~~ **v0.5 改为逐键补齐，显式 false/0/空字符串/空列表不覆盖，完整断言见 A18。**
3. 临时 HERMES_HOME 冷启动 + `hermes chat --oneshot` 真跑（开关置 true 后）：自检行出现、折叠计数>0、回程 200、告知行按 4.2 出现在 system 槽且同会话二次请求不重复注入（哨兵断言）。
4. 字节稳定断言（M1 表）连续 3 请求过。~~旧版按（会话×模型）冻结、会话内换模型可重选语言并重冻~~ **v0.5 改为 session_id 级绝对冻结，协议切换、热重载、进程重启均不得重冻；v0.5.1 进一步裁定：模型切换时前缀缓存已失效，视作新会话，允许重冻（A28）。完整断言见 A9-A17、A28。**
5. Anthropic 缓存读率对照基线不降（M1 表）。
6. 单元测试打桩集覆盖：四协议形状定位（anthropic block-list 带 cache_control 的形态是重点假样本）、fold 幂等、80% 回退、**净益门两侧（回收 2047 不介入 / 2049 介入，告知字节计入净额）**、**语言选择表（有序先匹配：`qwen3.8-flash`→中、`gpt-4o`→英、未匹配兜底英、大小写混拼、两个表都命中的名字取先者）**、异常 fail-open、M2 区块正则不误伤 guidance（假样本：相邻块含 "## " 的行）、开关读取矩阵（缺键/true/false/非法值四种 × 每开关，observe 缺键=true）。
7. 断网/无 hint 文件/hint 损坏三场景：全部静默降级，零异常进会话。
8. 全开关回 false（或删键）并创建新会话后：agent.log 无任何 `[PAPERHERMES]` 介入行、payload 与未装插件时逐字节一致；同一 session_id 的热重载/重启不得借此撤掉已冻结修改。

## 12. 坑位记录

- **旧版行为，v0.5 起不再合法（13.2）——热重载重置冻结器（实测）**：`hermes plugins disable/enable` 会重新 import 插件模块，进程级 `Freezer` 清空 → 各 (会话×模型) 在下一次请求按**当前 settings** 重新定型。表现为"本会话本不该生效的开关在重载后生效了"（2026-11-09 实机：用户开满激进档后桌面会话 skills_folded 0→41）。这是缓存铁律在插件重载点的合法例外（重载本身就打断会话前缀），README 的行为冻结说明以此为准。
- **record_outcome 不回写日志行**：观测器先写 pre 行、再更新内存 last_folded，saved_chars 永远缺账。修法=把替换后的 request 作 `post=` 参数传入 record_request 一次成行（已修，10:14:46 起 observe.log 出现 saved_chars）。教训：append-only 日志的字段必须在**落盘那一刻**齐，事后更新内存等于没记。
- **notice_chars 恒 0（已知欠账）**：告知行字节没单独入账，只在 pre−post 净值里混合体现；不影响折叠正确性，回填 est 三元组时顺手拆。
- **validate 抓出两处静默违规**：middleware 注册未声明 `provides_middleware`（✗ FAIL）；`agent.coding_context` 不在 `_PUBLIC` 允许面（⚠ WARN，私有 import 会在上游重构断裂）。修法=manifest 补声明 + 18 类目表抄录为镜像常量（漂移后果有界：折错类目描述，名字恒在场）。
- **诊断归因要按 session_id/payload 大小分离两条线**：桌面会话（pre≈1M）与 oneshot（pre≈140K）的 skills_folded 计数各属各进程——桌面 0 的原因是"本会话冻结发生在开关拨 true 之前"（缓存铁律，正常），oneshot 的 41 才是真折档数。中途一度误猜"oneshot 没有 ## Skills 索引"，被后续日志证伪：`hermes -z` 载荷里索引存在且被正常折叠。教训：多进程混写一份 observe.log 时，先按 session_id 分组再解释计数。
- **execute_code 内核 import 核心模块可能缺依赖**（ruamel 不在 tools/ 解释器，PM venv 也踩自举重跑）——量核心渲染函数的脚本优先用当前内核（有环境继承），别绕 venv 探测两连败。
- **registry.get_all_tool_names() 覆盖面不可作折叠参照（实测翻案）**：该名单只含"已注册"工具（本解释器实测 52 名，delegate_task/skill_manage/memory/web_search/clarify 全部 False——built-in 与 inline executor 因进程/时机差异不保证在内）。拿它做 fail-closed 白名单会把一半核心工具漏折（17K→2.7K 实测差）。**正确参照=外发 tools 数组自身**：它就是 host 组装的本会话准入面，成员必可 dispatch，而可调用性与 payload description 无关（回程三闸门背书）。v0.4 据此把豁免收敛为 BUILTIN_KEEP∪keep_eager 两张表。附带教训：逐工具 try/except 会把逻辑错（`x in None` TypeError）吞成"零折叠"静默——计数为 0 且无 WARN 先怀疑吞异常。
- **旧版行为，v0.5 起不再合法（13.2）——热重载重冻副作用（实测）**：`plugins disable/enable` 重 import 插件 → 进程级 Freezer 清空，**已冻结的活跃会话会在下一次请求按当前 settings 重定型**（"本会话不受开关影响"的边界=无重载）。本轮验证链因此提前让桌面会话吃满了激进档，等于全开 A/B 真跑。验收 3/4 的"逐字节一致"断言须在无重载窗口测。
- **实测净省（2026-11-09 终审，saved_chars 直接载荷相减）**：oneshot 32,876 chars/请求（63 工具+41 技能行）、桌面 33,600 chars/请求（64+41）≈ **8.2–8.4K tok**，占原固定面（16K 级）的一半以上。冷召回 A/B（禁调 skills_list，两批发 7 问）全对：powerpoint/course-group-presentation/ffmpeg/maps/kotlin-basics/github-releases-download/bilibili-search + 天气负例判定，无一漏。

## 13. v0.5 增量需求（2026-11-10）

> 本节是已实现 v0.4 之后的增量契约。与前节冲突时，**以本节为准**；v0.4 的实测数字只证明当时已实现的 M0/M1/M2，不证明本节任何新功能已经实现或有效。

### 13.1 旧决策的重裁与不变项

| 旧条目 | v0.5 裁定 |
|---|---|
| D3「params JSON Schema 永不改动」 | 收窄为「**JSON Schema 结构与语义永不改动**」。允许 M3 只折叠其中名为 `description` 的说明字符串；`type/properties/required/items/enum/default/$ref/additionalProperties` 等键与值均不得改。 |
| D4「会话级字节稳定」及旧 5.2/11.4 的会话×模型冻结 | 升级为 **session_id 级绝对冻结**。首次 LLM 调用完成后，配置变化、provider 切换、动态技能信号、工具描述变化、插件热重载、进程重启均不得令本插件重新定型。**v0.5.1 唯一边界例外：模型切换 → 视为新会话（缓存已失效，重冻零成本），按当前配置重新定型；旧「会话内换模型可重冻」以这一新语义恢复，但不等于会话键退回 `(session_id, model)`——快照仍按 session_id 单键持久化，切换时替换该键的快照。** |
| D6「不读取不覆写官方 defer」 | 「不覆写」不变；M4 允许只读审计官方 defer 配置与实际工具目录，只输出建议，不自动修改。 |
| D8「seed-once」及旧 4.0/6/7.1/11.2 的播种语义 | 改为**逐键自动补齐**：仅当某个 key 不在 settings 时写入该 key 的默认值；用户显式保存的 `false`、`0`、`""`、`[]` 一律不覆盖。仍使用官方 writer，不在热路径反复写配置。 |
| 4.2「开关回 false 后告知行与折叠同时消失」 | 只对**尚未冻结的首次请求暂存阶段**成立。首次 LLM 调用完成后，关开关也不得撤掉本会话已有折叠或告知；新行为只进入下一会话。 |
| 5.4「连续 5 次异常后本会话自动关闭 M1」 | 作废。异常可以单次 fail-open，也可以记录「下一会话建议停用」，但不得在同一会话中途改变已冻结形态。 |
| 12 节「热重载是合法重冻例外」 | 作废。热重载、进程重启后若继续同一 session_id，必须恢复原冻结快照，不能按当前设置重建。 |
| —（新增） | **`/paperhermes meltdown` 用户急停**：唯一允许在冻结会话中途解除冻结的机制，规格见 13.2 第 13 点；它是「回到原始形态」而非「生成新形态」，不与冻结不变量冲突。 |

不变项：所有修改类开关默认 false；enable 后全部修改类开关为 false 时外发载荷逐字节不变；不删工具、不隐藏技能名、不改 JSON Schema 结构、不自动改官方配置、不支持未实测的 api_mode、不做历史消息摘要压缩。

### 13.2 最高优先级不变量：首次 LLM 调用完成后的全会话冻结

**用户新增原话契约**：「会话的首次 LLM 调用结束后，任何被插件修改过的东西就此冻结，不应有任何改动。新的改动只应在新会话生效。」

本插件将其落实为以下可测试机制。冻结对象是**模型可见的载荷、工具结果与处理策略**；只读观测日志、错误计数和用量账可继续追加，不属于对模型可见内容的新改动。

1. **会话键只有 session_id，但模型切换是等价的新会话边界**：持久化结构仍按 session_id 单键、不使用 `(session_id, model)` 复合键；同一 session_id 内换 provider、热重载、进程重启都不是新会话。**例外（用户 2026-11-10 裁定）**：`llm_request` 携带的 model 与快照里记录的 model 不同，即判定为「刚切换完模型的会话」——此时上游前缀缓存本已失效，重冻不引入任何额外缓存损失，因此删除该 session_id 的旧快照、按当前配置与新模型重新生成一份快照（含重新选择告知语言、重新过净益门、重新折叠），并在日志留一行可诊断记录。快照中必须存 `model` 字段；旧版盘上快照缺该字段时补记一次且**不改变形态**，之后再遇模型不同才触发重冻。`/paperhermes meltdown` 的熔断态是用户显式安全状态，不受模型切换撤销（仍保持原样透传）。
2. **首次请求暂存**：第一次 `llm_request` 根据当时设置与首请求载荷生成 `pending_snapshot`。首次调用的所有重试必须复用该快照，不得因重试、配置变化或工具结果到达而重算。
3. **调用完成提交**：以宿主第一次为该 session_id 触发 `post_api_request` 为「首次 LLM 调用结束」的提交点，将 pending snapshot 提交为 `frozen_snapshot`。若宿主没有触发该钩子，则不视为已提交；后续请求仍只能复用 pending snapshot，不能另起新形态。
4. **先持久化，后允许修改**：首次修改外发载荷之前，pending snapshot 必须已经原子写入插件私有状态目录；无法持久化时首请求直接 fail-open，不允许只在内存中修改。提交时只更新状态，不重新计算折叠内容。
5. **冻结内容完整覆盖**：所有功能开关、数值阈值、白名单/排除表、净益门结果、工具顶部折叠文本、参数说明折叠文本、技能降档集与触发句文本、告知语言与文案、结果清理/外置规则都必须进入快照。不得在 apply 路径重新读取 config、hints 或其他会话信号。
6. **静态替换结果按名字复用**：首请求已经折叠的工具/技能，在本会话后续请求中复用快照里的同一段文本；源描述改变也不得重折。首请求不存在的新工具、新技能保持原样，不加入折叠集；消失的工具/技能不得由插件补回。
7. **协议变化不产生新形态，模型变化产生一次新会话形态**：同会话换 provider、或两种已支持 api_mode 之间切换时，不重新选择告知语言、不重新评估净益门、不重新折叠；若不能把快照中的相同文本安全映射到新协议形状，本次请求原样旁路并记 WARN，不得生成第二套修改形态。**模型切换**按第 1 点走：删除旧快照、重新定型，这是缓存失效后的新会话，不视为同一会话内生成第二套形态。切换后连续同模型的请求仍须逐字节复用新快照。
8. **新工具结果只适用旧规则**：首次调用之后才产生的工具结果，可以按快照中已冻结的 JSON 清理、噪音清理或外置规则处理；这属于对**新输入执行旧规则**，不是允许策略漂移。阈值、排除表、保留长度和处理顺序不得改变。
9. **动态信号只影响新会话**：`skill_view` 等使用信号仍可记账，但只能在 `on_session_end` 后供新 session_id 使用。`fold_skills_dynamic=false` 时不得读取 hints，也不得保留其他会话的信号；任何会话都不得合并其他活跃会话的 signal 集。
10. **熔断不破坏冻结**：单次异常按 L1 原样发送；连续异常只累计并提示下一次新会话停用。不得在同一会话自动撤掉折叠、改阈值或换策略。
11. **热重载/重启不是新会话**：冻结快照以 `$HERMES_HOME/paperhermes/session_freezes.json` 原子持久化，插件重载后先按 session_id 恢复。快照损坏或缺失时，不得按当前配置重建一个已存在会话的新形态；应原样旁路并给出可诊断日志。
12. **生命周期清理**：不得仅凭 `on_session_end`、进程退出、TTL 或 LRU 删除冻结快照；只有宿主明确删除/重置该 session_id，或用户主动清理插件状态时才能删除。若宿主只有 `on_session_end` 而没有可靠删除语义，则保留快照，并在 `/paperhermes` 显示快照数量/体积与手动清理方法。
13. **唯一例外：`/paperhermes meltdown` 用户急停**。用户在输入框键入该命令，本会话立即解除冻结并进入熔断态：此后所有修改类行为（M1/M2/M3/M5/M6/M7 及告知行）一律原样透传。硬规约：①**只能由用户命令触发**——模型没有对应工具，插件自身、hook、定时器、其他插件均不得触发；②熔断状态写入该会话的冻结快照（如 `melted: true`），热重载/重启后同一 session_id 保持熔断；③不写 settings、不影响其他会话、不影响新会话（meltdown 是会话级运行态，不是配置变更）；④**本会话内不可撤销**，不提供 un-melt，恢复方式只有新会话；⑤只读观测 observe 不受影响，用于验证熔断后 payload 与原文逐字节一致；⑥触发时输出一行 `[PAPERHERMES] meltdown engaged (session=<id>)` 自检日志。

「新的改动只在新会话生效」同时适用于用户改配置、插件升级、hints 更新、defer 审计建议、动态技能升降档和异常熔断建议。`/paperhermes` 必须把这类变化显示为 `pending_next_session`，不得显示成当前会话已生效。**两类会话中途例外**：①用户显式键入的 meltdown（第 13 点）解除冻结、回到原始形态，不生成任何新形态；②模型切换（第 1/7 点）等价于宿主开新会话，允许生成新形态——因为前缀缓存已失效，这是重冻最便宜的时机，也让用户改过的配置无需重启即可生效。面板须显示当前快照的 model 与切换计数。

### 13.3 v0.5 新增开关与参数

所有键仍位于 `plugins.entries.paperhermes.settings`，并受 13.2 冻结约束。修改类行为全部默认 false；读取失败按默认处理。逐键补齐只补不存在的键。

| 键 | 类型 | 默认 | 行为 |
|---|---|---|---|
| `fold_tool_param_desc` | bool | false | M3：独立折叠工具 JSON Schema 内部的说明字符串；结构不变。与 `fold_tool_desc` 可分别开启、分别计账。 |
| `keep_param_desc` | list[str] | `[]` | M3 参数级保留表，条目格式为 `tool_name.param_name`；同名嵌套参数全部保留。`keep_eager` 仍豁免整个工具。 |
| `audit_defer` | bool | false | M4：允许 `/paperhermes` 生成冷工具与失效 defer 项建议；只读，不改载荷、不改官方配置。 |
| `minify_json_tool_results` | bool | false | M5a：仅对可完整解析的 JSON 结果去格式化空白。 |
| `strip_terminal_progress_noise` | bool | false | M5b：去 ANSI、进度条中间帧与连续重复行。 |
| `spill_large_tool_results` | bool | false | M6：对超过阈值的非 terminal 结果做本地外置，保留头尾与读取路径。 |
| `spill_min_chars` | int | `20000` | M6 触发阈值；须低于宿主 `tool_output.max_bytes`（默认 50000），否则永不触发。冻结后不可改。 |
| `spill_head_chars` | int | `4000` | M6 头部保留字符数。 |
| `spill_tail_chars` | int | `4000` | M6 尾部保留字符数。 |
| `spill_exclude_tools` | list[str] | `[]` | M6 按工具名排除；terminal 永远内建排除。 |
| `fold_skills_trigger_only` | bool | false | M7：技能索引保留名字与触发句/首句，而非仅名字。依赖 `fold_skills_index=true`。 |
| `use_compact_store` | bool | false | M8：工具描述改用辅助模型生成的忠实摘要（`hermes paperhermes compact-tools`）。缺失/过期的工具逐条退回首句折叠，不会让整轮折叠归零。 |

M3/M5/M6/M7 均必须实现独立 A/B 与回滚测试；不能因为共享已有折叠函数就跳过各自验收。

### 13.4 M0.5：请求构成与实际 Token 账本

在任何新增压缩功能开启前，先扩展只读观测。`llm_request` 与 `post_api_request` 通过 `api_request_id` 关联，记录：

- 固定面：`system_chars`、`tool_schema_chars`、`tool_top_description_chars`、`tool_param_description_chars`、`other_schema_chars`。
- 动态面：`user_chars`、`assistant_chars`、`tool_result_chars`，以及最大的若干结果之 `tool_name + chars`；不得记录工具结果正文、参数正文、密钥或会话隐私内容。
- 请求面：`api_call_count`、attempt 序号、pre/post chars、各模块独立 saved chars、notice added chars。
- 返回面：宿主 `usage` 中实际存在的 input/output/cached token 字段；端点未返回时记 `unknown`，禁止用字符数冒充 token 数。
- 账本口径：API attempt、用户 turn、session 三种统计分开；重试不得被当成新的用户任务，缓存命中不得被算成全额节省。

`notice_chars` 必须结束恒 0 的欠账。观测日志增加大小轮转，轮转不得修改冻结快照。M0.5 采样至少覆盖 20 个真实请求后，才允许在 README 填写 M3/M5/M6/M7 的实测省量。

### 13.5 M3：参数说明折叠，不动 Schema 结构

递归遍历工具 JSON Schema，仅处理键名严格等于 `description` 且值为字符串的位置。允许修改的只有这些字符串；其余键、值、顺序与引用语义均不得改变。

- 沿用首句折叠、80% 回退与净益门，但 M3 的 saved chars 单列。
- `keep_eager` 豁免整个工具；`keep_param_desc` 豁免指定工具的指定参数名。
- `tool_describe` 从 registry 读取完整 schema 的找回路径不变。
- M1 的「说明不影响工具能否 dispatch」不能直接外推成「参数说明不影响填参质量」。M3 验收必须单独统计参数校验失败、错误枚举、错误类型、漏填必填字段与任务完成率。
- 两种 wire 形态均覆盖：Anthropic `input_schema` 与 Chat Completions `function.parameters`。

### 13.6 M4：冷工具与失效 defer 项只读审计

`/paperhermes` 在 `audit_defer=true` 时可生成以下报告：

1. 描述/schema 体积较大且观测窗口内未调用的 eager 工具；
2. defer 配置中存在、当前实际工具目录中不存在对应名称的条目；
3. 工具合并、拆分或改名造成的旧配置残留；
4. 建议加入官方 `tools.tool_search.defer` 的候选及预期可移走的 schema chars。

报告只读，不写 `tools.tool_search.defer`，不自动把工具移出 payload，不覆盖官方默认 defer 集。`clarify` 与 D5 白名单永远不得进入自动建议；其他候选也必须由用户明确采纳后，在新会话生效。

### 13.7 M5/M6：工具结果清理与外置

**M5a JSON 迷你化**：只有当完整结果能由标准 JSON 解析器成功解析时才处理；使用紧凑分隔符重新序列化，字符串值与数组顺序不变。解析失败、截断 JSON、非 JSON 文本一律原样返回。

**M5b 终端噪音清理**：去 ANSI 控制序列、由回车/退格产生的进度条中间帧、连续完全重复行；保留最终进度状态、错误文本和退出码。terminal 同时存在专用与通用 transform 接缝时只能选一条，禁止双重处理。通用 `transform_tool_result` 必须显式排除 terminal。

**M6 大结果外置**：只处理非 terminal 且未被排除的工具结果。超过 `spill_min_chars` 后，将完整结果原子写入插件私有 spill 目录，上下文只保留：

- 原工具名、原始总字符数、省略字符数；
- 冻结的头部长度与尾部长度；
- 可供现有读文件工具访问的本地路径；
- 「中段被省略，需要时读取原文件」的明确说明。

spill 文件使用内容 hash 命名并防路径穿越；默认保留 24 小时，过期清理由插件后台惰性执行。该 TTL 只针对工具结果副本，不适用于 13.2 的会话冻结快照。外置功能涉及额外敏感数据副本，README 必须披露位置、保留期、删除方法与故障回滚。

M5/M6 位于结果入历史前的官方 transform 接缝。若与其他插件的 transform 冲突，不得静默覆盖；应检测冲突并对该结果 fail-open，同时记录冲突来源。

### 13.8 M7：技能触发句保留模式

`fold_skills_trigger_only=true` 时，所有适用技能行保留「技能名 + 触发句/首句」，不再采用仅名字行。技能名仍全部在场，已用技能的跨会话全量回补仍只在新会话生效。

M7 是与原 M2 不同的召回/省量折中，必须独立 A/B：冷门技能无口头提示时，比较纯名字行、触发句行、完整描述三组的技能发现率与 chars/tokens。未通过召回门前不得默认开启，也不得替换 v0.4 已验收的静态类目策略。

### 13.12 M8：忠实摘要库（可选，默认关）

`use_compact_store=true` 时，工具顶层描述不再用正则首句，而用辅助模型为每个工具生成的忠实摘要。摘要按「工具名 + 原文 sha256」校验，防止上游描述变化后仍套旧摘要。

- **生成入口**：`hermes paperhermes compact-tools`（`--dry-run` 只算不写，`--only a,b` 限定工具）。工具清单必须**现场构建**——`model_tools.get_tool_definitions(enabled_toolsets=<全部已知 toolset>, skip_tool_search_assembly=True)`，即当前进程能看到的全目录；不能只取默认工具面（实测 CLI 进程默认仅 12 个，会漏掉桌面会话里的插件工具），也不能依赖上一次会话收割的残档。进程内不可用时才退回 `tool_originals.json`，再退回 registry。生成按批打印进度（第 N/M 批、已合格/已拒绝计数），不允许长时间静默。
- **校验口径**：`description` 的原文 sha256 必须一致；`schema` 摘要只作软信号。载荷 schema 随 `api_mode` 变形（Anthropic 去 nullable union、Moonshot 清洗），按结构 hash 硬门会让换协议后整库失效——schema 不匹配时仍使用摘要描述，只记一笔并提示重建。
- **不许一刀不砍**：某工具在库里缺失、或描述已变时，退回 M1 首句折叠，而不是保留原文。`use_compact_store=true` 不得让整轮折叠归零；这是 v0.5 初版的事故形态，必须由测试钉死。
- **与 M3 正交**：命中摘要库的工具仍照常执行参数说明折叠，不得因为换了描述来源就跳过 M3。
- **安全闸门**：摘要不得丢弃原文中的强制规则句（must/never/always/required/必须/不要 等），不得长于原文；不合格条目保留原文。
- **库文件**：`$HERMES_HOME/paperhermes/tool_compact.json`，记录 `source`（live/harvested/registry）与 `generated_at`。可整删；删后自动退回首句折叠，不阻塞会话。

### 13.9 v0.4 缺陷修复并入本期

1. 首句切分支持中文 `。！？` 后无空格，以及英文句点后无空格的常见句界；小数、省略号与常见缩写不得误切。
2. Skills 告知句拼接前保证恰好一个换行，有无尾换行均不得黏到最后一个技能条目。
3. 实现或删除文档中的 `hermes paperhermes stats`；若保留入口，必须真实注册并验证聚合结果，不允许留假命令。
4. 热路径每会话首请求输出一次 `[PAPERHERMES]` 自检行，包含冻结状态与实际折叠计数；全开关 false 时不得出现介入行。
5. 配置改为逐键补齐，并补「显式 false/0/空字符串/空列表不被覆盖」「删单键后下次启动只补该键」测试。
6. README 必须说明 meltdown 的触发方式、作用范围（仅本会话）、不可撤销性、恢复方式（新会话）与自检日志行，避免用户把急停当成配置开关的替代品。

### 13.10 阶段门与交付顺序

| 阶段 | 内容 | 过门条件 |
|---|---|---|
| v0.4.1 | 13.2 全会话冻结、动态信号隔离、13.9 缺陷修复 | A9-A17 全部通过；旧 29 项测试不回退。 |
| v0.4.2 | M0.5 构成账、真实 usage、日志轮转 | 能按 session/turn/attempt 输出分块账；unknown 不伪造；observe=false 零观测写入。 |
| v0.5 | M3 参数说明折叠 + M4 defer 审计 | M3 参数错误率与任务成功率不劣化；M4 零配置写入。 |
| v0.5.1 | 模型切换视为新会话（快照增 `model` 字段、切换重冻、旧快照补记、熔断态粘滞） | A28 全部通过；旧 57 项测试不回退。 |
| v0.5.2 | M8 摘要库：现场构建工具清单 + 进度行 + schema 软校验 + 缺失即退回首句折叠 | A29/A30 通过；`use_compact_store=true` 下真实会话 `folded_tools` 非空。 |
| v0.5.3 | 折叠保规则句（首句 + 含禁令/强制标记的句子）、参数折叠保枚举字面量、技能档按告知行净益取舍、语言判定认 `cn:`/`zh:` 前缀、净益门按实际注入的告知行计、`spill_min_chars` 降到宿主上限之下 | A31-A34 通过；旧 66 项测试不回退。 |
| 后续实验 | M5/M6/M7 | 各自由 M0.5 数据证明有必要，且通过各自 A/B；未过门不进入默认推荐配置。 |

历史消息摘要、旧结果 proactive prune 与 micro-compaction 继续归 Hermes 原生机制；PaperHermes 不得借新增结果处理功能扩张成逐轮历史压缩器。

### 13.11 新增验收（编号续接旧 11 节）

- **A9 首调用提交点**：首次 `llm_request` 生成 pending snapshot；首次 `post_api_request` 后转为 frozen。钩子未触发时，多次重试的修改结果逐字节一致，且不产生第二份 pending。
- **A10 全参数冻结**：冻结后改变所有布尔开关、`keep_eager`、`keep_param_desc`、首句长度、净益门、spill 阈值/头尾长度/排除表，当前会话的第 2..N 次请求均保持原冻结形态；新 session_id 才采用新值。
- **A11 开关双向冻结**：首次调用时 off→on 与 on→off 两组都在同一会话中途翻转，均不得改变当前载荷；告知句也不得中途新增或撤销。
- **A12 模型/协议切换**：同一 session_id 换 provider、以及两种已支持协议之间切换，不重新选语言、不重新折叠、不重新过净益门；无法安全复用时原样旁路并 WARN。**模型切换改由 A28 裁定**（v0.5.1）。
- **A13 热重载与重启**：冻结后执行插件 disable/enable 或重启进程，再恢复同一 session_id，修改结果与冻结前一致；不得按新配置重建。新 session_id 不受旧快照限制。
- **A14 快照故障**：首次修改前持久化失败则首请求完全原样；冻结后快照损坏/缺失则原样旁路且日志可诊断，不得生成第二套折叠结果。
- **A15 动态信号隔离**：动态开关关闭时，旧 hints 与其他会话 signals 均零影响；两个并发会话分别查看不同技能，不得改变彼此的冻结保留集。所有信号只影响下一会话。
- **A16 新对象不扩集**：冻结后出现的新工具/新技能保持原样；既有对象源描述变化仍复用快照文本；消失对象不被插件补回。
- **A17 熔断不破冰**：连续异常达到旧阈值后，本会话不得自动关闭或改变折叠；只允许单次 fail-open、累计计数和下一会话建议。
- **A18 逐键补齐**：已有 settings 中显式 false/0/空字符串/空列表保持不变；缺失键在下次启动补齐；除缺失键外无其他 config diff。
- **A19 Schema 结构守恒**：M3 前后递归比较，除 `description` 字符串外所有键和值完全一致；两种协议均通过；`tool_describe` 仍返回完整原文。
- **A20 参数质量 A/B**：M3 开关两组跑同一工具任务集，参数校验失败、必填遗漏、类型/枚举错误和任务完成率不劣化；失败则 M3 不得交付为可用功能。
- **A21 defer 只读**：审计前后官方 config 与工具目录零写入；白名单不出现在建议中；失效旧名称能被报告；采纳建议后只在新会话改变工具面。
- **A22 JSON 无损**：M5a 对 JSON 做语义等值比较，解析后的对象完全相同；非 JSON、截断 JSON 原样返回。
- **A23 结果处理不双跑**：terminal 只经过一种清理路径；与其他 transform 插件冲突时 fail-open且有日志；新到结果始终使用冻结阈值与顺序。
- **A24 外置可恢复**：M6 省略段可从 spill 文件逐字节取回，头尾长度与省略数准确，路径不可逃逸插件目录；terminal 与排除工具永不外置；过期清理只删 spill，不删会话冻结快照。
- **A25 技能召回三组 A/B**：纯名字、触发句、完整描述三组比较冷门技能发现率；M7 未达非劣门槛不得保留为推荐选项。
- **A26 账本真实性**：input/cache usage 缺失时显示 unknown；重试不重复计用户 turn；notice_chars 等于实际注入字节；日志只含结构计数，不含结果正文或敏感参数。
- **A27 meltdown 急停**：冻结会话中键入 `/paperhermes meltdown` 后，自下一请求起 payload 与未装插件逐字节一致（含告知行零残留）；触发日志出现且 settings 零 diff；热重载/重启后同一 session_id 保持熔断；其他会话与新会话不受影响；模型经工具调用无法触发（无对应工具注册）。
- **A29 摘要库生成**（v0.5.2）：`compact-tools` 的工具清单来自现场构建的全目录而非残档；`--dry-run` 零写盘；每条摘要的原文 sha256 可复算；不合格摘要保留原文；进度行按批输出。
- **A30 摘要库失效不归零**（v0.5.2）：删除/清空 `tool_compact.json`、或只保留部分工具时，`use_compact_store=true` 下的计划仍按首句折叠产出非空 `folded_tools`，净益门照常评估；描述变化使某条失效时只影响该条；schema 漂移（换 api_mode）时描述仍可用且 `schema_ok=false`。
- **A28 模型切换即新会话**（v0.5.1）：①冻结后把 model 从 `qwen3.8-flash` 换为 `gpt-4o`，下一次请求按新模型重新定型——告知行换语言、净益门与折叠文本按当前配置重算，日志出现切换记录；②切换后连续两次同模型请求修改结果逐字节一致；③重载进程后同一 session_id 恢复的是切换后的新快照，不得误判为又一次切换；④旧版盘上快照缺 `model` 字段时补记一次且形态不变；⑤熔断态下的会话切换模型仍保持原样透传，`melted` 不被撤销；⑥快照文件仍是 session_id 单键，边界只剩一份快照，不残留旧模型条目。
- **A31 规则句不丢**（v0.5.3）：折叠后的工具描述必须仍含原文中所有带 `must/never/do not/always/only/必须/禁止/务必/严禁` 等标记的句子（受 `rule_budget` 封顶）；折叠函数幂等；无标记句照常删除；`terminal` 折后仍含 "never pipe through head/tail"，`write_file` 仍含 "do NOT re-read the file"。实机 32 个被折工具在启用规则句保留后由 13,330 降至 11,472 字符回收，规则句零丢失。
- **A32 枚举不丢**（v0.5.3）：参数 schema 带 `enum` 时，折叠文本必须包含全部枚举字面量，否则该参数不折；`lcm_grep` 的 `mode`/`content_scope`/`sort`/`session_scope`/`role` 与 `lcm_recall` 的 `include`/`detail` 在实机快照里保持原文，`query`/`limit` 等无枚举参数照常折叠。
- **A33 告知行净益**（v0.5.3）：技能档折出的字符不超过告知行长度时，既不折也不注入告知行（实机触发句档 1 行/32 字符 vs 148 字符告知，净亏消除）；净益门按**实际会注入的那条**告知行计字节（compact 告知带库路径，比短句长 200+ 字符）；`cn:`/`zh:` 前缀模型判为中文告知。
- **A34 外置阈值可达**（v0.5.3）：`spill_min_chars` 默认 20,000，低于宿主 `tool_output.max_bytes` 默认 50,000，否则 terminal 结果先被宿主截断、插件永不触发；M5/M6 仍只受各自开关控制，不随折叠净益门 `act` 联动（折叠不划算不应连带关掉用户显式打开的结果清理）。

---

## v0.5.4 增量（2026-11-10，技能主动发现提示）

- **A35 独立开关**：`inject_skill_search_hint`（bool，默认 false）放 `plugins.entries.paperhermes.settings`，由 `config_schema` 声明、逐键补齐；不进 `MOD_SWITCHES`（全关折叠时不单独介入）。
- **A36 触发条件**：提示的注入条件是「本会话折叠实际介入」（`decision.act` 且本轮确有折叠落盘），**不是**「`fold_skills_index` 折过技能行」。依据：`local-62d0334f…`（`global:glm-5.3-flash`）冻结快照 `folded_skills=0`、31 个工具描述 + 22 组参数被折，注入的 208 字符告知行只讲工具原文回读；技能索引全在场，模型仍没有主动找技能。补的是行为指令，不是索引内容。
- **A37 告知经济**：提示文本按 `lang`（`cn:`/`zh:` 前缀判中文）选，净益门 `notice_bytes` 按**实际注入的那条**计字节，不用短句估算；旧盘上快照缺 `notice_hint` 键时 `sanitize_snapshot` 补空串，冻结形态不随版本升级漂移。实机 78,351 字符载荷接线实测：开提示后 61,903 字符、回收 16,563→16,448（每请求多 115 字符），`est[3]` 从 208 升到 322，门控按实际文本计。
- **A38 幂等**：`system_slot_apply` 用「文本已含该句则跳过」的哨兵，同句不会注入两次；与 `NOTICES["skills"]`、`compact_notice` 三条告知可共存，各占一行。
- **坑（2026-11-10）**：技能索引没被折 ≠ 模型知道要去搜技能。把「技能提示」挂在技能折叠分支上，恰好漏掉「只折工具描述」的多数会话；告知行的触发粒度必须对齐「插件是否介入」这一层，而不是对齐被折的区块。

---

## v0.5.4 补遗（2026-11-11 评审落档：代码已有、需求书未载的功能与缺陷）

**补回需求书的功能（代码已实现，本节之前未入册）**：

- **`observe_composition` 开关（bool，默认 false）**：M0.5 的量具开关，位于 settings 并进 `config_schema`/逐键补齐；true 时每请求记分区字节账（`payload_composition`）并经 `post_api_request` 按 `api_request_id` 关联真实 usage 回执。此前只在 13.4 描述了账本行为，没把它登记为开关面成员（4.0/13.3 两张表均缺）。
- **`/paperhermes rearm`（连续两次、120 秒内确认）**：第二个会话中途例外——删除本会话冻结快照，下一请求按当前配置重定型（会断一次前缀缓存）。**修正 13.2 第 13 点的「唯一例外」表述**：会话中途解除冻结的机制现有两个，meltdown（回到原始形态、不可撤销）与 rearm（按当前配置重定型、可反复用）。rearm 同样只能由用户命令触发、同样不写 settings。
- **辅助模型槽注册**：`register(ctx)` 里 `ctx.register_auxiliary_task("paperhermes_compact", inherit_from="compression")`——`hermes paperhermes compact-tools` 走独立辅助槽（不可用时退回宿主默认辅助模型）。13.12 只写了生成入口，没写槽位注册与回退链。
- **`tool_originals.json` 田间收割**：每次 `llm_request` 折叠前从原始载荷收割工具原文（内容变化才落盘，原子写），供 compact-tools 现场目录之外的 agent 运行时工具（lcm_*/minimem_* 等）补齐。13.12 只把它当「退回来源」提了一句。
- **M5b 实际范围**：`strip_terminal_progress_noise` 只走通用 `transform_tool_result`，**terminal 结果恒排除**（专用 `transform_terminal_output` 接缝未接）。即「终端噪音清理」目前只作用于非 terminal 工具结果里的 ANSI/`\r` 帧/重复行；terminal 自身输出不经本插件清理（13.7 的「只能选一条」以「选通用、排除 terminal」落场）。
- **spill 提示语固定中文**：`SPILL_NOTICE_ZH` 是唯一模板，不随模型语言切换（4.2/9.7 的语言规则未外推到 M6 结果通知）。英文模型会看到中文省略提示。

**评审发现的缺陷（待修，按影响排序）**：

1. **spill 24 小时 TTL 未实现**（13.7 要求「后台惰性清理」；实机 spill/ 已 1.5 MB/16 文件只增不减，README 的保留期披露也无法如实写）。
2. **observe.log 大小轮转未实现**（13.4/v0.4.2 门；实机已 2.1 MB 无上限增长）。
3. **`hermes paperhermes stats` CLI 未注册**（13.9.3 要求实现或删；代码只注册了 compact-tools 子命令，stats 只在聊天命令 `/paperhermes stats` 里实现）。
4. **跨进程快照竞态**：`_persist_freezes` 整文件原子重写、以进程内 `_snapshots` 全量为准——网关与 oneshot 两个进程并发时后写者会覆盖掉对方新增的快照（第 6 节只给 hint 规定了 O_EXCL 锁，冻结文件无任何锁）。
5. **hint 并发锁未实现**（第 6 节 O_EXCL+2s 超时；现为 last-writer-wins，多会话同刻结束会丢信号）。
6. **依赖键缺 WARN**：`fold_skills_dynamic`/`fold_skills_trigger_only` 在 `fold_skills_index=false` 时静默无效，4.0 规定的「WARN 一次」未实现。
7. **`error_streak` 只增不清**：成功请求不重置计数，5 次*非连续*异常也会触发 suggest_disable_next，违背「连续异常」语义。
8. **README 开关表 `spill_min_chars` 默认值笔误**：表写 50000，代码/plugin.yaml 实为 20000（正是 v0.5.3 特意降下来的那个值）。
9. **`_compact_for_plan` docstring 与行为相反**：注释说库缺失时「工具描述一律不折」，实际（正确地）退回首句折叠——按 13.12「不许一刀不砍」的口径，注释应改。
10. **transform 冲突检测未实现**（13.7「与其他插件冲突时 fail-open 并记来源」；现为静默链式处理）。

---

## v0.5.5 增量（2026-11-11，评审缺陷 #1/#2/#3/#6/#7 修复）

- **A39 spill TTL**（#1）：`on_session_end` 节流 1 小时做惰性清理，删 mtime 超 24h 的 spill 文件与空会话子目录；不设独立开关（资源卫生，非修改类行为）；删不掉只 debug。13.7「后台清理」落场为「会话末惰性清理」——不引后台线程，不抢锁。
- **A40 observe.log 轮转**（#2）：`Observer._write` 内联检查，超 10 MB 把旧档 `os.replace` 成 `.1`（覆盖旧 `.1`，保留一份）；轮转失败 fail-open 继续追加。阈值常量 `OBSERVE_LOG_MAX_BYTES`，暂不进开关面（先跑，嫌吵再开）。
- **A41 stats CLI**（#3）：13.9.3 的「实现或删」选实现——`hermes paperhermes stats` 子命令，直接复用 `/paperhermes stats` 的 `_stats()`（同一 observe.log 账本）。
- **A42 依赖键 WARN**（#6）：注册期读补齐后的 settings，`fold_skills_dynamic`/`fold_skills_trigger_only` 为 true 而 `fold_skills_index` 为 false 时 WARN 一次（4.0 原义）。
- **A43 连续语义**（#7）：`error_streak` 在成功请求后清零；5 次散布异常不再触发 `suggest_disable_next`。bypass-mode/snapshot-corrupt 不算成功也不清零（中性事件）。
- **未修保留**（#4/#5/#10）：跨进程快照竞态、hint O_EXCL 锁、transform 冲突检测——留 v0.5.6+；触发条件分别是「网关与 oneshot 并发写 freezes」「多会话同刻结束」「同接缝双插件」。
- **A44 CLI 无参防误触 + stats 输出**（2026-11-11 事故实录）：宿主在无子命令时仍调 handler_fn（main.py:3142 `set_defaults(func=handler_fn)` 顶层兜底），原先直落 `_run_compact_tools` —— 一次手滑的 `hermes paperhermes` 跑满 21 批辅助调用并整库重写了 tool_compact.json（64→58 条；实测新库命中 23 > 旧库 15，未回滚，token 已耗）。修复：`_cli_handler` 只认 `ph_cmd == "compact-tools"`，其余一律打印用法退出；`stats` 子命令必须**自己 print**——宿主只把返回值当退出码（main.py:3443 `rc = args.func(args)`），字符串返回值被静默丢弃。argparse 实测：subparser 的 `set_defaults(func)` 覆盖顶层 `handler_fn`，子命令路由成立。

---

## 脱敏记录（公开版 v0.5，2026-11-10）

v0.4 基线由私有母版脱敏生成；v0.5 增量直接维护在这份公开契约中，不引用私有母版内容。v0.4 基线除下列各项外与母版一致：

- 私有称呼统一为「用户/作者」（全部；无技术含义）。
- 上游审查件的本机绝对路径（头部）→ 调查件标题 + 「本地调查档案，未随仓库发布」。
- 作者技能库引用（3.1 节实测表来源、6 节解释器判定法）→ 「插件作者规范笔记」。
- 解释器路径中的开发机目录写法（6 节）→ `$HERMES_HOME` 相对表述。
- 「特批」表述（头部/D11）→ 「默认」；D11 的裁定语义不变。
- 各开关的最终默认值与净益门数字属于设计契约本体，保留；D8 播种纪律已在 v0.5 重裁，见 13.1。
- 具体模型槽标识与账户相关 URL 片段（3.3 节）→ 仅保留已验证协议范围。
- 个人账号有无、个人使用场景与「本机个人插件」表述（9.3/9.4/10 节）→ 中性的测试环境或产品定位描述。
- v0.5 只使用 `$HERMES_HOME` 相对路径、协议名称与匿名测试编号，不包含本机绝对路径、个人配置、真实会话内容或 API 凭据。
