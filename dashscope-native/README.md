# dashscope-native v0.5 — 百炼（DashScope）全接入面插件

一个插件承担百炼全部接入面（chat 原生线 / 语音 / 文生图 / 增强搜索），合并自
`t2i-bailian` 与 `web-dashscope`。需求书见 [PROMPT.md](PROMPT.md)（权威条款），
本 README 讲每个设置键改什么。零核心补丁。

| 面 | provider 名 | 计费 | 接缝 |
|---|---|---|---|
| chat 原生线 | api_mode + profile 均为 `dashscope`（`dashscope://<region>[@<workspace>]` URL） | 按量 | `register_transport` + `ProviderProfile.create_client()`（官方 dashscope SDK Client 本体） |
| 语音 TTS/ASR 按量 | `dashscope` | 按量 | 官方 SDK `client.audio.*` |
| 语音 TTS/ASR Token Plan | `dashscope-tp` | Token Plan | 官方 best-practices REST（httpx） |
| 文生图 按量 / TP | `dashscope` / `dashscope-tp` | 各自 | 标准库 urllib 直发 JSON |
| 增强搜索/抓取 | `dashscope` | 按量 | 百炼 EnhancedSearch/WebFetch MCP（标准库） |

Token Plan × 原生 chat 上游未文档化，不做（O1）。web 无 TP 变体（TP 侧无文档化 MCP 端点）。

## 设置树（plugins.entries.custom-plugins/dashscope-native.settings）

### 顶层共享键（四面共用，不再每面配凭证）

| 键 | 默认 | 改什么 |
|---|---|---|
| `base_url` | `dashscope://cn-beijing` | 按量侧总端点。chat 直接用此 URI；t2i/web/语音的 REST host 从 region 派生（cn-beijing→dashscope.aliyuncs.com，带 @workspace→工作区网关，其他 region→intl 网关）。也可直填 `https://…` 透传 |
| `key_env` | `DASHSCOPE_API_KEY` | 按量 Key 的**环境变量名**（值只在 .env，永不进 config）。四个按量面全用它 |
| `tp_base_url` | `https://token-plan.cn-beijing.maas.aliyuncs.com` | Token Plan 侧总端点（TP 语音、TP 文生图共用） |
| `tp_key_env` | `DASHSCOPE_TOKEN_PLAN_API_KEY` | TP Key（sk-sp-）的环境变量名 |

### 功能块开关（enabled=按量变体，enabled_tp=TP 变体，默认全 true）

| 键 | 关掉的效果 |
|---|---|
| `model_api_mode.enabled` | chat 原生线不注册（api_mode/profile 都不出现），其余四面不受影响 |
| `voice.tts.enabled` / `voice.stt.enabled` | 按量语音 TTS/STT 不注册 |
| `voice.tts.enabled_tp` / `voice.stt.enabled_tp` | TP 语音 TTS/STT 不注册 |
| `t2i.enabled` / `t2i.enabled_tp` | 对应文生图变体不注册 |
| `web.enabled` | 增强搜索/抓取不注册 |

### 语音参数（tp_ 前缀=分车道覆盖）

裸名键=按量车道；`tp_` 同名键=TP 车道。**TP 取值 tp_X → 缺省回落 X**；
`tp_*` 键缺失就是回落，自动补齐不写占位值。

| 键 | 默认 | 说明 |
|---|---|---|
| `voice.tts.model` | `qwen3-tts-flash` | 按量 TTS 模型（qwen 系走 http 族，其余走 WS 族） |
| `voice.tts.voice` | `Cherry` | 按量音色 |
| `voice.tts.sample_rate` | `24000` | 按量 PCM 流采样率（官方流式样例值） |
| `voice.tts.stream_pcm` | `true` | 按量 qwen 系 PCM 流式 opt-in |
| `voice.tts.tp_model` | `qwen-audio-3.0-tts-plus` | TP TTS 模型 |
| `voice.tts.tp_voice` | `longanxiaoxin` | TP 音色（不做白名单硬校验，上游 InvalidParameter 原样响亮透传；官方 500+ 音色不内嵌） |
| `voice.tts.tp_sample_rate` | `22050` | TP 采样率（docs 默认） |
| `voice.stt.model` | `qwen3-asr-flash` | 按量 ASR 模型 |
| `voice.stt.tp_model` | `qwen-audio-3.0-asr-flash` | TP ASR 模型（非实时，≤5min） |

### 文生图参数

| 键 | 默认 | 说明 |
|---|---|---|
| `t2i.model` | `wan2.7-image` | 缺省模型（两变体共用；调用时 model 参数可覆盖） |
| `t2i.models_ttl_s` | `3600` | 实时模型列表缓存秒数。列表从各变体 `{base}/compatible-mode/v1/models` 拉取、过滤图像族（video 排除）；端点只给 id，display 本地美化、strengths/price 留空不编造；拉取失败回落上次成功值→内置快照，绝不炸注册 |

## 凭证与安全

- 两个环境变量走天下：`DASHSCOPE_API_KEY`（按量）+ `DASHSCOPE_TOKEN_PLAN_API_KEY`（TP）。
  旧 `DASHSCOPE_T2I_API_KEY`/`DASHSCOPE_T2I_BASE_URL`/`TOKEN_PLAN_API_KEY` 已作废。
- F20：TP 车道不隐式复用 `ANTHROPIC_AUTH_TOKEN`；须 `DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN=1`
  显式开启（或把 `tp_key_env` 自己指过去），日志与 STT 返回体 `auth_source` 只写变量名不写值。
- 全部 HTTP 走 http 库直发（urllib/httpx），Key 只进 Authorization 头；
  curl/subprocess/shell 全面禁用（t2i v1.1 的撇号注入教训，验收 V9 有 token 级审计）。
- 错误体净化（F21）：上游 4xx/5xx 响应体剥 ANSI、打断模板标记、800 字符截断，
  前缀 `[上游错误·不可信]`；web 搜索结果/正文带 F19 外部内容围栏头。

## 迁移注意（config.yaml 由主人自己改）

1. `plugins.enabled` 删 `custom-plugins/t2i-bailian`、`custom-plugins/web-dashscope` 两行。
2. `image_gen.provider: t2i-bailian` → `dashscope`（或 `dashscope-tp`）。
3. 本插件旧 settings 树（voice.base/voice.api_key_env/voice.token_plan.* 等）已作废，
   可整段删除让逐键自动补齐落新树。
4. chat 名占 `dashscope` 会覆盖 bundled alibaba 的同名 alias：今后
   `model.provider: dashscope` 解析到**原生线**（原 OpenAI-compatible）。这是命名统一
   裁定的直接后果（PROMPT.md §6.4），本机 `custom:dashscope-token-plan` 路由不受影响。

## 测试

`tests/test_v05.py`：73 项验收（真实核心 ABC isinstance 闸、真实 transport/provider
注册链、开关矩阵、tp_ 分车道、逐键补齐、SDK 缺失不连坐、通道审计、错误包络契约），
含 live 探针（两变体模型目录实拉 + 真实 MCP 搜索/抓取）。跑法：

```
<Hermes venv python> tests/test_v05.py     # DSN_SKIP_LIVE=1 可跳过 live 段
```
