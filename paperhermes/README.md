# paperhermes — 外发载荷瘦身（工具描述与技能索引折叠）

名字取材自 PaperMC（Minecraft 服务端发行版），机制上确实是同一类事情：不动内容本体，只把**每次请求都要重发的固定开销**削薄。

它在 `llm_request` 中间件这一个官方接缝上做两件事：

- **M1**：外发 `tools` 数组里各工具的 `description` 折为首句摘要（默认 ≤120 字符）。参数 JSON Schema 与可调用性一字不动——工具照常调，全量描述随时可经工具注册表回读。
- **M2**：system prompt 的 `## Skills` 区块里，低频类目的技能降为名字行。技能名全部在场，描述可经 `skills_list` / `skill_view` 按需回读。

它是官方 `tools.tool_search` defer（管"哪些工具整体延迟"）之下的第二层：管"留下来的工具描述有多重"。**不是**压缩器（不动历史消息体），**不是** defer 列表的替代（与 `tools.tool_search.defer` 正交共存），**不是**提示词重写器（guidance 散文一字不动）。

## 实测省多少（observe.log 真实账目，非估计值）

作者本机固定面（30-64 个 eager 工具 + 163 条技能索引，跨 qwen / deepseek / kimi / gpt 七个模型实测）：

| 指标 | 值 |
|---|---|
| 单请求回收 | 中位 26.6K chars，区间 3.5K–40K（随会话工具面与技能数浮动） |
| 终审两形态 | oneshot 32,876 chars/请求，桌面会话 33,600 chars/请求（≈8.2–8.4K token） |
| 占原固定面比例 | 约一半以上（固定面 16K token 级时） |
| 相对整包载荷 | ≈5.4%（载荷大头是历史消息，本插件不碰） |

冷召回 A/B（禁止模型调 `skills_list`，两批发 7 问）全对，含负例判定，无一漏——这是"折叠描述不伤按需加载能力"的实测依据。

注意：省量与**你的**工具面/技能库规模成正比。装完先开 `observe` 跑几天，看 `$HERMES_HOME/paperhermes/observe.log` 里有没有 `saved_chars` 字段再决定开折叠。

## 安装与启用

放进 `$HERMES_HOME/plugins/`（或一级分类子目录），`plugins.enabled` 加入 `paperhermes`（子目录放置写 `custom-plugins/paperhermes`），重启。

enable 只是让插件在场；**所有修改类开关默认 false**，全关时插件除播种外等效不存在。功能开关在 `Settings → Plugins → paperhermes`（即 `plugins.entries.paperhermes.settings`）。

## 开关一览（每个选项：改什么 / 实测省多少 / 会引发什么 / 怎么回滚）

| 键 | 默认 | 改什么 | 会引发什么 | 回滚 |
|---|---|---|---|---|
| `observe` | **true** | 只记日志（pre/post 字节与折叠量进 `observe.log`），不改载荷 | 最大的问题只是 .log 变大；日志目录可随手删 | 置 false 连钩都不挂 |
| `fold_tool_desc` | false | M1 工具描述折叠。全量描述仍在注册表，`tool_describe`/文档面不受影响 | 依赖工具描述长文做决策的弱模型可能选错工具（A/B 未见；弱模型用户自担测）；`clarify` 与 `keep_eager` 点名的工具永不折叠 | 置 false，下一会话生效 |
| `fold_skills_index` | false | M2 技能索引低频类目降名字行，名字全在场 | 模型不主动 `skills_list` 时看不到类目描述（注入的告知行会提示它）；技能名保留是官方"降档不隐身"立场内的唯一形态 | 置 false |
| `fold_skills_dynamic` | false | M2 增强：本会话被 `skill_view` 用过的技能，下一会话保持全量描述（依赖 `fold_skills_index=true`，否则本键无效并 WARN 一次——WARN 不改配置） | 动态集跨会话持久（`session_hints.json`，30 天 TTL），极端情况是把该升档的技能也折了——同名技能再 view 一次即回补 | 置 false |
| `keep_eager` | `[]` | 点名永不折叠的工具名 | 省量变小 | 清列表 |
| `fold_first_sentence_max_chars` | 120 | 首句 ≤ 该长度则原样保留，超长截断加省略号 | 调大省得少、调小信息少 | 回 120 |
| `min_savings_chars` | 2048 | 净益门：预估回收减去告知行字节后低于此值 → 整轮不介入 | 载荷本来就小的会话不折腾（会话×模型首测定型） | 回 2048 |

## 行为契约（用前必读）

- **会话×模型冻结**：折叠形态在每个 (会话×模型) 组合的首个请求定型，冻结到会话结束。中途改开关**不影响进行中的会话**——这是刻意的缓存铁律：Anthropic/OpenAI 的前缀缓存骑在 system 与 tools 字节上，折叠形态逐请求漂移 = 缓存全毁，倒亏比省的多。新会话吃新配置。
- **例外——插件热重载**：`hermes plugins disable/enable` 会重新 import 插件、清空冻结器，已冻结的活跃会话在下次请求按当前 settings 重定型。
- **播种（seed-once）**：首次会话若读不到 settings 子树，插件经官方 writer 一次性写入全套默认值，让面板有真实值可显示。此后永不写配置。整段删空 settings 后重启会再播种一次（等价重置）；**删单个键 = 该键永久缺失，不补种**。
- **熔断与 fail-open**：折叠逻辑抛任何形状意外 → 该请求原样发送 + WARN；连续 5 次 → 本会话自动关折叠（不回写你的开关值）。永不因折叠失败让请求失败。
- **模型侧告知行**：折叠实际介入时，向 system 注入一句固定告知（中/英按模型名关键词选择），与折叠同生命周期——开关全关则零注入。
- 自检指纹：介入时 `agent.log` 每个新 (会话×模型) 打一行 `[PAPERHERMES] folded N tools, saved ~K chars`。缺这行 = 插件没吃到新代码或已降级。
- 兼容面：折叠只对 `chat_completions` / `anthropic_messages` 两种 api_mode 生效，其余模式原样透传。观测记账对所有模式可用。

## 盘外落点（全部可删）

```
$HERMES_HOME/paperhermes/observe.log        # M0 观测账（JSONL）
$HERMES_HOME/paperhermes/session_hints.json # M2 动态集（30 天 TTL）
```

## `/paperhermes` 命令

状态面板：当前开关读取值、本会话 pre/post 字节、累计回收、动态集。**只读**——不代写 settings，开关变更它原样打印应写入的 YAML 行，由你自己提交。

## 已知局限

- 省量取决于固定面占载荷比例：长历史会话里占比自然摊薄（缓存折扣另计）。
- codex_responses / bedrock 等模式的 system/tools 槽位形态各异，未做适配（旁路原样发送）；本机不可测的形态不实现是设计纪律，不是遗漏。
- `notice_chars` 未单独入账（在 pre−post 净值里混合体现），不影响折叠正确性。

完整设计契约与逐条裁定见同目录 `PROMPT.md`。
