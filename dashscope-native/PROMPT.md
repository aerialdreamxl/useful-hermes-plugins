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
- 过滤图像族（id 含 image/wan/z-image，排除 -video）。样板字段 `display/strengths/price`
  端点只给 id：display 本地美化，strengths/price 留空，**不编造**。
- 存量列表兼容手改：`t2i.models` 里写纯 id 字符串数组也认（自动包成条目）。

## 6. 功能面细则

### 6.1 t2i（并自 t2i-bailian，行为不变项 + TP 变体新增项）
- 不变：画幅映射（landscape→1696*960、portrait→960*1696、其余→1280*1280）、text-only
  声明、参考图明确拒绝、120s 超时。
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
1. §5.3：models 缓存 TTL / 失败回退策略（当前默认：上次成功值 → 内置快照兜底）。

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
