# browser-switcher — 会话作用域的浏览器后端切换插头

给 Hermes 的浏览器工具面（`browser_exec` / built-in `browser_*`）加一个**可随时切换的后端选择层**：你自己用 `/browserswitch` 命令切，或者让 Agent 用 `browser_switch` 工具切，只影响当前会话，不用改配置、不用重启。

三档后端（全部收敛到 Chromium CDP，刻意不管 Firefox 系 / 扩展控制 / 云浏览器厂商）：

| 档位 | 是什么 | 前置 |
|---|---|---|
| `default` | 不干预，走 Hermes 打包的 Headless Chromium | 无 |
| `shardx` | 本机 ShardX 指纹浏览器，经其本地 HTTP API 启动 profile 拿 CDP 端点 | ShardX Launcher 在跑 + 三个环境变量（见下） |
| `cdp` | 任意 Chromium CDP 端点（`ws://…/devtools/browser/…` 或 `http://host:port`） | 你自己有一个开着 `--remote-debugging-port` 的 Chromium；**主机必须在白名单内**（见下） |

## 安装与启用

放进 `$HERMES_HOME/plugins/`（或 `custom-plugins/` 这类一级分类子目录，通用插件扫描器支持递归发现），然后在 `config.yaml` 的 `plugins.enabled` 加入：

```yaml
plugins:
  enabled:
    - browser-switcher          # 平铺放置
    # - custom-plugins/browser-switcher   # 套了子目录就写带前缀的 id
```

重启 Hermes（桌面端要整个应用退出重开）。

## 用法

```text
/browserswitch default
/browserswitch shardx [profile名]     # 不给名字则用 SHARDX_BROWSER_NAME
/browserswitch cdp ws://127.0.0.1:9222/devtools/browser/<id>
/browserswitch                        # 不带参数 = 查看当前状态
```

Agent 侧对应 `browser_switch` 工具（`provider` + `arg` + `headless` 参数），描述里写明了三档语义，模型会在你要求"换个浏览器/用我开着的窗口"时自行调用。

切换状态是进程内的目标状态，但底层 CDP 环境变量/daemon 仍为进程级；并发多会话时可能 last-writer-wins，不能宣称已解决进程级隔离。

## ShardX 档的配置

全部来自环境（`$HERMES_HOME/.env` 或进程环境），插件不写任何配置文件：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHARDX_BASE_URL` | `http://127.0.0.1:40325` | ShardX 本地 API 地址 |
| `SHARDX_API_KEY` | （必填） | Bearer JWT，从 ShardX 的设置页复制；日志与报错永不回显值 |
| `SHARDX_BROWSER_NAME` | （选填） | 默认 profile 名，命令行省略时使用 |

`cdp` 档与 `default` 档零配置。没有 ShardX 的用户完全不受影响——只是少一档。

`cdp` 档的主机白名单（防模型被网页文本忽悠着把浏览器会话指到外部端点）：只放行 `localhost` / `127.0.0.0/8` / `::1` / `169.254/16` / `fe80::/10` / RFC1918（`10/8`、`172.16/12`、`192.168/16`）/ ULA `fc00::/7`。公网 IP、任何域名（含局域网单标签主机名、Tailscale `100.64/10`）一律拒绝；HTTP discovery 的最终 websocket URL 与 ShardX 返回的 CDP URL 都会再次检查 scheme/host。status/切换返回只显示 scheme、host、port，不显示 websocket path/GUID。

## 机制要点（为什么不用改核心）

Hermes 的浏览器后端解析优先序里，`BROWSER_CDP_URL` 环境变量是仅次于显式 CDP 配置的通道，且 harness daemon 与 agent-browser daemon 两条工具面都逐次读它。插件做的事就是：解析目标端点 → 写入/清掉该 env → 按档位等价重启对应 daemon。上游源码注释实锤两个 daemon 都只在冷启动时快照环境，所以"漂移检测 + 换端点 + 重启 daemon"三步缺一不可，这是插件的主体逻辑。

## 已知局限（如实陈述）

- **gateway 多路复用并发多会话时**，env 通道是进程级的：两个会话同时切不同后端会 last-writer-wins 并互相触发 reload（乒乓）。单会话部署（桌面端典型形态）无此问题。
- built-in `browser_*` 面在 `/browser connect` 等进程外通道也写同一 env 键时，归属本插件管辖——切回 `default` 会把那个连接清掉。
- ShardX 档假设 launcher 2.x（start 响应的 `cdp` 是对象形态；老式字符串形态也兼容）。ShardX 是第三方商业软件，本插件只对接其公开本地 API，不随仓库分发任何东西。

## 测试

```bash
python tests/test_standalone.py
```

离线自测（假端点 + 假响应），不需要 ShardX、不需要真浏览器。

## 卸载

从 `plugins.enabled` 移除并重启即可；插件没有盘外落点（不写日志文件、不建目录）。
