# Hermes Agent自制小插件
**声明：此仓库中有相当一部分代码是由语言模型在 Hermes Agent 框架内编写完成的，如您介意请不要使用（参与名单见文末）**

**本插件仓库中内容使用GPL-3.0协议开源，Hermes Agent 本体为MIT协议，作者承诺不会要求Hermes Agent及其衍生项目使用GPL-3.0或其他协议**

这里存放了我和我的 Agent 制作的插件，用于在部分场景下显著提升 Hermes Agent 的使用体验。

## 如何安装？
将本仓库 git clone 到你的 Hermes Agent 配置目录下的 `plugins` 文件夹里。

放进 `plugins/custom-plugins/` 这种一层子目录也可以：Hermes 插件扫描器支持一级分类目录递归发现。

然后编辑 `config.yaml`，在 `plugins.enabled` 列表里加入你想启用的插件（插件是 opt-in 的，没写进去的不会被加载）。平铺放置时写目录名，套了子目录就写 `custom-plugins/目录名`。

改完配置请**重启 Hermes**（含桌面端要整个应用退出重开），因为插件在进程启动时导入。

个别插件还需要额外配置（API Key、价目表、指定后端），都在各自 README 的《安装与启用》一节里写清了。

## 支持情况？
在 Hermes Agent 可以运行的前提下，这些插件应该都能用。

Windows 与 Linux 实测过；macOS 理论上没有平台相关问题，但没人试过，遇到问题欢迎提 issue。

## 后端类
- **[web-bingsearch](web-bingsearch/README.md)**: 一次将Microsoft Bing搜索的RSS Feed转化为Hermes Agent搜索后端的尝试，不过有些失败。零Key零成本，但 Bing 的反爬蜜罐只能治标，建议当兜底而不是主力。
- **[web-dashscope](web-dashscope/README.md)**: 将阿里云百炼平台提供的增强网页搜索工具和网页爬取工具MCP直接连接到Hermes Agent搜索后端，推荐使用。纯标准库手搓 MCP 协议，无需安装 mcp SDK；抓网页直接回 Markdown 正文而不是原始 HTML，省 token。
- **[t2i-bailian](t2i-bailian/README.md)**: 将阿里云百炼平台上面提供的文生图模型以文生图后端的形式接入Hermes Agent，亲测Token Plan可用。

## 改善类
- **[smart-terminal-cwd](smart-terminal-cwd/README.md)**: 可按系统、按会话类型配置终端默认目录的插件，在一定程度上改善 Hermes Agent 默认目录设置不清晰的问题。
- **[session-hold](session-hold/README.md)**: 使用/hold和/unhold轻松暂停或继续会话，对Agent无感。模型全程不知道中间停过，进程被杀后重启也能靠快照回放无缝续跑。
- **[better-pricing](better-pricing/README.md)**: 配置简单的报价表插件，给自定义模型注入价格数据，供用量统计使用。专治百炼 / Token Plan 用户在 `hermes insights` 和桌面端看到一排 `$0.00 unknown` 的问题，支持多币种与按次计费体系。
- **[hermes-provider-advanced](hermes-provider-advanced/README.md)**: 自定义提供商高级设置，零核心补丁实现两件事：`advanced.proxy` 单提供商代理分流；`api_mode: advanced_oauth` 把 Codex / Claude / Antigravity 订阅账号接成模型通道（导入或交互式登录，自动带单刷轮换安全刷新），请求身份可用 `hermes|cpa|sub2api` 指纹档。OAuth 半边可用 `ADVANCED_DISABLE_OAUTH=1` 整体关闭。注意：订阅通道与指纹功能可能受各提供商服务条款限制，使用前自行评估。需要把插件内 `model-providers/hermes-advanced-custom/` 复制到 `$HERMES_HOME/plugins/model-providers/` 才完整生效，步骤见其 README。

## 工具类
- **[agent-trace-viewer](agent-trace-viewer/README.md)**: DeepSeek Harness风格的Agent Trace查看器。只读浏览 state.db 里的历史会话轨迹，一键导出标准 OpenAI `messages[]` JSON，不向模型注册任何工具，对 agent 行为零影响。
- **[hermes-dsh-simulator](hermes-dsh-simulator/README.md)**: DeepSeek Harness极简模式模拟器，适用于缓解模型对DeepSeek Harness的过拟合。
- **[bili-tools](bili-tools/README.md)**: 哔哩哔哩工具集。6 个只读工具（搜索/视频信息/评论/弹幕/字幕/视频下载），纯标准库 + 匿名公开接口，零 Key、零 Cookie 开箱即用；可用 `implementation=curl|bilibili_api` 开关按需切换高画质下载与 AI 字幕。评论/弹幕发布与登录等互动工具默认完全不注册，需 `HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1` 环境变量显式解锁。

## 关于未在此处列出的插件包
一般情况下，如果你发现仓库里有未在此处列出的插件包，那意味着作者目前还无法保证其可以正常使用或目前还未经大规模验证。

除非你明确知道你在干什么（或是你知道你的 Agent 在干什么），否则最好不要使用，等这里列出该插件后再考虑启用。

## 如果某插件中的源码无意间侵犯了你或贵司的正当权益
请立刻通过PR/Issue/邮件等一切手段联系我，核实相关情况后我会尽快将相关代码去除

## 关于密钥
所有需要 Key 的插件都从环境变量或 `$HERMES_HOME/.env` 读取，源码里没有任何硬编码密钥。各插件用的变量名在其 README 中写明；需要 Key 的只有 web-dashscope（`DASHSCOPE_API_KEY`）与 t2i-bailian（`DASHSCOPE_T2I_API_KEY` / `DASHSCOPE_T2I_BASE_URL`）。hermes-provider-advanced 例外一档：它会把 OAuth 订阅账号的令牌存到 `$HERMES_HOME/.advanced-custom-providers.json`（本地文件，日志里永不出现），不用它就不用登录/导入账号，这个文件也永远不会生成。

## 关于第三方移植代码
hermes-provider-advanced 含有从 CLIProxyAPI（MIT）与 Sub2API（LGPL-3.0）移植/引用的模块，逐文件标注见根目录 `NOTICE-hermes-provider-advanced.md`，上游 MIT 全文保留为 `LICENCE.cpa`。

## 使用过的LLM名单
- Qwen3.8-Flash
- Qwen3.8-Max
- DeepSeek-V4-Flash-0731
- DeepSeek-V4.1-Flash
- DeepSeek-V4.1-Flash-1017