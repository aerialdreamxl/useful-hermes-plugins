# paperhermes — 外发载荷瘦身（v0.5.5）

名字取材自 PaperMC（Minecraft 服务端发行版），机制上确实是同一类事情：不动内容本体，只把**每次请求都要重发的固定开销**削薄。

它在 `llm_request` 中间件与 `transform_tool_result` 钩子两个官方接缝上做三类事：

- **M1/M3**：外发 `tools` 数组里各工具的 `description` 与参数 schema 内的 `description` 字符串折为首句摘要（默认 ≤120 字符）。**v0.5.3 起两条硬约束**：①首句之后凡是带 `must/never/do not/always/only/必须/禁止/务必/严禁` 等标记的句子必须保留（受 `rule_budget` 封顶 260 字符），安全与纪律规则不再被折掉；②参数 schema 带 `enum` 时，折叠文本必须仍含全部枚举字面量，否则该参数不折（`lcm_grep.mode` 的 full_text/semantic/hybrid 差异不会再消失）。JSON Schema 的结构与语义（type/properties/required/enum/default/$ref）一字不动，工具照常调，全量描述随时可经注册表回读。
- **v0.5.4 技能主动发现提示**（`inject_skill_search_hint`，默认关）：只要本会话折叠实际介入，就在 system 多注入一句「开工前主动 `skills_list` 搜索、`skill_view` 加载」的行为指令。触发条件是**插件介入**，不是「技能索引被折」——实机 `local-62d0334f…` / `global:glm-5.3-flash` 会话里技能索引一条没砍（`folded_skills=0`），模型仍没主动找技能，缺的是行为提醒而非索引内容。中文 113 字符 / 英文 325 字符，纯支出，不额外折任何东西。实机 78,351 字符载荷接线实测：开提示后 61,903 字符，回收从 16,563 降到 16,448，**每请求多 115 字符**；告知行只注入一次（哨兵去重）。
- **v0.5.5 CLI 防误触**：`hermes paperhermes` 无子命令时只打印用法，不再默认跑 compact-tools 批处理（此前的行为一次误触就烧 21 批辅助调用并整库重写摘要库）；`hermes paperhermes stats` 现在会实际打印账本（此前静默无输出）。
- **v0.5.5 资源卫生**（评审 #1/#2/#3/#6/#7）：spill 文件 24 小时惰性清理（on_session_end 节流 1 小时；删的是超龄文件，绝不动活跃内容）；observe.log 超 10 MB 轮转为 `.1`（保留一份，轮转失败仅继续追加）；`hermes paperhermes stats` CLI 子命令补上（与 `/paperhermes stats` 同一账本）；`fold_skills_dynamic`/`fold_skills_trigger_only` 开着而 `fold_skills_index` 没开时注册期 WARN 一次；`error_streak` 在成功请求后清零，「连续异常」语义恢复。仍未做：跨进程快照锁（#4）、hint 锁（#5）、transform 冲突检测（#10）——见 PROMPT.md 补遗。
- **M2/M7**：system prompt 的 `## Skills` 区块里低频类目降为名字行（M2）或保留触发句（M7）。技能名全部在场。**v0.5.3 起按净益取舍**：折出来的字符不够付告知行就不折、也不注入告知行（实机触发句档折 1 行省 32 字符却注入 148 字符告知，净亏已消除）。
- **M5/M6**：工具结果的可选项——JSON 去格式化、ANSI/进度条噪音清理、超大结果外置到本地文件（头尾保留+完整可取回）。terminal 工具结果**永远不处理**（宿主已渲染）。外置阈值默认 20,000（宿主 terminal 输出上限是 50,000，阈值设 50,000 等于永不触发）。这三项只受各自开关控制，不随折叠净益门联动。

它不是压缩器（不动历史消息体）、不是 defer 的替代（`tools.tool_search.defer` 正交共存，M4 可只读审计其配置）、不是提示词重写器。

## 行为契约 v0.5.5（用前必读）

- **首次 LLM 调用完成后的全会话绝对冻结**：折叠形态在会话首个请求定型（pending 快照先原子落盘，落盘失败则本轮不修改），以宿主首次 `post_api_request` 为提交点转为 frozen。此后**换协议、改开关（无论开还是关）、改阈值、插件热重载、进程重启，一律不改变本会话形态**——前缀缓存骑在 system 与 tools 字节上，形态逐请求漂移 = 缓存全毁，倒亏比省的多。新改动只在**新会话**生效（`/paperhermes` 面板会显示哪些键处于 `pending_next_session`）。
- **模型切换 = 新会话（v0.5.1）**：换模型时上游前缀缓存本来就没了，重冻不额外损失缓存，所以插件把这次请求当作新会话：删掉该会话的旧快照，按当前配置与新模型重新定型（告知行语言、净益门、折叠文本全部重算），并记一行切换日志。快照里存 `model` 字段；旧版盘上快照缺该字段时补记一次且形态不变。**这是让改过的配置无需重启就生效的最省事办法**。快照仍按 session_id 单键持久化，切换是替换该键而非新增条目；面板显示当前 model 与累计切换次数。换 provider、换 api_mode 不算新会话（仍冻结）。
- **会话中途例外之一——急停**：`/paperhermes meltdown`。键入后本会话立即解除冻结、回到原始形态（所有修改类行为原样透传，告知行消失）。只能由用户在输入框触发（模型没有对应工具）；熔断态持久化，热重载/重启后同会话保持；**本会话内不可撤销**，恢复方式是开新会话（新会话按配置正常生效）。它不写 settings、不影响其他会话。
- **会话中途例外之二——模型切换**：见上条；切换后连续同模型的请求仍逐字节复用新快照。`meltdown` 熔断态是用户显式安全状态，切模型也不会把它撤销。
- **协议旁路**：只对 `chat_completions` / `anthropic_messages` 生效；同会话中途切换协议 → 原样旁路 + WARN（不生成第二套形态）。codex_responses / bedrock 未实测，永久旁路。
- **逐键补齐（v0.5，取代旧 seed-once）**：启动时只向 settings 补**不存在的键**；你显式存的 `false`/`0`/`""`/`[]` 永不覆盖。删掉的单键下次启动会补回默认值。
- **熔断纪律**：单次异常该请求原样发送 + WARN；连续 5 次只在快照里记"建议下一会话停用"，本会话形态不变。
- **模型侧告知行**：折叠实际介入时向 system 注入一句固定告知（中/英按模型名关键词有序先匹配），**另起一行不黏连正文**；语言按当前模型段选定，模型切换后重选；开关全关或 meltdown 后零残留。**v0.5.4**：`inject_skill_search_hint=true` 时同批注入技能主动发现指令，按「插件是否介入」触发，与 `fold_skills_index` 独立；该开关不在 `MOD_SWITCHES` 里，全关折叠时不会单独注入。
- **动态信号**：`skill_view` 用过的技能在会话末记入 hints，**只有** `fold_skills_dynamic=true` 时读；不合并其他活跃会话的信号。

## 开关一览（每个选项：改什么 / 实测省多少 / 会引发什么 / 怎么回滚）

实测省多少列 v0.5 新功能**全部待 M0.5 账本回填**（13.9 禁令：不许预填估计值）。M1/M2 沿用 v0.4 实测（见下节）。

| 键 | 默认 | 改什么 | 实测省多少 | 会引发什么 | 回滚 |
|---|---|---|---|---|---|
| `observe` | **true** | 只记日志，不改载荷 | — | .log 变大，可随手删 | false |
| `observe_composition` | false | M0.5：每请求记分区字节账 + 真实 usage 回执关联 | —（这是量具本身） | 日志行更粗更大 | false |
| `fold_tool_desc` | false | M1 工具顶层描述折叠 | 中位 26.6K chars/请求（v0.4 实测） | 弱模型可能选错工具；clarify/keep_eager 永不折 | false（新会话生效；急用 meltdown） |
| `fold_tool_param_desc` | false | M3 参数 schema 内 description 折叠，结构键不动 | 待账本回填 | 模型填参缺提示——先开 composition 看 param_desc_chars 占比再决定；keep_param_desc 逐参豁免 | false |
| `fold_skills_index` | false | M2 技能索引降名字行 | v0.4 实测含于上行合计 | 不主动 skills_list 就看不到类目描述（告知行会提示） | false |
| `fold_skills_dynamic` | false | M2 增强：view 过的技能下会话保全量（依赖 fold_skills_index） | — | hints 跨会话持久（30 天 TTL） | false |
| `fold_skills_trigger_only` | false | M7：技能行保留触发句而非仅名字（依赖 fold_skills_index） | 省量小于 M2 | 召回 A/B（三组）未过门前不建议开 | false |
| `inject_skill_search_hint` | false | v0.5.4：折叠介入时注入一句主动搜索技能的指令（中/英按模型名选） | −113（zh）/ −325（en）chars/请求，纯支出 | 告知行变长，净益门按实际注入文本计；旧冻结快照缺该键按空串，形态不漂移 | false |
| `use_compact_store` | false | M8：工具描述改用辅助模型摘要（先跑 `hermes paperhermes compact-tools` 建库） | 待账本回填（摘要通常比首句更短、信息更全） | 摘要库缺失/过期时该工具自动退回首句折叠，**不会整轮失效**；库文件可整删 | false（或删 tool_compact.json） |
| `keep_eager` / `keep_param_desc` | `[]` | 点名豁免（工具名 / `tool.param`） | 省量变小 | — | 清列表 |
| `fold_first_sentence_max_chars` | 120 | 首句长度上限 | 调大省得少 | 调小信息少 | 回 120 |
| `min_savings_chars` | 2048 | 净益门（预估回收−告知字节） | — | 载荷小的会话不折腾 | 回 2048 |
| `audit_defer` | false | M4：面板显示失效 defer 项/重工具候选 | —（只读） | 无 | false |
| `minify_json_tool_results` | false | M5a：完整 JSON 结果去缩进 | 待账本回填 | 结果人类可读性下降（信息无损） | false |
| `strip_terminal_progress_noise` | false | M5b：去 ANSI/进度条中间帧/连续重复行 | 待账本回填 | 极少数靠 `\r` 帧计数的解读受影响 | false |
| `spill_large_tool_results` | false | M6：>阈值结果写 spill/，载荷留头尾+路径 | 待账本回填 | 模型看不到中段（提示句给了逐字节取回路径）；召回 A/B 未过门前不建议开 | false + 清 spill/ |
| `spill_min/head/tail_chars`、`spill_exclude_tools` | 20000/4000/4000/`[]` | M6 参数 | — | terminal 永远内建排除 | 回默认 |

v0.4 实测（M1+M2，作者固定面 30-64 工具 + 至多 163 技能，七模型）：单请求回收中位 26.6K chars（区间 3.5K–40K），约占固定面一半以上，相对整包载荷 ≈5%。M3/M5/M6 的省量以你的 `observe_composition` 账本为准。

## 安装与启用

放进 `$HERMES_HOME/plugins/`（或分类子目录），`plugins.enabled` 加 `paperhermes`，重启。enable 只是插件在场；所有修改类开关默认 false。开关在 `Settings → Plugins → paperhermes`（即 `plugins.entries.paperhermes.settings`），改动由你本人提交（面板只读，不代写）。

## 盘外落点（全部可删）

```
$HERMES_HOME/paperhermes/observe.log          # M0/M0.5 观测账（JSONL）
$HERMES_HOME/paperhermes/session_hints.json   # M2 动态集（30 天 TTL）
$HERMES_HOME/paperhermes/session_freezes.json # 冻结快照（删了=活跃会话本回合起重定型，谨慎）
$HERMES_HOME/paperhermes/tool_compact.json    # M8 摘要库（可整删；删后自动退回首句折叠）
$HERMES_HOME/paperhermes/spill/               # M6 外置原文（按会话子目录；可整删）
```

快照清理：宿主没有"删除会话"语义，插件只在用户主动清理状态目录时删快照；`/paperhermes` 面板显示快照数量与体积。

## `/paperhermes` 命令

- 无参 = 状态面板：开关读取值、本会话冻结状态（含当前 model 与模型切换次数）、pending_next_session 键、精简库状态、defer 审计（开了才显示）、快照统计。
- `stats` = observe.log 账本：请求数、fold 数、累计回收、中位单次、usage 回执行数。
- `meltdown` = **本会话急停**（见行为契约）。触发方式：输入框键入 `/paperhermes meltdown`；作用范围仅本会话；不可撤销；恢复=新会话；自检日志打 `[PAPERHERMES] meltdown engaged (session=…)`。
- `rearm` = **删掉本会话快照重定型**，需要【连续输入两次】（120 秒内）才执行，会断一次缓存前缀；用于不想重启也不想换模型的中途重定型。
- CLI：`hermes paperhermes compact-tools` 生成/刷新 M8 工具摘要库（`tool_compact.json`），`--dry-run` 只看不写，`--only a,b` 限定工具；工具清单在命令运行时**现场构建**（当前进程的全目录），并逐批打印进度。模型走 `paperhermes_compact` 槽（继承 `compression`）。

## 已知局限

- 绝对冻结的代价：同会话改开关无法热生效。三条出路：开新会话、切换一次模型（v0.5.1 自动重冻，缓存本来就没了）、或连续两次 `/paperhermes rearm`；急用零干预用 meltdown。插件宿主进程在启动时持有配置快照，直接手改磁盘 `config.yaml` 不热生效，仍需重启或走上面三条路。
- codex_responses / bedrock 旁路（不可测=不实现）。
- M3 填参质量、M6 召回率、M7 触发句召回三项 A/B 未跑，对应开关默认 false 且面板不推荐。**v0.5.4 技能主动发现提示同样未过 A/B**：它只改一句指令的措辞，是否真能提升小模型的技能命中率需要同会话 5 轮对比验证，不能当已验收结论。M3 的枚举保留规则（v0.5.3）只是消除"折掉枚举导致盲选"这一类隐性错误，不等于填参质量已验。

完整设计契约与逐条裁定见同目录 `PROMPT.md`（v0.5.5）。
