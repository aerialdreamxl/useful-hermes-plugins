# gateway-dashboard

单插件一体：dashboard 懒起胶水 + 活动页宿主。需求书：`D:\02-DIY\00日期分类\20261113\gateway-dashboard\PROMPT.md`（v3）。

## 组成（全在 `Data\plugins\gateway-dashboard\`）

| 文件 | 加载方 | 职责 |
|---|---|---|
| `__init__.py` register(ctx) | Gateway 进程（插件系统） | 订阅 `pre_gateway_dispatch`/`on_session_start`，首个事件踢起 dashboard 子进程（console python.exe + `windows_detach_flags()` 隐藏控制台、注入 `_CHILD` 标记）→端口探测握手→把 `dashboard_url` 写进**自己的** `cache/gateway-dashboard/dashboard_state.json` |
| `plugin.yaml` | Settings 面板 | config_schema 六键（§2A 全参数映射） |
| `dashboard/manifest.json + plugin_api.py` | dashboard 进程 | `/api/plugins/gateway-dashboard/*` 只读 API（现为 spike3 `/ping`） |

## 两个架构决定（源码实查后定）

1. **懒起而非开机起**：`gateway:startup` 不在插件 `VALID_HOOKS`（plugins.py:111），属 hooks
   目录机制；为守"一个插件"约束改用消息域 hook。语义变化：dashboard 在**首条 gateway 消息/
   首个会话**时才起，没人用时不白养进程。CLI/TUI 会话不触发（只跑 agent 域）。
2. **递归防线 = 环境变量标记**：dashboard 子进程也会惰性加载 user 插件（`_plugin_host()` →
   `discover_and_load()` 实查），spawn 注入 `HERMES_GATEWAY_DASHBOARD_CHILD=1`，register
   见标即 return 不装 hook。argv 判定被否决（venv_sync 重生链换解释器重拼 argv）。

## 开关与选项（`plugins.entries.gateway-dashboard.settings`）

生效前提：`plugins.enabled` 含 `gateway-dashboard`。**未列入 = 一切零动作**。

| 键 | 默认 | 改了什么 | 会引发什么 |
|---|---|---|---|
| `dashboard_port` | 9120 | 子进程监听口（`--port`） | 撞口时 CLI 自身拒绝（exit 78，不影响 Gateway）；改口需重启 Gateway（once-flag 在进程内存） |
| `host` | 127.0.0.1 | 绑定地址（`--host`） | **非 loopback → 拒绝启动 + log**（安全硬闸） |
| `skip_build` | true | 跳过前端构建（`--skip-build`） | false=子进程尝试 pnpm 构建，弹窗/挂死风险，仅限已配好构建链时手动开；true 但 web_dist 缺失→拒启+提示先构建 |
| `isolated` | true | 独立实例（`--isolated`） | false 时 multiplex 可能 attach 后 exit 0——端口没人动、握手超时的诡异态来源；保持 true |
| `open_profile` | "" | SPA 预选 profile | 首版未实测，留空 |
| `auto_restart` | false | 子进程死后补踢 | true：就绪 60s 后探测，两轮不可用补踢**一次**（防振荡）；false：一次即止 |

## 已知局限（诚实账）

- **懒起时延**：首条 gateway 消息到 dashboard 就绪（实测 ≤8s 起口）之间，面板还没数据。
- **弹窗教训（2026-11-13 修复）**：pythonw 起子进程是反模式——GUI 进程无控制台，
  venv_sync 重生链的每个 console 后代自配可见窗口（cmd 闪窗）。正解 = console
  python.exe + 官方 `windows_detach_flags()`（顶层拥有隐藏控制台，后代继承，零闪烁）。
- Gateway 被强杀时子进程可能孤儿；下次 kick 探测复用自然收敛。手工清理 `hermes dashboard --stop`
  注意它停**全部** Hermes web 进程，慎用。
- CLI venv 同步重生（pythonw→python.exe 孙进程）是正常路径，握手按端口不按 PID。
- **自己的状态文件**：`<HERMES_HOME>/cache/gateway-dashboard/dashboard_state.json`（由 `_PLUGIN_ID` 推导，
  不再硬编码任何已退役目录名）。这是本插件**唯一**写入点——铁律：同一份总线 state 只允许一个插件写，
  两个插件写同一个 state.json 会互相覆盖对方的 `dashboard_url`/`pet_enabled`。
- `dashboard_state.dashboard_url` 回写：gateway-ui 未装时静默跳过。

## 验证状态

- spike1'/2 ✅（9120 全链路 + 零弹窗 + 重生链，2026-11-13）
- 静态自检 ✅：register 递归防线双路径、命令拼装、host/port 负样本拒启、plugin_api import
- spike3（/ping 实弹）⏳：需 `plugins.enabled` 列入 + Gateway 重启（信任闸）
