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

**两个安装例外要知道**：`hermes-minimem` 是记忆 provider，provider 发现器**不认一级子目录**（只扫 `$HERMES_HOME/plugins/` 平铺一级），必须平铺放置或建同名链接，装法见其 README 文首；`hermes-provider-advanced` 的 model-provider 桥需要出现在 `$HERMES_HOME/plugins/model-providers/` 下（插件在 CLI/agent 启动时自动生成，也可手动复制），步骤见其 README。

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
- **[hermes-provider-advanced](hermes-provider-advanced/README.md)**: 自定义提供商高级设置，零核心补丁实现两件事：`advanced.proxy` 单提供商代理分流；`api_mode: advanced_oauth` 把 Codex / Claude / Antigravity 订阅账号接成模型通道（导入或交互式登录，自动带单刷轮换安全刷新），请求身份可用 `hermes|cpa|sub2api` 指纹档。OAuth 半边可用 `ADVANCED_DISABLE_OAUTH=1` 整体关闭。注意：订阅通道与指纹功能可能受各提供商服务条款限制，使用前自行评估。model-provider 桥需落在 `$HERMES_HOME/plugins/model-providers/hermes-provider-advanced/`（插件自动生成或手动复制均可），步骤见其 README。
- **[paperhermes](paperhermes/README.md)**: 一个高度可配置的省token插件，名字取材自PaperMC。把每次请求重发的工具 description 折成首句、技能索引低频类目降名字行（名字全在场，可按需回读），单请求实测回收 2.6–3.4 万字符；会话×模型字节冻结保前缀缓存，所有修改类开关默认关、观测先行。
- **[browser-switcher](browser-switcher/README.md)**: 可由你或Agent切换的浏览器后端，和ShardX指纹浏览器有联动。default / shardx / cdp 三档收敛到 Chromium CDP，`/browserswitch` 或 `browser_switch` 工具切换，仅当前会话生效，零配置零落盘。
- **[gateway-ui](gateway-ui/README.md)**: 给 Hermes Gateway 配一套 Qt 图形界面——系统托盘状态灯（绿/橙/红/灰实时反映忙闲与降级）+ petdex 桌宠浮窗（透明底、会在窗口边沿爬走、忙时演对应动作、完成/失败/等输入时挥手闪托盘）+ Fluent 设置窗口 + 可插拔余额气泡（左键点桌宠或托盘弹出，DeepSeek/百炼按量/Token Plan 出厂三适配器，峰谷倒计时，查询归宿主、点击零网络）。托盘+桌宠跑在隔离的 side-env worker 进程，崩溃不连坐 Gateway；配置全走官方 writer，修改类开关默认关。仅 Windows 实测（桌宠爬窗用 Win32 枚举），需先 `hermes pets install` 装宠物素材。

## 工具类
- **[agent-trace-viewer](agent-trace-viewer/README.md)**: DeepSeek Harness风格的Agent Trace查看器。只读浏览 state.db 里的历史会话轨迹，一键导出标准 OpenAI `messages[]` JSON，不向模型注册任何工具，对 agent 行为零影响。
- **[hermes-dsh-simulator](hermes-dsh-simulator/README.md)**: DeepSeek Harness极简模式模拟器，适用于缓解模型对DeepSeek Harness的过拟合。
- ~~**[bili-tools](bili-tools/README.md)**: 哔哩哔哩工具集。6 个只读工具（搜索/视频信息/评论/弹幕/字幕/视频下载），纯标准库 + 匿名公开接口，零 Key、零 Cookie 开箱即用；可用 `implementation=curl|bilibili_api` 开关按需切换高画质下载与 AI 字幕。评论/弹幕发布与登录等互动工具默认完全不注册，需 `HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1` 环境变量显式解锁。~~**已废弃，功能并入 aerialdreams-toolsets，目录仅作迁移指路牌保留。**
- **[aerialdreams-toolsets](aerialdreams-toolsets/README.md)**: 一个插件包五个工具套组（32 个工具）：B 站匿名读取 / B 站账号操作（扫码登录、写操作需明确指令）/ ffmpeg+DashScope ASR+Qwen-Omni 多媒体 / 剪贴板+截屏 / adb+fastboot。匿名 B 站路线零 Key 零 Cookie 开箱即用；纯标准库实现（内置 MIT 二维码编码器），截屏走 PowerShell 无第三方依赖（Windows）。
- **[hermes-minimem](hermes-minimem/README.md)**: 极简混合记忆 provider：单 SQLite 文件、int8 稠密向量 + BM25/模型稀疏混合检索、三档 embedding 后端（OpenAI 兼容 / DashScope / 本地 fastembed），选 local 档可完全离线零 API。⚠️ 安装位置有硬规矩，见其 README 文首。
- **[hermes-pack](hermes-pack/README.md)**: `hermes package` 一键把 Hermes 数据家目录打成迁移 zip：黑名单语义自动剔除可再生环境与运行时残渣，你新装的插件/目录默认进包；带热库保护与包内 manifest。

## 关于未在此处列出的插件包
一般情况下，如果你发现仓库里有未在此处列出的插件包，那意味着作者目前还无法保证其可以正常使用或目前还未经大规模验证。

除非你明确知道你在干什么（或是你知道你的 Agent 在干什么），否则最好不要使用，等这里列出该插件后再考虑启用。

## 如果某插件中的源码无意间侵犯了你或贵司的正当权益
请立刻通过PR/Issue/邮件等一切手段联系我，核实相关情况后我会尽快将相关代码去除

## 关于密钥
所有需要 Key 的插件都从环境变量或 `$HERMES_HOME/.env` 读取，源码里没有任何硬编码密钥。各插件用的变量名在其 README 中写明；需要 Key 的有 web-dashscope（`DASHSCOPE_API_KEY`）、t2i-bailian（`DASHSCOPE_T2I_API_KEY` / `DASHSCOPE_T2I_BASE_URL`）、aerialdreams-toolsets 多媒体组（`DASHSCOPE_API_KEY`，变量名可配）、browser-switcher 的 shardx 档（`SHARDX_API_KEY`，不用该档无需配置）。hermes-minimem 的 `embedding_api_key` 明文字段是便利选项，**默认留空走环境变量**——明文会进 config.yaml 并被同机进程可读，是否接受由你决定。hermes-provider-advanced 例外一档：它会把 OAuth 订阅账号的令牌存到 `$HERMES_HOME/.advanced-custom-providers.json`（本地文件，日志里永不出现），不用它就不用登录/导入账号，这个文件也永远不会生成。

## 每个插件都带需求书

各插件目录里的 `PROMPT.md` 是这个插件的需求书（设计契约、逐条裁定与踩坑记录），不是使用说明——想改它，或者想知道"为什么是这样而不是那样"，读那份文件。公开版由私有母版脱敏生成，删了/替了哪些字段，每份文件末尾的《脱敏记录》一节有清单。

## 关于第三方移植代码
- hermes-provider-advanced 含有从 CLIProxyAPI（MIT）与 Sub2API（LGPL-3.0）移植/引用的模块，逐文件标注见根目录 `NOTICE-hermes-provider-advanced.md`，上游 MIT 全文保留为 `LICENCE.cpa`。
- aerialdreams-toolsets 内置 Nayuki qrcode-generator（MIT，版权头原样保留在 `common/qrcodegen.py`）。
- hermes-minimem / browser-switcher 只对接阿里云百炼与 ShardX 的公开 API/协议，不捆绑任何第三方代码。

## 使用过的LLM名单
- Qwen3.8-Flash
- Qwen3.8-Max
- Qwen4-Max-Preview
- DeepSeek-V4-Flash-0731
- DeepSeek-V4.1-Flash
- DeepSeek-V4.1-Flash-1017