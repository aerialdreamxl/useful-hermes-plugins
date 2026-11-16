# dashscope-native — DashScope 原生线 provider（含语音 lane）

用**官方 dashscope SDK** 走百炼原生协议（`api_mode: qwen_dashscope`），零核心补丁，两条官方接缝：`register_transport("qwen_dashscope", …)` 与 `ProviderProfile.create_client()`。

| 面 | provider 名 | 计费 | 说明 |
|---|---|---|---|
| chat | `dashscope-native`（`dashscope://<region>[@<workspace>]` URL） | 按量（metered Key） | Token Plan × 原生 chat 上游未文档化，不在范围内 |
| 语音 TTS/ASR | `dashscope` | 按量 | 官方语音 REST |
| Token Plan 语音 | `dashscope-tp` | Token Plan | 按官方 best-practices 文档实现 |

## 凭证解析（两条 lane 互相独立）

- **语音（按量）**：只认 `voice.api_key_env`（默认 `DASHSCOPE_API_KEY`）。
- **Token Plan 语音**：顺序 = `voice.token_plan.api_key_env`（默认 `TOKEN_PLAN_API_KEY`，`sk-sp-` 前缀）→ 显式开关下的 `ANTHROPIC_AUTH_TOKEN`。
  - **不再隐式复用 Anthropic token**：想复用必须 `DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN=1`（接受 `true/yes/on`），或自己把 `voice.token_plan.api_key_env` 指到 `ANTHROPIC_AUTH_TOKEN`。开启后日志与 STT 返回体的 `auth_source` 会写明来源，且只写变量名不写值。以前环境里随手有一个 `ANTHROPIC_AUTH_TOKEN` 就会被静默当成百炼语音凭证。
  - `is_available()` 用同一份解析结果——没配真凭证时该 provider 在 picker 里就不显示为可用，而不是等你点了才报错。

## 错误体处理

上游 4xx/5xx 的响应体会被**净化为不可信文本**再进上下文：剥 ANSI、剔控制字符、把 `<|`、`{{`、`[[` 之类模板标记打断、超 800 字符截断加 `…(已截断)`，前缀固定为 `[上游错误·不可信]`。状态码与异常类型保留（`status_code` 等属性不变，只改消息文本）。目的：错误体是上游可控内容，原样进上下文就是一条注入通道。

## 已知局限

- 语音 lane 因上述严格化，以前靠环境里现成 `ANTHROPIC_AUTH_TOKEN` 跑通的部署会明确报错——按上面二选一显式开启即可，推荐正路仍是配 `TOKEN_PLAN_API_KEY`。
- 错误体截断到 800 字符，超长响应里的上游 trace id 可能被截掉。
- `ds_client` 的净化函数被语音模块以函数内相对导入引用，与包内其它模块同机制；宿主若以非包方式加载单文件会 import 失败。
