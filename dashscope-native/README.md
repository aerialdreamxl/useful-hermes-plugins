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
| `t2i.discover_models` | `true` | 是否随上游刷新模型列表。false=零网络，原样用 `t2i.models` 存量 |
| `t2i.models` | `[]`（首拉后自动填充） | **按量上游全部文生图模型**的共享目录，两变体返回同一份（TP 用户自己挑套餐覆盖的）。刷新经官方 writer 持久化回此键；可手改（纯 id 字符串数组也认）。无内置硬编码兜底 |
| `t2i.models_ttl_s` | `3600` | 刷新间隔秒数。列表从按量 `{base}/compatible-mode/v1/models` 拉取、过滤图像族（video 排除）；拉取失败回落存量，绝不炸注册 |

**没有 `t2i.model` 键**（2026-11-17 裁定）：用哪个模型由系统级 `image_gen.model` 决定
（`hermes setup tools` → Image Generation 里选），工具层每次调用注入；插件不再自持缺省模型。

**定价与简介**（v0.5.2）：目录刷新时对缺定价/简介的条目，从官方文档页
`help.aliyun.com/zh/model-studio/<id点换横线>` 扒取——简介取页面 meta description（截 80 字），
定价只读**第一个地域块（北京）**的"图片生成"行（含 1K/2K、标准/思考档位），国际价绝不混入。
`t2i.discover_models` 开启时，文档 enrich 单轮最多处理 24 条，整体最多约 45 秒；超出预算的条目保持空字段并在下一次刷新再尝试。

**参考图/图像编辑**（v0.5.3，PROMPT.md §6.5）：给 `image_generate` 传 `image_url`
或 `reference_image_urls` 即进入编辑。能力按**当前所选模型**（系统级 `image_gen.model`）
动态声明——wan2.7 系最多 9 张、qwen-image-3.0 系 3 张、qwen-image-2.1 系 10 张、
qwen-image-2.0 系 3 张（均为官方文档实锤，未收录模型按纯文生图保守声明）。
输入形态：公网 URL 直接透传、data URI 直接透传、本地文件读盘转 base64（不存在/超 10MB(wan 20MB)/
非图像一律响亮报错，绝不悄悄丢图或降质）。**编辑画幅**：qwen 系不传 size（输出跟随最后一张
输入图的比例，官方默认行为），wan2.7 沿用文生图同一套像素映射。**多图返回**：上游给几张
返回几张，`image` 字段=第一张（兼容老消费方），完整列表在 `images`/`image_count`。
超时：文生图 120s、编辑 240s；超时不自动重试（同步通道按张计费，重试=多收一份钱），
错误里会写明结果无法找回。**不支持**：z-image-turbo、wan2.6/2.5/2.2/2.1 与老
wanx2.1-imageedit 等模型收到参考图会被点名拒绝并提示换模型；bbox 框选编辑、组图模式、
wan2.7 的异步提交+轮询通道本期未做（异步通道记为 v0.5.4 备选）。

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

## 核心补丁（非插件文件，上游升级会冲掉需重放）

1. **v0.5.1 STT picker 缝**：`hermes_cli/tools_config_providers.py` 补 STT 插件行注入
   （spec/partial/builder 三处对称条目）。原核心 STT picker 无插件注入口，导致
   `hermes setup tools` 里 TTS 可见百炼而 STT 不可见。
   备份：`Data/cache/scratch/tools_config_providers.py.bak-stt-picker`。
2. **v0.5.2 设置落盘防回退**：`hermes_cli/config.py` 的 `save_config` 新增
   `refresh_plugin_settings`（默认 true）——全文档保存前用磁盘上的
   `plugins.entries.*.settings` 刷新调用方快照里的同子树。根因：`hermes setup tools`
   全程持一份 load_config 快照，插件中途经官方 writer 原子落盘的键（如 t2i.models）
   会被流程收尾的 save_config 用旧快照整份写回、静默抹掉。
   `plugins_state.py` 的权威写路径显式传 false 防止自我覆盖。
   备份：`Data/cache/scratch/config.py.bak-leaf-authority`。

## 测试

`tests/test_v05.py` + `tests/edit_mode_block.py`：118 项验收（真实核心 ABC isinstance 闸、真实 transport/provider
注册链、开关矩阵、tp_ 分车道、逐键补齐、SDK 缺失不连坐、通道审计、错误包络契约、
共享目录/discover_models 冻结、§6.5 编辑路径 E1-E10 全冻结面），含 live 探针（全量目录实拉 + 真实 MCP 搜索/抓取）。跑法：

```
<Hermes venv python> tests/test_v05.py     # DSN_SKIP_LIVE=1 可跳过 live 段
```
