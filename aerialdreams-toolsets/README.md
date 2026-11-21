# aerialdreams-toolsets

Hermes 个人工具箱：**一个插件包，五个工具套组**（32 个工具）。

| 套组 key | 工具 | 说明 |
|---|---|---|
| `bilibili` | 12 | 匿名 B 站读取：搜索 / 视频详情 / 评论 / 楼中楼 / 弹幕 / CC 字幕 / 匿名下载 / UP 资料 / UP 作品 / 热榜 / 相关推荐 / 合集 |
| `bilibili_unsafe` | 13 | 需要账号：扫码登录、高清下载、点赞投币收藏、私信、评论/弹幕写入、收藏夹、稍后再看、历史、AI 字幕、关注、空间动态 |
| `multimedia-utils` | 3 | `ffmpeg`（probe/convert/trim/mux/extract_audio/extract_frames/thumbnail/burn_subtitle/gif）、`dashscope_asr`、`dashscope_qwenomni_analysis` |
| `desktop-utils` | 2 | `clipboard` 读写、`screenshot`（full/region/window，PowerShell 截屏，无第三方依赖） |
| `android-utils` | 2 | `androidadb`（devices/shell/push/pull/logcat/screenshot/install/uninstall/props/am/pm）、`androidfb`（devices/flash/reboot/oem） |

`bilibili` / `multimedia-utils` / `desktop-utils` 默认注册；`bilibili_unsafe` 与 `android-utils` 两个高风险组**默认关闭**（schema 默认 false，代码里也有同款兜底），要用在设置里显式打开。`bilibili_unsafe` 的每个工具描述都带 `【需登录】`，写操作要求用户明确指令。

## 安装

插件已放在：

```
$HERMES_HOME/plugins/custom-plugins/aerialdreams-toolsets/
```

启用（需要用户本人执行，插件不改 config）：

```bash
hermes plugins enable aerialdreams-toolsets
```

然后**重启桌面端**（桌面 backend 在进程启动时加载插件；仅开新会话不够）。

验证：

```bash
hermes plugins doctor custom-plugins/aerialdreams-toolsets   # doctor 要带前缀 id
hermes plugins enable aerialdreams-toolsets                 # enable 用裸名
hermes tools            # 应看到 5 行：🔌 Bilibili / Bilibili_unsafe / Multimedia-utils / Desktop-utils / Android-utils
```

## 设置

`Settings → Plugins → aerialdreams-toolsets`（写进 `plugins.entries.<id>.settings`）：

| key | 默认 | 说明 |
|---|---|---|
| `group_bilibili_enabled` | true | 匿名组 |
| `group_bilibili_unsafe_enabled` | false | 账号组（高风险，默认关） |
| `group_multimedia_enabled` | true | ffmpeg/ASR/Omni |
| `group_desktop_enabled` | true | 剪贴板/截屏 |
| `group_android_enabled` | false | adb/fastboot（高风险，默认关） |
| `multimedia_api_key_env` | `DASHSCOPE_API_KEY` | 百炼 Key 的环境变量名 |
| `multimedia_base_url` | `https://dashscope.aliyuncs.com` | 百炼端点 |
| `asr_sync_model` | `qwen3-asr-flash` | 短音频同步 |
| `asr_async_model` | `qwen-audio-3.1-asr-flash-filetrans` | 长音频/时间戳 |
| `asr_wait_seconds` | 300 | 异步提交后前台最多等多久 |
| `asr_poll_interval` | 5 | 轮询间隔 |
| `asr_watch_max_seconds` | 7200 | 后台线程最长等待 |
| `omni_model` | `qwen3.8-omni-flash` | Qwen-Omni 模型 |
| `omni_max_file_mb` | 7.4 | Base64 后需 <10MB，原始文件目标 |

环境变量 `DASHSCOPE_API_KEY` 按你的环境设置即可（插件从环境变量或 `$HERMES_HOME/.env` 读取）。凭据（B 站登录）落在
`$HERMES_HOME/plugins-data/aerialdreams-toolsets/credential.json`（原子写）。落盘后尽力 `chmod 0600`：POSIX 下生效；Windows 上 `os.chmod` 只影响只读位、不等同权限隔离，实际隔离靠该文件位于当前用户目录的 NTFS ACL（同用户进程仍可读）。

## 用法示例

```text
bili_search(keyword="影视飓风", type="video")
bili_video_info(bvid="BV...")            # 拿 cid
bili_comments(bvid="BV...", size=20)     # 拿 rpid
bili_comment_replies(bvid="BV...", root_rpid=123456)
bili_download(bvid="BV...", quality=64)  # 匿名画质有上限
bili_login()                             # 返回 qrcode_path，用 MEDIA: 发给用户扫码
bili_login_check()                       # 轮询到 done 后凭据落盘
bili_download_hd(bvid="BV...", quality=80)
ffmpeg(action="probe", input="D:/x.mp4")
ffmpeg(action="trim", input="D:/x.mp4", start=10, duration=30)
dashscope_asr(action="transcribe", source="<scratch>/a.mp3")            # 同步（本地 source 限 scratch/plugin-data）
dashscope_asr(action="transcribe", source="<scratch>/long.mp3", timestamps=True)  # 异步
dashscope_asr(action="result", task_id="...")                          # 取结果
dashscope_qwenomni_analysis(source="<scratch>/v.mp4", prompt="这段视频讲了什么？")
clipboard(action="read")
screenshot(target="window")
androidadb(action="devices")
androidadb(action="shell", command="pm list packages | head")
androidfb(action="devices")
```

## 实测结论与已知边界

- **匿名 B 站**：搜索/详情/评论/楼中楼/弹幕/热榜/相关/匿名下载均实测通过；匿名下载最高通常 720P（`quality` 传 64），FLV 输出。
- **UP 作品列表**：`space/wbi/arc/search` 在无登录态下经常 `-352/-799` 风控；工具会**自动回退**到"按 UP 名搜索 + author 过滤"，返回 `source: "search_fallback"` 且结果可能不全。要精确全量列表，登录后用 `bili_dynamic` 同账号体系或自行扩展。
- **空间动态**：实测匿名必被风控，因此 `bili_dynamic` 归入 `bilibili_unsafe`（需登录 cookie + buvid3）。
- **ASR 同步**：`qwen3-asr-flash` 是专用 ASR 任务，只接受**纯音频 part**；带 `format` 字段或文本 part 会 400（已按实测修正）。同步不返回时间戳，要时间戳走 `timestamps=true` 的 filetrans 异步。
- **ASR 异步回注**：等待超时后返回 `task_id` 并起后台线程。TUI 会话里会尝试通过 `tui_gateway` 的会话表 `agent.steer()` 把结果注入原会话；**桌面端**的 gateway 不暴露 agent 对象，因此以 `dashscope_asr(action="result", task_id=...)` 主动取结果为准（提示词里已写明）。后台线程同时把全文写到 `$HERMES_HOME/cache/scratch/aerialdreams/asr/asr_<task_id>.txt`。
- **Qwen-Omni**：本地文件 ≤ 目标上限直接 base64；超限先 ffmpeg 压缩（降分辨率/码率），仍超限则截到约 9.9MB 边界并在返回里明确写"截断到约 N 秒"。**不切图片帧**（作者裁定：不抽帧）。
- **截屏**：PowerShell + `System.Drawing` 实现（`common/screenshot.ps1`），DPI-aware，无需 Pillow；Pillow 仅作兜底。
- **二维码**：内置 Nayuki `qrcodegen`（MIT）+ 纯 stdlib PNG 编码器（`common/qr.py`），所以在 `python -I` 隔离解释器里也能生成二维码，不依赖 `qrcode`/Pillow。
- **adb/fastboot**：优先 `AERIALDREAMS_ADB` / `AERIALDREAMS_FASTBOOT` 环境变量，其次 PATH。`flash` / `oem`（fastboot）与 `shell` / `am` / `pm` / `install` / `uninstall`（adb）必须显式 `confirm=true`——这些动作要么不可逆、要么等于在设备上执行任意命令，模型被注入文本忽悠时不能一键执行。
- **confirm 的诚实边界**：`confirm` 是模型自设参数，宿主不代审批——它挡的是"顺手误触"，挡不住被说服的模型；真正的闸门是 `group_android_enabled` 默认关（不打开组，这些工具根本不注册）与"仅在用户明确指令下执行"的纪律。
- **输出/输入路径边界**：`bili_download(filename)`、`ffmpeg(output)`、`screenshot(path)`、`androidadb(pull local / screenshot path)` 一律钳制在 `$HERMES_HOME/cache/scratch/aerialdreams/` 的 realpath 范围内，越界直接报错；写路径默认拒绝覆盖已存在文件（bili_download 例外：允许重写 scratch 内自己的缓存）。`dashscope_asr / dashscope_qwenomni_analysis` 的本地 `source` 必须位于 scratch 或 plugin-data 的 realpath 范围内，且过媒体扩展名/MIME 与大小硬校验（base64 路径 10MB 上限），`~/.ssh`、`.env` 这类任意路径不会被读入 base64 或上传；http(s) URL 与 data URI（≤10MB 校验）行为保留。ffmpeg 的 `input/video/audio/subtitle` 保留任意本地路径工作流，但强制媒体扩展名/MIME + 2GB 大小校验。B 站流媒体下载 URL 走 HTTPS + host 白名单（bilibili.com / bilivideo.com(.cn) / hdslb.com），且不跟随重定向。
- **不可信文本围栏**：B 站的标题、简介、UP 签名、评论、弹幕、字幕一律包进 ```` ```untrusted ```` 围栏并在首个字段前加一行固定警示（`[外部内容·不可信]`），内容里自带的连续反引号会被打断，防止"用代码块越狱"。字段层级与键名不变，`bvid`/`cid`/`author`/用户名等标识字段不围栏（下游过滤要能直接比较）。风险承认：UP 名/用户名仍是不围栏的窄通道，属刻意取舍。
- **截屏**：PowerShell + `System.Drawing` 走主路径；Pillow 兜底改在**一次性子进程**里跑并设 Per-Monitor-V2 DPI——以前是父进程直接调 `SetProcessDpiAwareness`，那会把整个 Hermes 进程的 DPI 模式永久改掉，影响同进程里所有 GUI 代码。
- **ffmpeg**：需要 PATH 上有 `ffmpeg`/`ffprobe`（以本机 PATH 为准）。
- **ASR 任务表**：内存任务表上限 64 条、TTL 7 天（写时与取值时都剪枝），两处等待轮询都改成指数退避（最高 30s 一次）——原来是无限增长 + 固定间隔轮询。

## 目录

```
__init__.py          register(ctx)：读设置 → 逐组注册 → 注入系统提示段
plugin.yaml          manifest + provides_tools + config_schema
common/
  config.py          设置读取、路径管理
  util.py            HTTP/WBI/JSON 信封
  auth.py            B 站凭据读写
  ffmpeg.py          ffmpeg/ffprobe 子进程封装
  qr.py              QR PNG（stdlib）
  qrcodegen.py       Nayuki 编码器（MIT）
  screenshot.ps1     PowerShell 截屏
groups/
  bilibili.py        匿名组 12 工具
  bilibili_unsafe.py 账号组 13 工具
  multimedia.py      ffmpeg + ASR + Omni
  desktop.py         剪贴板 + 截屏
  android.py         adb + fastboot
```

## 验证安装

```bash
hermes tools            # 应看到 5 行：🔌 Bilibili / Bilibili_unsafe / Multimedia-utils / Desktop-utils / Android-utils
hermes plugins doctor custom-plugins/aerialdreams-toolsets   # 子目录放置时 doctor 要带前缀 id
```

`hermes plugins validate <源码目录>` 当前 15 项全过、**0 WARN**（会话键与 steer 只走 `gateway.session_context` / `tui_gateway`，不再引用核心私有函数）。