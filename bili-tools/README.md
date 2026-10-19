# bili-tools — Bilibili Agent 工具集插件

为 Hermes Agent 打造的哔哩哔哩（Bilibili）全功能工具集插件。默认采用**纯 Python 标准库 + 公开匿名接口**实现，**零三方依赖、免 Cookie、免登录**即可开箱即用；同时支持通过第三方库扩展高清画质、AI 字幕与用户账号互动操作。

---

## 特性亮点

1. **零依赖与开箱即用**：默认 `curl` 实现路径仅使用 Python 标准库（`urllib`、`xml.etree` 等），无需额外安装任何 pip 包即可完成视频搜索、详情解析、弹幕抓取、字幕提取与视频流下载。
2. **双实现引擎（Dual Implementation）**：
   - `curl`（默认）：轻量、匿名、公开接口、无外部依赖。
   - `bilibili_api`（可选）：依赖 `bilibili_api-python` 库；在配置登录凭据后可拉取更高画质（1080P+ / 4K）、AI 智能生成字幕及完整弹幕池。
3. **安全门控（Danger Feature Gate）**：所有涉及用户账号写入（发评论、发弹幕）及扫码登录的高危工具被严格隔离在 `bili-tools-danger` 工具集中，仅当会话环境变量显式指定 `HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1` 时才会被注册与暴露，防止模型误触发账号违规风险。
4. **灵活目录与缓存管理**：下载的视频片段和临时二维码自动对齐 `$HERMES_HOME/cache/scratch/bili`，登录凭据持久化保存在 `$HERMES_HOME/plugins-data/bili-tools/`，不污染工作目录。

---

## 工具清单

### 只读工具集（默认启用）

| 工具名称 | 关键参数 | 功能说明 |
| :--- | :--- | :--- |
| `bili_search` | `keyword`, `page`, `order`, `implementation` | 关键字检索视频，返回标题、BV/AV号、作者、播放量与时长。 |
| `bili_video_info` | `bvid` / `aid`, `implementation` | 获取视频详细元数据（分P信息、cid、点赞/投币/收藏数等），为多P视频后续处理前置依赖。 |
| `bili_comments` | `bvid` / `aid`, `sort`, `page`, `size`, `implementation` | 读取视频顶层热门/最新评论及 rpid，便于情绪洞察与评论分析。 |
| `bili_danmaku` | `bvid` / `aid`, `limit`, `implementation` | 解析视频分P弹幕流（包含时间戳偏移秒数与文本）。 |
| `bili_subtitle` | `bvid` / `aid`, `implementation` | 提取 CC/AI 分段字幕（带精确时间轴；匿名访问下若无公开 CC 字幕返回空为正常现象）。 |
| `bili_download` | `bvid` / `aid`, `quality`, `page_no`, `force`, `filename`, `implementation` | 下载指定分P视频至本地缓存目录，返回绝对路径供本地音视频模型或视觉分析调用。 |

### 账号与写入工具集（门控保护）

> **注意**：需在启动环境设置 `HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1` 方可激活。

| 工具名称 | 关键参数 | 功能说明 |
| :--- | :--- | :--- |
| `bili_login` | 无 | 生成移动端扫码登录二维码图片（返回本地 png 路径供扫码），启动鉴权流程。 |
| `bili_login_check` | 无 | 轮询扫码登录确认状态；登录成功后自动加密持久化保存 Session 凭据。 |
| `bili_comment_post` | `bvid`, `text`, `reply_to_rpid`, `root_rpid` | 以当前登录用户身份对指定视频发表评论或回复二级评论。 |
| `bili_danmaku_post` | `bvid`, `text`, `time_offset_sec` | 以当前登录用户身份在指定播放时间点发射弹幕。 |

---

## 安装与启用

本插件支持直接平铺在插件根目录，也支持放在自定义分类子目录（例如 `custom-plugins`）内：

### 1. 放置插件目录（任选其一）
- **平铺安装**：
  将插件放入 `$HERMES_HOME/plugins/bili-tools`
- **分类安装**：
  将插件放入 `$HERMES_HOME/plugins/custom-plugins/bili-tools`

### 2. 在配置中启用
在 Hermes 配置文件 `config.yaml` 的 `plugins.enabled` 列表里加入本插件：
```yaml
plugins:
  enabled:
    - bili-tools   # 若放置在 custom-plugins 下，写 bili-tools 或 custom-plugins/bili-tools 均可
```
或直接通过命令行启用：
```bash
hermes plugins enable bili-tools
```

### 3. 重启生效
重启 Hermes Agent（桌面端需退出重开）使插件加载生效。

---

## 可选增强依赖

如需使用 `bilibili_api` 高清画质与 AI 字幕模式，可在当前 Python 环境安装：
```bash
pip install bilibili-api-python
```
若不安装，所有工具将平滑保持在默认的匿名 `curl` 模式下稳定作业。
