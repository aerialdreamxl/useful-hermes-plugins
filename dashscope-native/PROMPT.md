# dashscope-native — 需求书（合并重构版 v0.5 规划）

> 本文件是 dashscope-native 的权威需求书。代码注释引用的 S0 v3 / O1 / §10 / §11 / F19 / F20
> 条款以本文件为准（历史裁定原样继承、标注于各节）。
>
> **v0.5 定案（主人 2026-11-17 给出 exp.yaml 样板，覆盖 v0.3/v0.4 的散键设计）**：
> ①顶层两对共享键——`base_url` + `key_env`（按量变体）与 `tp_base_url` + `tp_key_env`
> （Token Plan 变体），各功能块不再持独立凭证键；
> ②每个功能块内 `enabled` = 启用按量变体、`enabled_tp` = 启用 Token Plan 变体，二者独立开关；
> ③注册名统一：按量面一律 `dashscope`、TP 变体一律 `dashscope-tp`（原名 dashscope-native /
> t2i-bailian / qwen_dashscope / 旧 aliases 全部作废）；chat 面 = api_mode 与 profile 双名 `dashscope`，
> profile 机制不可裁剪（S0 v3 反壳缝物化官方 SDK Client 本体、dashscope:// 前缀匹配均挂 profile）；
> ④环境变量收敛为 `DASHSCOPE_API_KEY` 与 `DASHSCOPE_TOKEN_PLAN_API_KEY` 两个（.env 已在位，
> 后者已实测为 sk-sp- 正主）。
> 样板中的 `t2i.model_list` 主人批注「要从上游模型列表实时获取」——§6.1 落实。

## 1. 定位与范围

一个插件承担百炼（DashScope）全部接入面，合并自 t2i-bailian、web-dashscope（主人拍板
"做成默认开启的功能模块"）。模块 × 变体矩阵（键名以 §2 样板树为准）：

| 功能块 | enabled（按量） | enabled_tp（Token Plan） | 注册名 |
|---|---|---|---|
| `model_api_mode` chat 原生线 | ✅ | ❌ 不适用（O1 继承：TP×native chat 官方未文档化，不做） | api_mode 与 profile 双名 `dashscope` |
| `voice.tts` | ✅ | ✅ | `dashscope` / `dashscope-tp` |
| `voice.stt` | ✅ | ✅ | `dashscope` / `dashscope-tp` |
| `t2i` | ✅ | ✅ | `dashscope` / `dashscope-tp` |
| `web` | ✅ | ❌ 样板未给 enabled_tp；Token Plan 侧无文档化 MCP web 端点，**不做**；将来官方出了再补键 | `dashscope` |

- 注册顺序：chat 接缝 → 语音 → 文生图 → web；各段独立 try 块，任何一段失败只降级该段。
- 两变体同开时两个名字并存于同一 registry（如 image_gen 里 `dashscope` + `dashscope-tp`），
  运行时由 `image_gen.provider` / `tts.provider` 等上游选择键分派，互不干扰。

## 2. 设置树（权威样板 = 主人 exp.yaml）

```yaml
plugins:
  entries:
    custom-plugins/dashscope-native:
      settings:
        base_url: dashscope://cn-beijing               # 按量侧总端点（chat 用 URI；REST 族按 region 派生，见 §5.3）
        key_env: DASHSCOPE_API_KEY                     # 存变量名，值只在 .env
        tp_base_url: https://token-plan.cn-beijing.maas.aliyuncs.com
        tp_key_env: DASHSCOPE_TOKEN_PLAN_API_KEY       # 存变量名
        model_api_mode:
          enabled: true
        web:
          enabled: true
        voice:
          tts:
            enabled: true        # 按量变体
            enabled_tp: true     # TP 变体
            model: qwen3-tts-flash          # 按量车道
            voice: Cherry
            sample_rate: 24000
            tp_model: qwen-audio-3.0-tts-plus  # TP 车道（tp_ 前缀=分车道覆盖，见下）
            tp_voice: longanxiaoxin
            tp_sample_rate: 22050
          stt:
            enabled: true
            enabled_tp: true
            model: qwen3-asr-flash
            tp_model: qwen-audio-3.0-asr-flash
        t2i:
          enabled: true
          enabled_tp: true
          model_list: []         # 运行时从上游实时获取（§6.1），样板结构: id/display/strengths/price
```

- **DIY 插件规范（主人 2026-11-09 定版）继续适用**：缺键「逐键自动补齐」（key not in settings
  才算缺失，显式存的 ""/false/0 不覆盖）；写入走官方 `ctx.set_config`；README 写清每键语义。
- v0.2 的 `voice.tts.enabled` 等语义变化：原来管"启不启用"，v0.5 的 `enabled/enabled_tp`
  管"注册哪个变体"，**默认值按样板全部 true**（主人明示默认开启的功能模块）。
- 旧键（`voice.base`、`voice.api_key_env`、`voice.token_plan.*`、`chat.*_env`、`image.*_env`、
  `web.*_env`）全部作废，不留兼容读；迁移靠逐键自动补齐自然落新树（主人自己动手清旧键）。

## 3. chat 原生线（继承 S0 v3，实现不变项）

- 纯 dashscope API 形态：官方 SDK Client 本体（`create_client()` 反壳缝，主人裁定不得违反）；
  kwargs 转发 + `parameters=` 透传。
- api_mode 与 profile 双名 `dashscope`（原 `qwen_dashscope` / `dashscope-native`；config 全文无
  旧名引用，改名零迁移）。`_resolve_api_mode` 阶梯确认手写 `api_mode: dashscope` 即命中。
- URI 语法（ds_uri.py）不变：`dashscope://<region>[@<workspace-id>]`。
- 顶栏 `base_url` 即 chat 的缺省路由端点（v0.2 时代该值藏在 voice.base / provider 配置里，
  v0.5 提到顶层）。

## 4. 语音面（继承 §10/§10.6/§11/F20）

- 官方 REST 实现、无 live 探测、docs 冲突 best-practices 页胜出——均不变。
- F20（适配到共享键）：TP 变体不隐式复用 `ANTHROPIC_AUTH_TOKEN`，须
  `DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN=1` 才兜底；日志/`auth_source` 只写变量名不写值。
- **分车道参数（v0.5 主人裁定：`tp_` 前缀覆盖键）**：块内裸名键（`model`/`voice`/`sample_rate`）
  = 按量车道参数；`tp_` 前缀同名键（`tp_model`/`tp_voice`/`tp_sample_rate`）= TP 车道参数。
  TP 车道取值优先级 `tp_X` → 缺省回落 `X`（未配 tp_ 键时两车道共享，向后兼容）；逐键自动补齐
  只对裸名键补默认，`tp_*` 键缺失即视为"回落"，不补占位值。
  跨车道模型名不互认的炸点由此消除（如 qwen3-tts-flash 按量专用、qwen-audio-3.0-tts-plus TP 专用）。
- voice 名 `longanxiaoxin` 不在 2026-11-11 内嵌 TP 音色快照（3 项）与按量 48 项快照内：
  官方音色表 500+ 不内嵌（§11 继承）。**实现改为不硬校验音色白名单**，快照仅作参考注释；
  上游 InvalidParameter 即报错透传。

## 5. 凭证与端点模型（v0.5 收敛版）

### 5.1 环境变量终态
| 变量 | 消费者 |
|---|---|
| `DASHSCOPE_API_KEY` | chat、语音按量、t2i 按量、web |
| `DASHSCOPE_TOKEN_PLAN_API_KEY` | 语音 TP、t2i TP |
| `ANTHROPIC_AUTH_TOKEN` | 仅 F20 显式开关下的 TP 兜底 |
作废：`DASHSCOPE_T2I_API_KEY`、`DASHSCOPE_T2I_BASE_URL`、`TOKEN_PLAN_API_KEY`（默认不再引用）。

### 5.2 规则
1. `key_env` / `tp_key_env` 存**变量名**；值永不进 config（minimem 式明文特批不适用本项目）。
2. Key 缺失时报错文案指向对应 `*_key_env` 键名与 .env，绝不回显值。
3. **按量 REST 端点派生**：`base_url: dashscope://<region>` 的 region 经内置表映射 REST host
   （cn-beijing→`https://dashscope.aliyuncs.com`，其余 VALID_REGIONS→官方对应 host）；
   t2i 按量、web MCP（路径 `/api/v1/mcps/{EnhancedSearch,WebFetch}/mcp`）、语音按量 REST
   全部跟随派生值。
4. TP 侧 REST 直接用 `tp_base_url`（t2i TP 走 `{tp_base_url}/api/v1/services/...` 或
   compatible-mode，以官方文档为准；语音 TP 沿用现实现路径）。

### 5.3 实时模型列表（v0.5.1 主人修订：单一共享目录）
- **已实测通道（2026-11-17）**：按量 base 的 `/compatible-mode/v1/models` 返回 id 清单
  （182 项含 wan2.7-image/-pro、qwen-image-3.0/-pro、z-image-turbo 等）。
- **v0.5.1 裁定（覆盖 v0.5 的双变体各拉各的）**：`t2i.models` 设置键存**按量上游的
  全部文生图模型列表**，**两个变体返回同一份**——TP 用户自己挑套餐用得着的。
  刷新永远打按量端点（`key_env`），与变体无关。
- `t2i.discover_models`（默认 true）决定是否随上游刷新：true 时按 `t2i.models_ttl_s`
  过期重拉并经官方 writer 持久化回 `t2i.models`；false 时零网络、原样返回存量列表。
  拉取失败回落存量列表，不得炸注册/picker。**内置三模型快照已删除**（v0.5.1 BUG 修：
  任何变体都不得回退到硬编码三件套）。
- 过滤图像族（id 含 image/wan/z-image，排除 -video）。
- **v0.5.2 定价/简介扒取（主人 2026-11-17 追加）**：models 端点只给 id；对缺
  strengths/price 的条目从官方文档页 `help.aliyun.com/zh/model-studio/<slug>` 扒取
  （slug=id 点换横线，实测 wan2.7-image→wan2-7-image 全目录可达）。简介=meta
  description 截 80 字；定价只读第一个地域块（北京）的「图片生成」行，含 1K/2K 与
  标准/思考档位，国际价严禁混入。带日期快照 id 404 时回退无日期基础页。扒不到留空
  **不编造**；已扒到的条目刷新时保留不重扒。受 discover_models 同一开关管辖。
- **v0.5.2 BUG4 裁定：删除 `t2i.model` 设置键**——模型选择唯一权威=系统级
  `image_gen.model`（setup tools 写入、tools/image_generation_tool.py 每调用注入
  kwargs["model"]）；default_model() 用核心基类语义（目录第一项）。generate 内
  `or "wan2.7-image"` 仅作目录全空时的最后防线。
- **v0.5.2 BUG3 根因在核心**：`hermes setup tools` 全程持一份 load_config 快照，
  插件中途 `ctx.set_config` 原子落盘 t2i.models 后，流程收尾 `save_config(config)`
  用旧快照整份写回把它抹掉（复现实锤：中途盘上 12 条→收尾 0 条）。修在
  `hermes_cli/config.py`：save_config 新增 refresh_plugin_settings=True，全文档
  保存前用磁盘 raw 的 plugins.entries.*.settings 深合并刷新快照同子树；
  plugins_state.py 权威写路径显式传 False。核心补丁备份见 README。
- 存量列表兼容手改：`t2i.models` 里写纯 id 字符串数组也认（自动包成条目）。

## 6. 功能面细则

### 6.1 t2i（并自 t2i-bailian，行为不变项 + TP 变体新增项）
- 不变：画幅映射（landscape→1696*960、portrait→960*1696、其余→1280*1280）、120s 超时。
  ~~text-only 声明、参考图明确拒绝~~ —— v0.5.3 起按 §6.5 放开（编辑能力按所选模型动态声明）。
- **HTTP 通道硬性要求（主人 2026-11-17 重申）**：一切请求走 http 库直发（urllib，标准库），
  Key 只进 Authorization 头。curl/subprocess/shell **全面禁用**——t2i v1.1 已消灭
  "curl 命令串 + shell=True，prompt 一个撇号=宿主任意命令执行"的老写法（源码注释留有教训），
  并档与新写的 TP 变体一律继承此纪律，不得回退、不得"临时调试"绕过。
- TP 变体定性（主人 2026-11-17）：**就是原来临时改 `DASHSCOPE_T2I_API_KEY`/
  `DASHSCOPE_T2I_BASE_URL` 指向 token-plan 端点做到的事**，v0.5 升格为内置 `t2i.enabled_tp`
  开关——端点与 Key 取顶层 `tp_base_url`/`tp_key_env`，注册名 `dashscope-tp`，API 形态与
  按量变体完全一致（同一份代码类，只是构造参数不同，勿复制粘贴两份）。
- 模型来源从"内置三模型清单"改为 §5.3 实时列表；`wan2.7-image` 仍是缺省首选。

### 6.2 web（并自 web-dashscope，全部不变项）
- MCP Streamable HTTP 三步握手、session 缓存 + 作废只重握手一次、超时 20/30/60s、
  `web_extract` 返回纯列表（包壳即 AttributeError）、F19 外部内容围栏。端点域名改从 §5.3 派生。

### 6.3 同名不同 registry
`dashscope` 同时出现在 profile、TTS/transcription、image_gen、web_search 四个 registry，
`dashscope-tp` 出现在 TTS/transcription 与 image_gen——各 registry 独立命名空间，**不算冲突，不许"修复"**。

### 6.4 chat 名占 `dashscope` 的覆盖裁定（接受项）
- 覆盖 bundled alibaba 的 `dashscope` alias → 今后 `model.provider: dashscope` 解析到原生线
  （原 compatible-mode）。本机 chat 走 `custom:dashscope-token-plan` 路由不受影响；
  models_catalog_static 映射连带变化照旧接受，勿当 bug 修。
- 旧 aliases（`dashscope-native`、`qwen-dashscope`、`dashscope-native-api`）不保留。

### 6.5 t2i 参考图/图像编辑（v0.5.3 需求书，主人 2026-11-17 发起，待裁定后动工）

**目标**：`image_generate` 带上参考图就能编辑/融合/风格参考，不再被 provider 拒绝。
核心工具层零补丁——`image_url`/`reference_image_urls` 参数本来就收，工具面按
`capabilities()` 动态渲染（官方 openai 图像插件同款做法）。

**6.5.1 能力声明（capabilities 动态化）**
- 现读系统级 `image_gen.model`（官方 `plugins/image_gen/_common.py` 的
  `load_image_gen_config()` 或直接 `hermes_cli.config.load_config`，与核心
  `_active_image_capabilities` 的解析口径一致），按模型家族查能力表：
  | 家族（前缀匹配，日期快照归基础家族） | modalities | max_reference_images | 单图上限 |
  |---|---|---|---|
  | wan2.7-image / wan2.7-image-pro | text+image | 9 | 20MB |
  | qwen-image-3.0 / -pro | text+image | 3 | 10MB |
  | qwen-image-2.1-pro / -turbo | text+image | 10 | 10MB |
  | qwen-image-2.0 系（含快照） | text+image | 3 | 10MB |
  | 其余（z-image-turbo、wan2.6/wan2.5/wan2.2/wan2.1、wanx2.1-imageedit、未收录 id） | text | 0 | — |
  - 2026-11-18 实现日二次取证定稿：qwen-image-generation-and-editing-api-reference 页
    明写「qwen-image-2.1系列支持传入1-10张图像，qwen-image-3.0系列支持传入1-3张图像」；
    qwen-image-edit-api 页（实为 2.0 系文档）「包含1-3张图像」；wan 页「可传入0-9张图片」。
    negative_prompt 仅 qwen-image-3.0/2.0 系支持（wan 页全文零命中；2.1 页明写仅3.0系，
    但 2.0 的 edit 页列有此参数——按页各自收录）。n 范围：qwen 系 1-6、wan2.7 1-4（同步）。
    老 `qwen-image-edit` 独立 id 未出现在 live 目录，不收录（fail-closed 自然兜住）。
- 能力表是**官方文档实锤的静态事实**（2026-11-17 查证：wan-image-generation-and-editing-api-reference、
  qwen-image-generation-and-editing-api-reference、qwen-image-edit-api、image-model 总表），
  不是营销字段，允许内置；上游新增家族未收录时按 text-only 保守声明（fail-closed，与核心一致）。
- capabilities() 每次调用现读配置（picker 每帧都调，禁网络、禁重 I/O）。

**6.5.2 generate 编辑路径（同端点同信封）**
- 端点不变：`{base}/api/v1/services/aigc/multimodal-generation/generation` 同步 POST。
  官方编辑信封 = `content` 数组先放 N 个 `{"image": ...}` 再放**一个** `{"text": 指令}`
  （多 text 或不传 text 都报错）。`image_url` 与 `reference_image_urls` 合并为有序列表
  （image_url 在前），按能力表上限钳制，超出部分丢弃并在日志说明。
- 输入形态三种，逐一处理：
  1. `http(s)://` 公网 URL → 原样透传（不下载、不探测，URL 可达性由上游裁定）；
  2. `data:image/...;base64,` → 原样透传（校验大小上限）；
  3. 本地绝对路径（含 `file://`）→ 读盘 + mimetypes 猜 MIME + base64 data URI。
     读盘失败/超大小/非图像 MIME → 响亮 invalid_input 错误，**绝不静默丢图**。
- 不支持编辑的模型收到参考图 → 保留现拒绝语义但改教学文案：点名当前模型不支持编辑、
  提示去 `hermes setup tools` → Image Generation 换编辑模型（对齐核心 FAL 路径的报错风格）。
- **画幅策略（编辑模式，主人 2026-11-18 已裁）**：qwen 系不传 size（官方默认跟随
  末张输入图比例），**除非模型要求特定尺寸**——能力表增设 `edit_size_policy` 列
  （`follow_input` / `required:<尺寸>` / `pixel_map`），现查文档所有支持编辑的家族
  均不强制尺寸；未来家族若强制，按表执行。wan2.7 沿用现有像素映射（2K 内）。
  文生图路径画幅映射不变。此差异写入 README。
- 可选参数顺带透传（kwargs 里出现才带）：`n`（qwen 系 1-6、wan2.7 同步 1-4，数值
  原样透传不硬校验——上游超界会报 InvalidParameter）、`seed`、`negative_prompt`
  （wan2.7 与 qwen-2.1 不支持，带了会被上游拒——按家族过滤）、`watermark`（默认不传）。
  `prompt_extend` 不动（上游默认 true）；wan2.7 不支持 prompt_extend，不传。
- **超时（主人 2026-11-18 裁定：适当延长，直至转后台异步）**：文生图 120s 不变；
  编辑/多图 240s。~~上游无异步可转~~ **实现时取证修正（2026-11-18）**：wan2.7
  其实有异步通道（`image-generation/generation` 提交 + `GET /api/v1/tasks/{task_id}`
  轮询，页内「HTTP异步调用」节，`X-DashScope-Async: enable` 必带）；「仅同步」
  只对 qwen-image 系成立。但「转后台异步」需要核心侧任务续跑机制（image_generate
  工具没有，提交后无人 steer 回来），插件内自轮询=换个姿势阻塞等待，无增益。
  故本期两条路都走同步长等待：到点响亮 timeout 错误，文案注明
  「请求可能仍在服务端完成并计费；同步通道无任务句柄，本次结果无法找回」，
  不静默重试（重试=二次计费）。wan2.7 异步通道记为 v0.5.4 备选（插件可自建
  提交/查询两步工具实现真后台）。响应解析复用现逻辑
  （output.choices[*].message.content[*].image）。
- **多图输出（主人 2026-11-18 裁定）：上游给几张返回几张，不截断。**
  核心契约是单 `image` 字段 + `extra` 透传（`success_response` 的 extra 键原样并进
  结果、工具层不做裁剪），故交付形态：`image` = 第一张 URL（兼容现有消费方），
  `extra={"images": [全部 URL 按上游顺序], "image_count": N}`。逐张遍历所有
  choices 的所有 content 项收集 image，一张不丢；n 参数（qwen 系 1-6）照常透传，
  上游实际返回数与 n 不符时以实际为准。零核心补丁。
- 安全纪律不变：urllib 直发、Key 只进头、错误体 sanitize、本地路径只读不写。

**6.5.3 明确不做（本期范围外，主人 2026-11-18 已裁 d 条）**
- **老端点一律不支持**：wan2.5-i2i-preview / wanx2.1-imageedit / wan2.6-t2i 的异步
  `image-generation/generation` 端点 + task 轮询不做；目录里这些模型保持 text-only，
  收到参考图时教学式拒绝并点名「该模型走老异步端点，本插件不支持」。
- bbox_list 交互式编辑、upscale、creative_controls 声明。
- 参考图自动缩放/压缩（超限直接报错，不做有损处理——主人要原图质量时不许偷偷降质）。

**6.5.4 验收（实现时逐条过）**
- E1 capabilities 随 image_gen.model 切换：qwen-image-3.0-pro → 3 refs；wan2.7-image → 9；
  z-image-turbo → text-only；未收录 id → text-only。
- E2 编辑信封冻结测试：content = [image×N, text×1] 顺序与数量；多 text 构造不出来。
- E3 本地文件 → data URI（MIME 正确）；不存在/超大小/非图像 → invalid_input 响亮报错。
- E4 钳制：给 wan2.7 传 12 张 → 只发 9 张；给 qwen-3.0 传 5 张 → 只发 3 张。
- E5 不支持编辑的模型 + 参考图 → 教学式拒绝，零网络。
- E6 qwen 编辑不带 size、wan 编辑带像素 size；文生图画幅映射回归不变。
- E7 negative_prompt 在 qwen-2.1 家族被过滤；seed/n 透传。
- E7b 多图全量交付：冻结响应含 2 个 choice × 各 2 张 → images 列表 4 张按序、
  image=第一张、image_count=4；单图响应 images=[该图]（形态恒定）。
- E8 ~~live 探针~~ —— 主人 2026-11-18 裁定不做（「最好别」）。编辑路径以冻结响应
  测试为准；首次真实使用即验收，出错按 upstream_error 原文透传排查。
- E9 通道审计扩展：编辑路径同样零 curl/subprocess/shell。
- E10 超时语义：编辑路径 240s、文生图 120s；超时错误文案含「可能已计费/无异步
  查询口」警示；超时后零自动重试（冻结计时器断言只发一次请求）。

## 7. 目录结构与代码纪律

- `ds_image.py`（并自 t2i）、`ds_web.py`（并自 web-dashscope），`register(ctx)` 按样板树
  依次拉起各块各变体。
- 迁移代码原样搬运，保留原作者风格（函数内 import、带括号 return 等怪癖不重构不美化——
  主人 2026-08-28 纠正，本仓 DIY 代码一律适用）。
- 旧目录 `t2i-bailian/`、`web-dashscope/` git mv 进 `_archived-plugins-dont-use/`（不删）。
- `plugin.yaml` version 0.5.0；改前 `_backup/merge-20261117/` 快照已在。

### 7.1 config.yaml / .env 迁移清单（主人自己动手，AI 不代改）
1. `plugins.enabled`：删 t2i-bailian、web-dashscope 两行。
2. `image_gen.provider: t2i-bailian` → `dashscope`（或 `dashscope-tp`，看想用哪侧计费）。
3. dashscope-native settings 旧树清空，按 §2 样板重写（或删 settings 让逐键自动补齐落新树）。
4. `.env`：`DASHSCOPE_T2I_API_KEY`、`DASHSCOPE_T2I_BASE_URL`、`TOKEN_PLAN_API_KEY` 可删
   （`DASHSCOPE_API_KEY`、`DASHSCOPE_TOKEN_PLAN_API_KEY` 已在位且实测可用）。

## 8. 验收清单

- V1 注册名：profile/api_mode=`dashscope`；语音/t2i=`dashscope`+`dashscope-tp`；web=`dashscope`。
- V2 连坐禁止：模拟 SDK 缺失 → chat/语音按量降级，TP 语音/t2i/web 照常。
- V3 开关矩阵：`enabled`/`enabled_tp` 逐键开关，只消对应变体，其余健在（含 t2i 双变体同开）。
  分车道核对：TP 车道实际使用 `tp_model`/`tp_voice`/`tp_sample_rate`；未配 tp_ 键时回落裸名值。
- V4 逐键自动补齐：手删一键补回默认；显式 false/"" 不覆盖。
- V5 凭证收敛：.env 只剩两个 DASHSCOPE 变量，全部功能通。
- V6 model_list 实时性：上游增删模型后（缓存过期重拉）列表跟随；models 端点故障不炸注册。
- V7 音色不硬校验：`longanxiaoxin` 直通上游，InvalidParameter 原文响亮透传。
- V8 旧名清零：`t2i-bailian`/`dashscope-native`/`qwen_dashscope` 不再出现在任何 registry/alias。
- V9 通道审计：全插件 grep 无 `curl`/`subprocess`/`shell=True` 触网路径（§6.1 硬性要求）。

## 待主人裁定项（实现前必须落定）
1. ~~§5.3：models 缓存 TTL / 失败回退策略~~ —— 已裁（v0.5.1：TTL 3600s，失败回落存量，无硬编码兜底）。
2. ~~§6.5（v0.5.3 参考图）整体方案~~ —— 已裁（主人 2026-11-18）：
   a. 编辑画幅 qwen 跟随输入图，除非模型强制尺寸（能力表 edit_size_policy 列）；
   b. 超时适当延长（编辑 240s）；「转后台异步」上游不支持（wan2.7/qwen-image 页
      明写仅同步），落地为响亮超时+计费警示+零重试，见 §6.5.2；
   c. 多图：上游给几张返回几张（image=第一张 + extra.images 全量）；
   d. 老异步端点不支持。
3. ~~live 探针放行~~ —— 已裁：不做（「最好别」），冻结测试为准，首次真实使用即验收。
4. **唯一遗留**：§6.5.2 的 240s 具体数值是助理建议（主人只说「适当延长」），
   实现时若实测不够再调；不阻塞动工。

~~§4 语音分车道覆盖键~~ —— 已裁（2026-11-17：`tp_` 前缀覆盖键，回落规则见 §4）。

## 坑账（追加区，一事一条）

- 2026-11-17：v0.2 注释引用 PROMPT.md 条款号但文件从未入库；本版成文，条款增删先改本书再写代码。
- 2026-11-17：t2i 旧 README 的独立 Key 是为分额度；主人 v0.5 裁定收敛，分 Key 需求回归时
  改 `key_env`/`tp_key_env` 指向别的变量名即可，不恢复专用变量。
- 2026-11-17：chat 占名 `dashscope` 覆盖 bundled alibaba alias，改变 `model.provider: dashscope`
  解析协议（§6.4）——裁定后果不是 bug。
- 2026-11-17：主人"只注册 api_mode 不注册 model provider"的实质是命名面统一；profile 机制
  不可删（create_client 反壳缝挂其上）。
- 2026-11-17：`/compatible-mode/v1/models` 两变体实测可直拉 id 清单，但无 display/价目字段；
  model_list 的 strengths/price 若上游不给就留空，禁止编造。
- 2026-11-17（v0.5.1）：STT picker 不见百炼是**核心缺口**非插件 bug——
  `hermes_cli/tools_config_providers.py` 的 `_PLUGIN_ROW_BUILDERS` 只登记了五类，
  STT 无插件行注入口（桌面端 `web_server_config.py` 走另一条合并路径所以能选）。
  已补对称 stt 条目（spec + partial + builder 三处，备份在
  Data/cache/scratch/tools_config_providers.py.bak-stt-picker）；核心仓 git 可回滚。
  上游 hermes-agent 升级会冲掉此补丁，届时需重放或推上游 FR。
- 2026-11-17（v0.5.1）：t2i 目录从"每变体各拉各的+内置三件套兜底"改为"按量全量单目录
  双变体共享 + discover_models 开关"，内置快照删除——兜底回退到硬编码三件套本身就是 bug。
- 2026-11-18（v0.5.3 需求书）：多图交付契约取证——核心 `success_response` 只定义单
  `image` 字段，但 `extra` 键 setdefault 并进结果、`_postprocess_image_generate_result`
  只注解不裁剪，故 `extra.images` 全量列表可零核心补丁透传给模型；FAL 路径的
  `images` 列表是另一套内部形态（dict 带 url/width/height），插件路径勿混用。
- 2026-11-18（v0.5.3 实现日）：需求书曾据「不支持异步接口」一句判 wan2.7 无异步通道——
  那句在 **Python SDK 节**，说的是 SDK 不封装异步；HTTP 层其实同步(multimodal-generation)
  与异步(image-generation + GET /api/v1/tasks)两条都有。教训：读 API 文档按节归属读，
  关键词命中要回看上下文标题，别把子节限定语当全页结论。本期仍走同步（转后台缺核心续跑
  机制），异步通道记 v0.5.4 备选。
- 2026-11-18（v0.5.3 实现日）：capabilities() 的口径=系统级 image_gen.model（与核心
  _active_image_capabilities 的 _read_configured_image_model 同一把钥匙）；测试冻结面
  用 monkeypatch ds_image._system_image_model 注入，勿改真实 config.yaml。
