# hermes-dsh-simulator

DSH（DeepSeek Harness）模拟器 · **极简模式先行版 v0.4.0**

在一个还没有消息的新会话里执行 `/dshsim minimal`（兼容别名 `simple`），该会话的**模型视角**立即与 DSH 极简模式 1:1 对齐——system prompt 与工具 schema 逐字取自 DSH 源码快照——背后**静默转发**到 Hermes 的 `terminal` / `str_replace_editor` 对应工具。默认状态下插件对会话零介入。

除保真复刻外还有一重现实用途：为**对 DSH 面过拟合的模型**提供其“训练原生面”——如在 Hermes 原生工具面下表现失常的各类开源模型（如 DeepSeek-V3/V4 衍生调优版），切到 minimal 模式即可在 Hermes Agent 内正常作业。

## 用法

| 命令 | 时机 | 效果 |
|---|---|---|
| `/dshsim minimal` | 仅限全新会话（尚无消息） | 接管本会话：覆写 system prompt + 工具面转译为 `bash` / `str_replace_editor`；登记持久化接管清单并给会话名加 `[DSHSIM]` 标 |
| `/dshsim optout` | **任何时候** | 取消接管：恢复 Hermes Agent 默认行为（模式、清单条目、`[DSHSIM]` 标一并移除） |
| `/dshsim` | 任何时候 | 查看当前状态与用法 |

已有消息的会话执行 `minimal` 会被拒绝（“改不了了”）。用户可随时 `optout` 退出接管。

### 持久化接管清单与 `[DSHSIM]` 标（v0.4.0）

- 每接管一个会话，其会话 ID 持久化登记在 `$HERMES_HOME/dshsim-sessions.json`；插件注册时**自动重新武装**清单内的全部会话——桌面后端重启后接管不再丢失（中间件每请求自愈模式与标签）。
- 被接管的会话，其**会话名**自动带 `[DSHSIM] ` 前缀（写入宿主 `state.db` 标题，桌面侧栏即时刷新；标题尚未生成的全新会话等下轮请求补标）。
- `optout` 反向清理：模式、清单条目、`[DSHSIM]` 标全部移除。

不带模式时插件对会话零介入：两枚工具以**不可见方式注册**（私有 toolset + 永假 `check_fn`），不会出现在任何会话的工具定义、`tool_search` 延迟目录、`tool_call` 桥或校验集合中；载荷侧另有一层按名的剥离兜底（四种协议形状都认）。

## 安装与启用

本插件支持直接平铺在插件根目录，也支持放在自定义分类子目录（例如 `custom-plugins`）内：

1. **放置目录**（任选其一）：
   - 平铺路径：`$HERMES_HOME/plugins/hermes-dsh-simulator`
   - 分类路径：`$HERMES_HOME/plugins/custom-plugins/hermes-dsh-simulator`
2. **启用插件**：
   在 Hermes 配置文件 `config.yaml` 的 `plugins.enabled` 列表里加入本插件：
   ```yaml
   plugins:
     enabled:
       - hermes-dsh-simulator   # 若放置在 custom-plugins 下，写 hermes-dsh-simulator 或 custom-plugins/hermes-dsh-simulator 均可
   ```
   或使用命令行启用：
   ```bash
   hermes plugins enable hermes-dsh-simulator
   ```
3. **重启生效**：
   重启 Hermes Agent（桌面端需退出重开）使插件加载生效。

## 模型视角 = DSH 极简（1:1）

- **system prompt**：`You are a helpful software engineer assistant.`（web flavor；整段覆写，系统位置不剩别的）
- **工具面**：恰好两枚，schema 逐字节等于 DSH：
  - `bash` — `{command}`，描述与 DSH 极简一致
  - `str_replace_editor` — DSH 全参数（view/create/str_replace/insert + 7 参数，含 null 占位约定）

### 协议适配（四种 api_mode 全支持）

覆写落在**每种协议自己的 system 位与工具位**上，形状与“该协议下 Hermes 处理自家工具”完全一致：

| api_mode | system 位 | 工具形态 |
|---|---|---|
| `chat_completions` | `messages[0]`（system） | `tools[type=function, function=…]`（OpenAI 原形） |
| `codex_responses` | `instructions` | `tools[type=function, name=…]`（Responses 函数定义，含 `strict`） |
| `anthropic_messages` | `system` | `tools[name, input_schema]`（含宿主对可空联合的归一化） |
| `bedrock_converse` | `system[{text}]` | `toolConfig.tools[toolSpec]`（`cachePoint` 保留） |

### flavor（两档 DSH 极简变体）

- `web`（默认，即“极简模式”agent preset）：prompt `You are a helpful software engineer assistant.`；bash 描述为 RL 环境版（“Network access depends on the task environment…”）
- `sdk`（裸双工具 SDK 运行时）：prompt `You are the environment-selected minimal software engineer.`；bash 描述为无网版（“You don't have access to the internet via this tool…”）
- 切换：设置环境变量 `HERMES_DSHSIM_FLAVOR=web|sdk`；系统提示词另可用 `DSH_SYSTEM_PROMPT` 覆盖（与 DSH 自身的 env 约定一致）。

## 转译层（模型看到的 → Hermes 实际执行）

| DSH 面 | Hermes 底层 | 说明 |
|---|---|---|
| `bash{command}` | `terminal`（`timeout=300`，对齐 DSH `timeoutMs: 300000`） | 结果渲染为 DSH 格式：`输出\n[Command finished with exit code N]`；超时输出 DSH 超时块；工具级错误透传 Hermes 文案 |
| `str_replace_editor view` | `read_file`（守卫/追踪）+ 本地原文重渲染 | 文件：DSH `cat -n` 风格（6 位右对齐行号 + 双空格）；目录：DSH 两层过滤清单（隐藏项/node_modules/__pycache__ 排除、按路径排序） |
| `str_replace_editor create` | `write_file` | 已存在文件按 DSH 规则先拒 |
| `str_replace_editor str_replace` | `patch(replace)` | 本地预检逐字匹配 + 唯一性（行号与 DSH 一致）；通过才转发变更 |
| `str_replace_editor insert` | `read_file`（分页补齐整读基线）→ `write_file` | 满足 Hermes 写守卫的“整读基线”；守卫拒绝时透传其文案 |

**机制本体**有三块，缺一不可：

1. **请求侧替换**：minimal 模式激活时，出站载荷的工具列表被**整体替换**为两枚（上表），模型从第一帧起就看不见两枚之外的工具——不是"看着一堆、拦掉多数"。
2. **会话侧注入**：宿主在**回程**会拿 `agent.valid_tool_names`（来自 agent 自身的工具快照，与出站载荷无关）校验每次调用名，光换载荷赢不了这道闸——把 `bash` / `str_replace_editor` 的 wire 定义**注入活体 agent**（`agent.tools` + `valid_tool_names`，幂等，每轮请求自愈），直呼两枚工具才真的可执行。注册侧的不可见性与可执行性由此解耦：`check_fn` 只关"暴露"、从不拦"调用"。
3. **执行侧兜底**：万一出现两枚之外的调用（模型幻觉等异常情况），`pre_tool_call` 会拒绝执行——正常流程永远触发不到它，非 minimal 会话完全不碰。

## 会话解析与门控

解析梯度：① 桌面派发绑定的会话上下文 → ② approval 上下文钥匙 → ③ CLI `_cli_ref` → ④ 桌面会话表（单运行/单开/最近活跃；≥2 个运行中→拒绝并要求明确目标）。

门控（仅约束 `minimal`）：无历史 / 无恢复前缀 / 非运行中 / 本进程未见该会话的任何请求（turn tracker 由 llm_request 中间件打点）。`optout` 永不受门控。

## 已知差异与限制

1. **OAuth（Claude Code）风格的 Anthropic 会话**：Hermes 会在 system 头部加合规身份块（“You are Claude Code…”），该块保留（传输层要求），DSH 提示词作为第二块；工具名随宿主规则加 `mcp__` 前缀。
2. **缓存标记行为**：Bedrock 的 `cachePoint` 标记在 system / tools 里保留；Anthropic 侧原 system 的 `cache_control` 块标记在覆写时不保留；OpenAI 系（chat / codex）的 `prompt_cache_key` 保持宿主原值。
3. **Moonshot/Kimi 的 chat 会话**：工具 schema 先过宿主的 K 家消毒再发出。
4. **Bedrock 非工具模型**：不加 `toolConfig`，只覆写 system，并告警一次。
5. 接管**已持久化**：登记在 `dshsim-sessions.json` 的会话在插件注册时自动续管。彻底退出请对目标会话执行 `/dshsim optout`（或删除清单文件后重启）。
6. gateway（Feishu 等）面的会话解析目前为实验性质；桌面端 / CLI 为原生支持。
7. `bash` 底层为每次独立命令环境；DSH 文案里的“persistent”措辞系逐字还原。
8. 视图/列表截断默认 40000 字符（可通过环境变量 `HERMES_DSHSIM_MAX_OUTPUT_CHARS` 调整）。
9. “只对新会话生效”是刻意设计：与 DSH“预设随任务开始确定”同构，不污染已有交互上下文。

## 测试

在 Hermes 运行环境（或配置了 Hermes 依赖的 Python 环境）中运行：

```bash
# 运行单元测试（测试各协议 wire 生成、等价断言与转译逻辑）
python tests/test_unit.py

# 运行集成测试
python tests/test_integration.py
```
