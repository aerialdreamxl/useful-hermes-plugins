# smart-terminal-cwd

按**启动表面**（cli / tui / gateway / desktop）分别设定终端工具的默认工作目录，
并支持按操作系统（win32 / darwin / linux）覆盖。解决的核心问题：
`terminal.cwd` 是 HERMES_HOME 级全局配置，无法区分表面——

| 表面 | Hermes 原生行为 | 本插件注入后 |
|---|---|---|
| CLI | 无视 `terminal.cwd`，永远用启动目录（`cli.py:336`） | 配置 `cli: "."` 时行为不变（启动目录） |
| TUI (`hermes --tui`) | 服从配置文件 `terminal.cwd`（`server.py:596`，优先级在 env 之前） | 钉到 `tui` 配置值（`.`=启动目录 → 获得 Claude Code 体验） |
| Gateway | 显式值直通；占位符解析到家目录 | 钉到 `gateway` 配置值（如 Nest） |
| Desktop | 同 TUI 兜底链，但显式挑选的工作区优先 | 钉到 `desktop` 配置值；**显式工作区绝不覆盖** |

## 配置位置（Hermes 官方的插件配置字段）

`config.yaml` → `plugins.entries.smart-terminal-cwd.settings`：

```yaml
plugins:
  entries:
    smart-terminal-cwd:
      settings:
        default_dirs:            # 跨系统通用值
          cli: "."               # "." / auto / cwd / launch = 进程启动目录
          tui: "."
          gateway: "D:/01-AI/Hermes-Agent/Nest"
          desktop: "D:/01-AI/Hermes-Agent/Nest"
        os:                      # 按当前系统覆盖（sys.platform 取值）
          win32:
            gateway: "D:/01-AI/Hermes-Agent/Nest"
          linux:
            gateway: "~/hermes-nest"
          darwin:
            gateway: "~/Hermes/Nest"
```

值语义：
- `""` / 缺失 / 整段不写 → **不干预**，保持 Hermes 原生行为；
- `"."`（或 `auto` / `cwd` / `launch`）→ 进程启动目录；
- `~` 与环境变量会展开；相对路径按启动目录解析；
- **目录必须真实存在**，否则忽略该值并记日志（宁可不干预也不注入死路径）。

改配置后需**新开会话**生效（钩子注册与会话起点都在 session start）。

## 运行时交互

- **`/terminal-cwd`** — 无参显示当前生效状态；`/terminal-cwd <路径>` 把本会话默认目录钉到
  新位置（`.`=启动目录）；`/terminal-cwd default` 撤销本插件对接管的会话的接管。
- **`terminal_cwd` 工具** — 同上：`{}` 查询、`{"path": "..."}` 切换、`{"action":"reset"}` 撤销。
  只影响"默认目录"（后续新命令没 cd 时的落点），**不影响会话内已有的 `cd` 状态**，也不写配置文件。

## 安全边界（设计红线）

1. 仅在 **local 终端后端**干预（docker/ssh 后端里宿主路径不存在，钉了必错）。
2. 用户在桌面/project 显式挑选的工作区（overrides `cwd_source: "session"`）**绝不覆盖**，
   也不谎报信任标记（该标记同时是 docker per-session 挂载的准入依据）；
   对全局兜底值（`cwd_source: "process"`）只做原位替换，保持原 source。
3. 不创建目录、不改 `terminal.cwd`、不写 `.env`。所有失败路径 fail-open（回退原生行为）。
4. `pre_tool_call` 守卫只对本插件钉过的会话生效（dict 命中检查），未管理会话零开销。

## 安装 / 卸载

```bash
# 安装（复制本目录到插件家 + 启用 + 重启后端）
cp -r smart-terminal-cwd "$HERMES_HOME/plugins/"
hermes plugins doctor smart-terminal-cwd      # 应为 OK、0 WARN
hermes plugins enable smart-terminal-cwd      # 覆盖工具询问选 no（本插件不覆盖任何内建工具）
# 新开会话生效（无需重启 gateway 亦可，重启后对所有表面生效）

# 卸载
hermes plugins disable smart-terminal-cwd && rm -rf "$HERMES_HOME/plugins/smart-terminal-cwd"
```

本机（HERMES_HOME=`D:\01-AI\Hermes-Agent\Data`）：把本目录复制到
`D:\01-AI\Hermes-Agent\Data\plugins\smart-terminal-cwd`。

## 机制注记（v0.21.3 取证）

注入点只有两个公开载体：`record_session_cwd(key, cwd)` 与 overrides 的原位 `cwd` 替换。
terminal 工具消费端有两把键（`_plan_execution` 用 `task_id`、`_resolve_command_cwd`
用审批 contextvar 的 session_key），`on_session_start` 在轮内触发时两把键同源，
插件对两者都钉；`execute_code` 子内核 ladder 第一站读的也是会话记录，随之对齐。
桌面/TUI 在 agent 重建路径会用 `_register_session_cwd` 重写 overrides 冲掉钉值，
`pre_tool_call` 守卫负责廉价重断言（见 `__init__.py` docstring 的行号地图）。

### 系统提示词 cwd 漂移修正（v1.1.0，pre_llm_call）

系统提示词的 `Current working directory` 来自 `agent/runtime_cwd.resolve_agent_cwd()`
（会话 contextvar `_SESSION_CWD` → 会话作用域 `TERMINAL_CWD` → `os.getcwd()`），
与终端工具执行时的 overrides 注册表是**两条独立通道**：桌面网关只在 agent 构建时
绑一次 contextvar，本插件钉的 overrides 值不会回灌，于是出现"提示词说家目录、
命令实际跑在钉值目录"。提示词按字节稳定设计（prefix 缓存契约，见
`agent/surface_switch.py`），不能逐轮改写——所以用同款哲学：`pre_llm_call` 检测
漂移后注入一次性便签到 user message（`{"context": ...}` 通道，
`agent/turn_context.py:_collect_pre_llm_call_context`），告知模型以钉值为准。
去重签名 `(钉值, 轮次计数, 历史长度)`：钉值变/历史被压缩卷掉 → 立即重发，
否则每 10 轮补发一次。核心解析器与本钩子同线程同 context 调用，读数零猜测。

## 测试

`tests/test_drift_note.py`（同 venv 直接跑）：8 项断言覆盖漂移检测、节奏去重、
压缩重发、钉值变更重发、路径分隔符等价、未接管会话静默。
旧 `../stc_test.py`（表面判据/双键钉住/守卫重断言等 15 项）已失散，如找回请放
`tests/` 下。
