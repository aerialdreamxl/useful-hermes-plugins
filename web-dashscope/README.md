# web-dashscope — 阿里云百炼增强搜索/网页抓取 MCP 直连后端

把**阿里云百炼（DashScope）官方托管的两个 MCP 服务**——`EnhancedSearch`（search_pro）和 `WebFetch`（web_fetch）——直接短路成 Hermes 的原生 `web_search` / `web_extract` 工具后端。

相比隔壁 Bing 白嫖方案，这是**有官方 API、结果质量稳定**的路线；相比在 Hermes 里挂一个完整 MCP client，本插件用**纯标准库**手写 Streamable HTTP 协议，不装 `mcp` SDK 也能跑，启动零开销。作者自用推荐款。

## 前置条件

- 一个百炼 API Key（通用 Key，`sk-` 开头；阿里云百炼控制台开通，新用户有免费额度）。
- 网络可达 `dashscope.aliyuncs.com`。

## 安装与启用

1. 目录放入 `$HERMES_HOME/plugins/web-dashscope/`（可嵌一级分类目录）。
2. Key 放 `$HERMES_HOME/.env`（Hermes 启动自动加载）：
   ```
   DASHSCOPE_API_KEY=sk-xxx…xxxx
   ```
   或当前 shell 里 export 同名变量。
3. `config.yaml` → `plugins.enabled` 加 `web-dashscope`（目录名与 manifest name 一致，provider id 也是 `dashscope`，三者不踩坑）。
4. `config.yaml` → 选择后端：
   ```yaml
   web:
     search_backend: dashscope
     extract_backend: dashscope   # 可选；不配则 extract 走别的后端
   ```
5. 重启 Hermes。

## 两个能力面

| Hermes 工具 | 背后 MCP | 说明 |
|---|---|---|
| `web_search` | EnhancedSearch → `search_pro` | 返回结构化 `{title,url,snippet,hostname}` 列表，已自动剥掉 `<em>` 高亮标签 |
| `web_extract` | WebFetch → `web_fetch` | 服务端直接回**页面 Markdown 正文**（不是生 HTML），质量高、省 token——这是它对 Bing 方案的最大优势 |

## 实现细节（如果你想改它）

- 协议：MCP Streamable HTTP，`initialize → notifications/initialized → tools/call` 三步握手；响应体 SSE（`data:` 行）与裸 JSON 两种都吃。
- **会话缓存**：`mcp-session-id` 进程级复用，避免每次搜索重新握手；服务端隔段时间作废旧 session 时自动重握重试一次。
- 超时：握手 20s、工具调用 60s。
- 无任何第三方依赖，`urllib` 一把梭。

## 已知边界

- 计费跟着你的百炼账号走（按量），搜索结果偶发对超冷门查询空返回——属上游行为。
- Key 权限只用到这两个 MCP 端点；建议按百炼控制台方式给这个 Key 收敛权限。
