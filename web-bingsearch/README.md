# web-bingsearch — Bing RSS 直采搜索后端

把 **Microsoft Bing 的 RSS Feed**（`cn.bing.com/search?format=rss`）直接转化为 Hermes 的原生搜索后端：无 API Key、无第三方聚合、零账号——发请求拿结果，纯白嫖路线。

注册为 web search provider（`ctx.register_web_search_provider`），同时支持 **search** 和 **extract** 两个原生工具面。

> 诚实声明（作者原话）："不过有些失败。"——Bing 对这个免登录入口有反爬蜜罐，本插件内置了缓解阶梯，但**质量不如官方 API 类后端**。适合作为零成本兜底，不适合当主力。主力推荐隔壁 [web-dashscope](../web-dashscope/README.md)。

## 安装与启用

1. 目录放入 `$HERMES_HOME/plugins/web-bingsearch/`（可嵌一级分类目录）。
2. 依赖：`primp`（带 TLS 浏览器指纹伪装的 HTTP 客户端）——`pip install primp`。
3. `config.yaml` → `plugins.enabled` 加一行。这里有三个容易混淆的名字，各司其职：
   - **目录名** `web-bingsearch`：决定 `plugins.enabled` 里的 key（嵌套放置时是 `分类/web-bingsearch`）；
   - **manifest name** `web-bing-search`：`plugin.yaml` 里的 `name:`，gate 同时接受它，所以两种写法都能激活；
   - **provider id** `bing-search`：`__init__.py` 里 `name` property 返回的值，选后端时**只用这个**。
4. `config.yaml` → `web.search_backend: bing-search`（正文提取同理 `web.extract_backend: bing-search`）。
5. 重启 Hermes。

无需任何环境变量或 Key，`is_available()` 只检查 `primp` 可导入。

## 实测特性与坑（都写在代码注释里，这里摊开说）

- **反蜜罐三档阶梯**：实测确诊 Bing RSS 对 **≥4 词的查询**确定性返回反爬垃圾（`<channel>` 正常回显但 `<item>` 是无关填充）。插件按"原句 → 截 3 词 → 截 1 词"逐级降级重试，并用垃圾检测器把关。所以**短查询质量明显好于长句查询**。
- **TLS 指纹轮换**：chrome / edge / safari 三个真实浏览器指纹轮着发，缓解单一指纹被限速。
- **extract 是生肉**：按 URL 抓原始 HTML 直接回 `raw_content`，不做正文解析（作者注释："我懒得解析出来了"）。模型通常能自己啃 HTML，但 token 消耗比正经阅读器后端大。
- 超时保护：搜索 20s、抓取 30s，Bing 抽风不会拖死 Hermes 进程。
- 单个 URL 抓取失败只标记该项 `error`，不整批失败。

## 合法合规提示

这是对公开 RSS 端点的模拟浏览器访问，请自行评估是否符合你的使用场景与 Bing 的服务条款；被限流或封 IP 属预期内风险。
