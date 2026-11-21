# 「AerialdreamXL 的 Hermes 工具套组」需求书

版本：v0.4（2026-11-07，工具面已定稿，待冻结后开工）
状态：**需求书阶段，未落实现代码**
源码基线：Hermes v0.23.0（install: `D:\01-AI\Hermes-Agent\Data\hermes-agent`，本地 2bd378ed1b8d），机制锚点均在该树实读验证。

## 0. 变更记录

- v0.1：初稿，B 站拆分 + 单一「搬家合并」路线 + 开放在裁决。
- v0.2：B 站口径钉死为「口径 B——需要登录的才划到 unsafe」；工具面扩为四组；其余想法改走 Python+Skill；remotes 与 androiduse 列二期。
- v0.3：默认全注册；ASR 支持等待/异步提交/完成后 steer 回会话；multimedia-utils 增加 `dashscope_qwenomni_analysis`（Qwen-Omni 音视频分析，模型/端点/密钥可配）。
- v0.4：取消「慎用」标注（清单行标签不可定制，用户裁定不做）；Omni 超 10MB 视频改为先压缩、仍超限则截到约 9.9MB 并告知模型，不抽帧。
- v0.5（2026-11-20 安全修复批次）：`bilibili_unsafe`/`android-utils` 改默认关（schema + 代码兜底，`group_bilibili_unsafe_enabled` 确认在 `_SETTING_KEYS` 读取链内）；输出路径（bili filename / ffmpeg output / 截屏 path / adb pull local）钳制 scratch realpath 且拒绝覆盖；multimedia 本地 `source` 限 scratch/plugin-data realpath + 媒体 MIME/扩展名 + 大小硬校验，data URI ≤10MB；B 站流下载 URL HTTPS + host 白名单且不跟随重定向；confirm 文案声明「模型自设参数，宿主不代审批」；credential 0600 措辞改诚实。

## 1. 一句话定位

一个单体插件包（`aerialdreams-toolsets`），同一 `register(ctx)` 入口注册多个独立 toolset：`bilibili`、`bilibili_unsafe`、`multimedia-utils`、`desktop-utils`、`android-utils`（二期 `remotes`）。`bilibili`/`multimedia-utils`/`desktop-utils` 默认注册，`bilibili_unsafe` 与 `android-utils` 默认关闭（2026-11-20 安全修复批次改判，见 §6），每组在 `hermes tools` 清单里独立开关；工具是写死的，变数多的能力交给 Skill + Python。

## 2. 可行性与机制边界（实测锚点）

| 机制 | 源码锚点 | 结论 |
|---|---|---|
| `ctx.register_tool(name, toolset, ...)` 的 toolset 参数可自定 | `hermes_cli/plugins.py:463` | 插件自定套组键合法 |
| 单插件多套组已有先例 | `bili-tools/__init__.py:786`（两组并存） | 被实战验证的形态 |
| 插件套组自动进 `hermes tools` 清单（🔌 前缀） | `hermes_cli/plugins.py:2136`；`tools_config.py:_get_effective_configurable_toolsets()` | 每组独立勾选、独立持久化 |
| 套组解析走注册表合并 | `toolsets.py:363 resolve_toolset()`、`toolsets.py:444 get_toolset_names()` | 组名可当 `platform_toolsets` / `agent.disabled_toolsets` 开关单位 |
| 套组键跨进程持久化 | `hermes_cli/plugins.py:1736-1751`、`config.yaml:known_plugin_toolsets` | CLI 冷启动可渲染清单 |
| `agent.steer(text)` 可向会话注入一段 steer 文本 | `tui_gateway/methods_tools.py:_cmd_steer`；`agent_runtime_helpers.apply_pending_steer_to_tool_results` | 异步任务完成后可把结果送回原会话（空闲会话在下一轮消化） |

边界条件：

1. manifest 无 `toolsets:` 声明字段（`plugins_manifest.py:32`），分组只由运行时 `register_tool` 决定；`provides_tools` 逐工具声明。
2. 同名工具跨组注册会被硬拒（`tools/registry.py:777`）；unsafe 变体必须换名，不能两组同名。
3. **套组行标签不可单独定制**：`get_plugin_toolsets()` 的标签由 key 自动生成（`🔌 + key.title()`），描述取插件 manifest description（所有组共用）；插件 API 没有单组备注接口。用户裁定：不做「慎用」标注。
4. memory provider / model provider 类插件不能被本套组收编（独立扫描器，机制不允许）。
5. `requires_plugins` 仅为装载顺序 + 建议性依赖；本包合并实现后不依赖它。
6. 部署位决定配置键：必须放 `$HERMES_HOME/plugins/aerialdreams-toolsets/` 顶层；放 `custom-plugins/` 下会得到带前缀的 path-derived key，`plugins.entries.<id>` 查找落空。

## 3. 已冻结的决策

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 形态 | 单体 agent-side 插件，`kind: standalone`，一个 `register(ctx)` 注册 N 个 toolset |
| D2 | B 站拆分口径 | **口径 B**：匿名（免登录）可达 → `bilibili`；需要登录 cookie / 写操作 → `bilibili_unsafe` |
| D3 | B 站工具归属 | 见 §5.1 / §5.2；高清下载、AI 字幕、赞币藏、私信、评论/弹幕发布、收藏夹、历史、稍后再看进 unsafe |
| D4 | 新工具组 | `multimedia-utils`、`desktop-utils`、`android-utils` 准许制作；`remotes`、`androiduse` 二期 |
| D5 | 合并原则 | 同一底层能力的多动作合并成一个工具（action 参数），不拆微工具 |
| D6 | 其他想法 | image/pdf/docx/xlsx/yt-dlp/qr/系统监控等一律走 Python + Skill，不进工具面 |
| D7 | bili 实现归属 | 整体搬运自 bili-tools（函数级移动，不重写 handler），旧插件 disable → `_attic/` 退场 |
| D8 | 部署位 | `$HERMES_HOME/plugins/aerialdreams-toolsets/`（顶层，非 custom-plugins/） |
| D9 | 注册与闸门 | 低风险组默认注册；`bilibili_unsafe`/`android-utils` 默认关（settings 显式打开）；不做「慎用」标注（见 §2 边界 3） |
| D10 | 依赖策略 | v1 目标 stdlib-only；ffmpeg/adb/fastboot/ssh 全部子进程调用系统已装程序；ASR/Omni 用 stdlib urllib 打百炼 |
| D11 | ASR 异步 | 同步/异步自动选路；`wait_seconds` 可等（默认 300，上限 1800）；超时返回 task_id；后台完成后 `agent.steer()` 把结果送回原会话 |
| D12 | Omni 分析 | 新增 `dashscope_qwenomni_analysis`：音视频 + 自定义 prompt 交给可配置的 Qwen-Omni 模型，返回完整分析文本 |
| D13 | Omni 超限 | 本地文件 >10MB：先 ffmpeg 压缩（降码率/分辨率）再试；仍超限则截到约 9.9MB 的位置，并在结果里明确告知「已截断、剩余未分析」；不抽帧 |

## 4. 包结构

```
aerialdreams-toolsets/
├── plugin.yaml          # name/version/description/kind: standalone
│                        # provides_tools: 全集（含默认关闭的 unsafe 工具，照常声明）
│                        # config_schema: 各组开关 + ASR/Omni/路径设置
├── __init__.py          # register(ctx)：读 settings → 按组注册 TOOL_DEFS
├── groups/
│   ├── bilibili.py          # 读组（公开匿名）
│   ├── bilibili_unsafe.py   # 登录/写组
│   ├── multimedia.py        # ffmpeg + dashscope_asr + dashscope_qwenomni_analysis
│   ├── desktop.py           # clipboard + screenshot
│   ├── android.py           # androidadb + androidfb（androiduse 二期）
│   └── remotes.py           # 二期：remote_shell + remote_files
└── README.md
```

注册约定：
- 每组一个模块、一张 `TOOL_DEFS` 表（沿用 bili-tools 的 `(name, toolset, handler, props, required, desc)` 六元组），注册循环收在 `__init__.py`。
- 子模块用 `from .groups.bilibili import TOOL_DEFS` 式相对导入（`from . import x` 在包加载形态下会哑弹）。
- handler 内对三方依赖一律惰性导入；缺依赖返回带安装指引的错误串，不抛异常进会话。
- 某组 import 失败 → 该组跳过 + `logger.error` 点名，其余组照常注册（组粒度 fail-open）。

## 5. 工具面（v1）

### 5.1 `bilibili`（公开，匿名）

| 工具 | 状态 | 说明 |
|---|---|---|
| `bili_search` | 迁入 | 关键词检索；新增 `type` 参数（video/user/bangumi） |
| `bili_video_info` | 迁入 | 视频元数据（分P/cid/stat） |
| `bili_comments` | 迁入 | 顶层评论读取 |
| `bili_danmaku` | 迁入 | 弹幕读取 |
| `bili_subtitle` | 迁入 | 匿名 CC 字幕 |
| `bili_download` | 迁入 | 匿名画质上限下载；新增 `audio_only` |
| `bili_user_info` | 新增 | UP 资料卡（昵称/粉丝/简介/作品数） |
| `bili_user_videos` | 新增 | 按 UP 拉作品列表（分页/排序） |
| `bili_hot` | 新增 | 热门榜/分区排行 |
| `bili_comment_replies` | 新增 | 楼中楼（某评论下回复） |
| `bili_related` | 新增 | 相关推荐视频 |
| `bili_collection` | 新增 | 合集/系列视频列表 |
| `bili_dynamic` | 新增 P2 | UP 动态；实现时验证免登录可达，不可达则移入 unsafe |

### 5.2 `bilibili_unsafe`（需要登录）

| 工具 | 状态 | 说明 |
|---|---|---|
| `bili_login` / `bili_login_check` | 迁入 | 扫码登录 + 轮询；凭据落 `plugins-data/aerialdreams-toolsets/credential.json` |
| `bili_download_hd` | 新增 | 登录后 1080P+/4K |
| `bili_interact` | 新增 | `action=like/unlike/coin/fav/triple`（合并） |
| `bili_dm` | 新增 | `action=send/list`（私信合并） |
| `bili_comment_write` | 新增 | `action=post/delete`（替代旧 `bili_comment_post`，合并） |
| `bili_danmaku_post` | 迁入 | 弹幕发送 |
| `bili_fav` | 新增 | `action=folders/list/add/remove`（收藏夹合并） |
| `bili_watch_later` | 新增 | `action=list/add/remove`（稍后再看） |
| `bili_history` | 新增 | 播放历史 |
| `bili_subtitle_ai` | 新增 | 登录后 AI 字幕 |
| `bili_follow` | 新增 P2 | 关注/取关 |

### 5.3 `multimedia-utils`

| 工具 | 说明 |
|---|---|
| `ffmpeg` | `action=probe/convert/trim/mux/extract_audio/extract_frames/thumbnail/burn_subtitle/gif`；输入统一绝对路径（媒体扩展名 + 2GB 大小硬校验）；输出钳制在 scratch realpath 范围内且默认拒绝覆盖，`output`/`filename` 只能落在 scratch 内 |
| `dashscope_asr` | `action=transcribe/status/result`；短音频走 `qwen3-asr-flash`（同步，本地 base64 / URL），长音频走 `qwen3-asr-flash-filetrans`（异步）；本地 `source` 限 scratch/plugin-data realpath 范围 + 媒体扩展名/MIME + 100MB 硬校验；`wait_seconds`（默认 300，上限 1800）内轮询，超时返回 task_id 并后台继续；完成时 `agent.steer()` 把结果送回原会话 |
| `dashscope_qwenomni_analysis` | 音视频交给可配置的 Qwen-Omni 模型分析：`source`（scratch/plugin-data 内本地路径、http(s) URL 或 ≤10MB data URI）、`prompt`（自定义，必填）、`model`（默认取 settings）；本地 base64 <10MB 直接传；超限先用 ffmpeg 压缩（降码率/分辨率）再试，仍超 10MB 则截取到约 9.9MB 的位置并把「已截断、后续未分析」告知模型；返回完整分析文本 + usage |

### 5.4 `desktop-utils`（Windows）

| 工具 | 说明 |
|---|---|
| `clipboard` | `action=read/write`，文本剪贴板；PowerShell `Get-Clipboard/Set-Clipboard` |
| `screenshot` | `target=full/region/window`；输出 PNG 到 scratch，返回路径（供 `vision_analyze`） |

隐私约束：两工具只在显式调用时动作，不后台抓取；截图默认只存 scratch。

### 5.5 `android-utils`

| 工具 | 说明 |
|---|---|
| `androidadb` | `action=devices/shell/push/pull/logcat/screenshot/install/uninstall/props/am/pm`；exec 返回 stdout+exit code |
| `androidfb` | `action=devices/flash/reboot/oem`；**flash 必须显式 `partition`+`image`+`confirm=true`**，无默认目标 |
| `androiduse` | 二期：`action=tap/swipe/text/key/screenshot/dump`（类 CUA 手机操作） |

### 5.6 `remotes`（二期）

| 工具 | 说明 |
|---|---|
| `remote_shell` | `action=exec/connect/close`；协议 `ssh/telnet/serial`；SSH 走系统 `ssh.exe`，串口实现时定（pyserial 或 PowerShell） |
| `remote_files` | `action=list/get/put/rm/mkdir`；同协议栈 |

## 6. 开关与默认值

| toolset | 默认 | 闸门 |
|---|---|---|
| `bilibili` | 注册 | settings 可关 |
| `bilibili_unsafe` | **默认关**（settings 显式开） | 开关真正进 `_SETTING_KEYS` 读取链 |
| `multimedia-utils` | 注册 | settings 可关；ffmpeg 缺失时工具自报错，组不关 |
| `desktop-utils` | 注册 | settings 可关；显式调用才动作 |
| `android-utils` | **默认关**（settings 显式开） | confirm 是模型自设参数、宿主不代审批，组默认关才是真闸门；fb `flash` 仍需 `confirm=true` |
| `remotes` | 注册（二期上线后） | settings 可关 |

上下文成本：全注册约 32 个工具（bilibili 13 + unsafe 12 + multimedia 3 + desktop 2 + android 2）。如工具面过大，可在 config `tools.tool_search.defer` 里把低频工具延迟加载（用户的配置动作，插件不代劳）。

## 7. 依赖与运行时事实（实测）

- ffmpeg/ffprobe：ffmpeg 9.x（win32-x64）目录已在 PATH。
- adb/fastboot：本机 platform-tools 目录已在 PATH（adb 37.x）。
- ssh/scp：系统 OpenSSH 已在 PATH。
- 运行时解释器：`$HERMES_HOME/tools/python-3.14.x/python.exe`，已带 `PIL`、`pptx`、`qrcode`、`requests`；**无** `pypdf/docx/openpyxl/numpy/yt_dlp`。
- 百炼 ASR：`qwen3-asr-flash`（同步，OpenAI 兼容 + DashScope 同步）与 `qwen3-asr-flash-filetrans`（DashScope 异步）；`.env` 有 `DASHSCOPE_API_KEY`。
- 百炼 Omni：`qwen3.8-omni-flash`（文本输出，默认；默认开启思考模式）、`qwen3.5-omni`（语音输出）；兼容模式 base_url 建议业务空间域名 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，回退 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
- Omni 输入限制：本地 Base64 音频/视频编码后必须 <10MB；公网 URL 音频/视频（3.8-flash/3.5 系列）不超过 2GB、最长 3 小时；`qwen3-omni-flash` 为 100MB/20 分钟。
- Omni 超限策略（v0.4）：本地文件先用 ffmpeg 压缩（目标 <9.9MB，按码率/分辨率/时长迭代）；压不下去就按 9.9MB 边界截断，返回结果里明确告知「视频已截断至约 N 秒，剩余部分未分析」；不抽帧。
- PowerShell 可用（clipboard/screenshot 走它）。

## 8. 迁移与切换门禁（bili-tools → 套组）

1. 开发目录先跑 `hermes plugins validate <dev-dir>`（免部署 15 项门禁）。
2. 旧 bili-tools 退场：`hermes plugins disable bili-tools`（趁目录在）→ 目录整体移出扫描根进 `_attic/`（根内 `.bak` 会被发现器抢先解析）→ 套组包部署顶层 → enable。
3. config.yaml `plugins.disabled:` 旧名幽灵条目手工清掉（**用户本人动作**，我给逐行 diff）。
4. 门禁表：

| 指标 | 门槛 | 验证 |
|---|---|---|
| 工具数 | bare doctor：全套组工具全注册（含 unsafe，因默认注册） | `hermes plugins doctor` 对数 |
| 旧插件零残留 | `plugins list` 无 bili-tools；`bili_search` 真实返回 | 新会话实跑一发 |
| 清单渲染 | `hermes tools` 出现各组 🔌 行；勾选持久化 | 人工 + grep config |
| 顶层键生效 | `plugins.entries.aerialdreams-toolsets.settings` 被 `ctx.get_config` 读到 | 插件自检日志 |

回滚：套组目录移回 `_attic/`，bili-tools 复原 enable；两侧不共享数据。

## 9. 待裁决（冻结前只剩这些）

| 编号 | 问题 | 我的建议 |
|---|---|---|
| 9.1 | Omni 默认模型是否用 `qwen3.8-omni-flash`（settings 可改） | 是；模型/端点/密钥全走 settings |
| 9.2 | 全注册后工具面约 32 个，是否现在就 defer 一部分低频工具 | 先全开观察，必要时由用户配 `tools.tool_search.defer` |
| 9.3 | `remotes` 二期上线时默认注册还是默认关 | 默认注册（与「默认都注册」一致） |

## 10. 非目标（明确不做）

- 不收编 memory provider / model provider 类插件。
- 不注册 toolset 别名、不碰 `registry.register_toolset_alias`、不 override 内置工具。
- 不做 image/pdf/docx/xlsx/yt-dlp/qr/系统监控等工具（走 Skill + Python）。
- 不做「慎用」标注（套组行标签不可定制）。
- 不新建 cron、不写扫描根内备份目录。
- 不改 bili handler 行为语义（等价搬运；新增能力另算）。

## 11. 验收标准

1. `hermes plugins validate <dev-dir>` 无 FAIL、无本可避免的 WARN。
2. 部署 + enable + **整进程重启**后：新会话 `bili_search` 真实返回 BV 号；`bili_download` 匿名路径可下；`ffmpeg(probe)` 对下载文件返回时长/码率。
3. `bilibili_unsafe` 已注册；关闭该组后 `bili_login` 不在工具面；开启后扫码登录成功。
4. `dashscope_asr` 短音频同步返回带时间戳文本；长音频 `wait_seconds=5` 返回 pending，后台完成后 `steer` 文本出现在原会话（或下次交互时可见）。
5. `dashscope_qwenomni_analysis` 对一段本地短视频返回完整分析文本；对超 10MB 视频先压缩、仍超限则截断到约 9.9MB 并在结果里明确告知截断位置。
6. `clipboard(read)` 返回当前剪贴板文本；`screenshot(full)` 落盘 PNG 且 `vision_analyze` 可读。
7. `androidadb(devices)` 列出已连设备；无设备时返回空列表不报错。
8. 断 settings 文件：默认档注册读组、不炸会话，一行 WARN。

## 12. 坑位记录

（实现阶段追加，格式：现象 / 根因 / 规避）

预判条目（来自既有实测，届时逐条兑现）：
- 扫描根内 `.bak` 抢解析；custom-plugins 前缀键静默落空；长驻进程热载假象（对 CreationDate）；包加载形态 `from . import` 哑弹；gated 工具 bare-doctor WARN 属预期；adb 多设备时必须 `-s serial`；ffmpeg dash 合并需同时取 video+audio 轨；PowerShell 输出编码（UTF-8 声明）。
- Omni 本地 Base64 <10MB 硬限；超限先压缩、仍超限按 9.9MB 截断（不抽帧）；`qwen3-omni-flash` 与 3.8-flash 的时长/体积上限不同。
- `agent.steer()` 注入空闲会话后会等到下一轮才被消化（源码注释明示），不是即时弹窗。
- 套组行标签由 key 生成，插件改不了。

### 实施实录（2026-11-08 凌晨，已于部署副本实测）

- **ASR 同步 400**：`qwen3-asr-flash` 是专用 ASR 任务，`content` 里只能有 `input_audio`，且 `input_audio` **不能带 `format`**；带文本 part 或 format 都返回 `400 dedicated task asr does not support this input`。语言走 `asr_options={"language":"zh"}`（实测 200）。同步不返回时间戳。
- **UP 作品列表匿名风控**：`x/space/wbi/arc/search` 无登录态稳定 `-352/-799`（带 buvid3、dm_img_* 也一样）。已改为：先试 space，失败回退「按 UP 名搜索 + author 过滤」，返回 `source:"search_fallback"` 并在 `note` 说明结果不全。
- **空间动态必须登录**：匿名 wbi 请求偶发 code 0 但 items 空，多数直接 `-352/412`。已把 `bili_dynamic` 移入 `bilibili_unsafe`，`_ck()` 自动补 buvid3（`x/frontend/finger/spi`）。
- **`x/player/wbi/v2` 必须签名**：匿名 `bili_subtitle` 原先未带 `w_rid/wts`，返回「无字幕」是被接口拒绝。已改为 `wbi_sign({bvid,cid})`。
- **弹幕发送端点**：`x/v2/dm/web/submit` + `c` 参数不适用于网页端；改为 `x/v2/dm/post`，字段 `type=1, oid=cid, bvid, progress(ms), color, fontsize, pool, mode, plat, csrf`。
- **历史接口形状**：`x/v2/history` 的 `data` 可能是 list 也可能是 `{list:...}`，作者字段是 `author_name` 而非 `owner.name`；已兼容两种。
- **QR 码依赖陷阱**：`qrcode`/Pillow 只存在于 PM venv（靠 PYTHONPATH 注入），真实 `python -I` 下 `ModuleNotFoundError`。已内置 Nayuki `qrcodegen.py`（MIT）+ 纯 stdlib PNG 编码器 `common/qr.py`，`-I` 下实测可生成 780B PNG。
- **截屏依赖陷阱**：同上，`-I` 下无 Pillow。已改为 PowerShell + `System.Drawing`（`common/screenshot.ps1`，先 `SetProcessDPIAware()`），实测 full 3072×1920、region、window 均通过；Pillow 仅兜底。
- **Omni 音频 URL 的 format**：不能写死 `wav`，按 URL 路径扩展名推断；本地压缩后按产物扩展名推断。
- **嵌套部署的 doctor 键**：`custom-plugins/aerialdreams-toolsets` 的 doctor 必须传带前缀的 id（裸名会报 not found），但 `plugins.enable` 仍用裸名。settings 读取已加裸名/前缀双键兜底。
- **Omni 压缩/截断实测**：23.9MB 720p 视频压到目标内并正常分析；把 `omni_max_file_mb` 调到 0.05 时触发截断分支，返回 `note="已截断到约 3.1 秒（0.05MB 上限）"`，未抽帧。
- **未覆盖**：登录态写操作（赞币藏/私信/评论/弹幕/关注/历史/收藏夹）无账号，仅按官方参数改好代码，未做真机写请求；`bili_collection` 已用 season 2046621 实测通过；`remotes`/`androiduse` 属二期未实现。
---

## 脱敏记录（公开版，2026-11-10）

本版由私有母版脱敏生成，除下列各项外与母版逐行一致：

- 称呼统一：「主人」→「用户」（全部；母版为用户对助手的称呼，无技术含义）。
- 本机工具链绝对路径（8 节实测环境：ffmpeg / platform-tools / 运行时解释器）→ 泛化为 PATH / `$HERMES_HOME/tools` 表述；版本号保留。
- 配置动作归属（8 节：`plugins.disabled` 幽灵条目清理）保留「用户本人动作」纪律，仅换称呼。
- 实测记录（含 season id、视频体积、二维码字节数）均为公开可复验数据，保留。
