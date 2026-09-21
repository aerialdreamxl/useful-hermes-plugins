# Hermes Agent自制小插件
**声明：此仓库中有很大一部分代码是由Qwen3.8-Flash或DeepSeek-V4-Flash-0731语言模型驱动Hermes Agent框架完成编写，如您介意请不要使用**
**本插件仓库中内容使用GPL-3.0协议开源，Hermes Agent 本体为MIT协议，作者承诺不会要求Hermes Agent及其衍生项目使用GPL-3.0或其他协议**

这里存放了我和我的Agent制作的可以显著提升Hermes Agent在部分场景下的使用体验的插件

## 如何安装？
将其git clone到你的Hermes Agent配置文件目录的plugins文件夹里

放进 `plugins/custom-plugins/` 这种一层子目录也可以：Hermes 插件扫描器支持一级分类目录递归发现。

然后编辑 `config.yaml`，在 `plugins.enabled` 列表里加入你想启用的插件（插件是 opt-in 的，不列进来自不会被加载）。平铺放置时写目录名，套了子目录就写 `custom-plugins/目录名`。

改完配置请**重启 Hermes**（含桌面端要整个应用退出重开），因为插件在进程启动时导入。

个别插件还需要额外配置（API Key、价目表、指定后端），都在各自 README 的《安装与启用》一节里写清了。

## 支持情况？
在Hermes Agent可以运行的前提下，这些插件应该都能用

Windows 与 Linux 实测过；macOS 理论上没有平台相关问题，但没人试过，遇到问题欢迎提 issue。

## 后段类
- **[web-bingsearch](web-bingsearch/README.md)**: 一次将Microsoft Bing搜索的RSS Feed转化为Hermes Agent搜索后端的尝试，不过有些失败。零Key零成本，但 Bing 的反爬蜜罐只能治标，建议当兜底而不是主力。
- **[web-dashscope](web-dashscope/README.md)**: 将阿里云百炼平台提供的增强网页搜索工具和网页爬取工具MCP直接连接到Hermes Agent搜索后端，推荐使用。纯标准库手搓 MCP 协议，无需安装 mcp SDK；抓网页直接回 Markdown 正文而不是生 HTML，省 token。
- **[t2i-bailian](t2i-bailian/README.md)**: 将阿里云百炼平台上面提供的文生图模型以文生图后端的形式接入Hermes Agent，亲测Token Plan可用。

## 改善类
- **[smart-terminal-cwd](smart-terminal-cwd/README.md)**: 可配置的分系统，分会话类型的增强终端默认目录的插件，在一定程度上可以改善Hermes Agent默认目录设置不清晰的问题。
- **[session-hold](session-hold/README.md)**: 使用/hold和/unhold轻松暂停或继续会话，对Agent无感。模型全程不知道中间停过，进程被杀后重启也能靠快照回放无缝续跑。
- **[better-pricing](better-pricing/README.md)**: 一款配置较为简单的自定义模型报价表，可以用来给自定义模型注入报价表供你统计。专治百炼 / Token Plan 用户在 `hermes insights` 和桌面端看到一排 `$0.00 unknown` 的问题，支持多币种与按次计费体系。

## 工具类
- **[agent-trace-viewer](agent-trace-viewer/README.md)**: DeepSeek Harness风格的Agent Trace查看器。只读浏览 state.db 里的历史会话轨迹，一键导出标准 OpenAI `messages[]` JSON，不向模型注册任何工具，对 agent 行为零影响。

## 关于密钥
所有需要 Key 的插件都从环境变量或 `$HERMES_HOME/.env` 读取，源码里没有任何硬编码密钥。各插件用的变量名在其 README 中写明；需要 Key 的只有 web-dashscope（`DASHSCOPE_API_KEY`）与 t2i-bailian（`DASHSCOPE_T2I_API_KEY` / `DASHSCOPE_T2I_BASE_URL`）。
