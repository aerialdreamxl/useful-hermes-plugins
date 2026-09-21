# agent-trace-viewer — 会话轨迹查看器 / OpenAI 格式导出

给 Hermes 补一个 DeepSeek-Harness 风格的 **agent trace 界面**：只读浏览 `state.db` 里的历史会话（消息流、工具调用、token 数），并支持**一键导出标准 OpenAI `messages[]` JSON**——方便把真实轨迹拿去喂别的模型做对比、写评测集、或者纯粹存档研究。

纯 UI/后端插件：**不向模型注册任何工具、不挂任何钩子**，对 agent 行为零影响。

## 组成

| 部件 | 路径 | 作用 |
|---|---|---|
| Dashboard 后端 | `dashboard/plugin_api.py` | 只读 FastAPI 路由，挂载于 `/api/plugins/agent-trace-viewer/` |
| 桌面端页面 | `desktop/plugin.js` | Hermes Desktop 内的 trace 渲染与导出按钮 |

## 安装与启用

1. 目录放入 `$HERMES_HOME/plugins/agent-trace-viewer/`（可嵌一级分类目录）。
2. `config.yaml` → `plugins.enabled` 加 `agent-trace-viewer`（嵌套时写 `分类/agent-trace-viewer`）。
3. 重启 Hermes Desktop。后端 API 随 dashboard/web server 自动挂载，桌面端出现对应页面。

## API 一览（全部只读，直接对 `state.db` 走 `mode=ro`）

```
GET /api/plugins/agent-trace-viewer/health
GET /api/plugins/agent-trace-viewer/sessions?limit=100&offset=0&q=关键词
    → 会话列表（标题/模型/来源/消息数/工具调用数/时间），q 匹配标题与 id
GET /api/plugins/agent-trace-viewer/sessions/{id}/messages?limit=300&offset=0&order=latest|oldest
    → 消息轨迹分页
GET /api/plugins/agent-trace-viewer/sessions/{id}/export
    → 标准 OpenAI messages[] JSON（可直接作为 chat/completions 请求体）
```

不想要桌面端的话，这几个 HTTP 接口本身就是完整功能，`curl` 即可用。

## 设计边界

- **严格只读**：数据库以只读模式打开，任何写操作都不存在；归档/删除会话请用 Hermes 原生命令。
- `state.db` 不可读时返回 503，不会崩服务。
- 导出内容包含工具调用原文与系统提示词——发布/分享导出文件前注意自查敏感信息。
