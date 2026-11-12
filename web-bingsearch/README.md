# web-bingsearch — Bing HTML 直采搜索后端（v1.3.1）

把 **Microsoft Bing 的公开搜索结果页**（`cn.bing.com/search`，纯 HTML）直接转化为 Hermes 的原生搜索后端：无 API Key、无第三方聚合、零账号——发请求拿结果，纯白嫖路线。

注册为 web search provider（`ctx.register_web_search_provider`），同时支持 **search** 和 **extract** 两个原生工具面。

> 诚实声明：Bing 对这个免登录入口有反爬蜜罐（内容降级、频率墙、验证码），本插件内置了缓解手段，但**质量不如官方 API 类后端**。适合作为零成本兜底，不适合当主力。主力推荐隔壁 web-dashscope。

## 版本沿革

- **v1.0**（2026-08-31）：RSS 通道 + 三档截词阶梯。RSS 蜜罐静默降级（垃圾长得像真结果），已弃。
- **v1.2**（2026-10-02）：纯 HTML 通道，不截词。源码随开发目录删除丢失，架构从记录还原。
- **v1.3**（2026-11-12）：还原 v1.2 架构 + 三目标升级（客户端选型/伪装层/跨变体解析器）+ 子进程隔离 + settings 开关。v1.0 的 RSS 函数全部保留在源码但不接线。
- **v1.3.1**（2026-11-12 晚，本版）：质量实测驱动的蜜罐回退大修——CJK 边界分词+品牌型号合并（RTX 5090→RTX5090）、纯中文长句前3字兜底候选、高置信度垃圾判定器（标识符全灭/覆盖率<1/3）、选词逆长度评分。回退默认改为开。8 查询出厂实测 48/68 相关、6/8 真结果（v1.3 默认仅 12/70）。

## 安装与启用（依赖走 Hermes PM 新格式）

1. 目录放入 `$HERMES_HOME/plugins/custom-plugins/web-bingsearch/`（可嵌一级分类目录）。
2. **依赖声明在 `pyproject.toml`**（PM 新格式：`[project].dependencies = ["primp>=2.0.1"]`，uv workspace 成员，metadata-only 不打包插件本身）。`hermes pm install` 会把 primp 装进 Hermes 环境；首次 enable 时也有依赖同意询问。**不需要手动 pip install，也不往主环境/Hermes venv 手动装东西。**
   - 本机 pypi.org 直连超时的话，给 PM 同步设镜像：`UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`（PM 会把 pip.conf/UV_INDEX_URL 桥接给 uv）。
3. `config.yaml` → `plugins.enabled` 加一行。三个容易混淆的名字，各司其职：
   - **目录名** `web-bingsearch`：决定 `plugins.enabled` 里的 key（嵌套放置时是 `custom-plugins/web-bingsearch`）；
   - **manifest name** `web-bing-search`：`plugin.yaml` 里的 `name:`，gate 同时接受它；
   - **provider id** `bing-search`：`__init__.py` 里 `name` property 返回的值，选后端时**只用这个**。
4. `config.yaml` → `web.search_backend: bing-search`（正文提取同理 `web.extract_backend: bing-search`）。
5. 重启 Hermes。

无需任何环境变量或 Key，`is_available()` 只检查 `primp` 可导入（不发网络请求）。

## v1.3 核心设计（2026-11-12 实测裁定，数据在 PROMPT.md）

- **客户端 primp，钉死桌面版本指纹池**（chrome_152/chrome_146/safari 轮换）。primp 2.0.1 的裸别名 `chrome`/`edge` 会随机解析到 iPhone 移动版 → Bing 返回移动页（5 条、无 h2），**禁用裸别名**。curl_cffi 本网络冷连接 19-25s 实测太慢，未纳入。
- **纯 HTML 通道，不截词**：原句完整直发，锁中文（setlang=zh-CN&ensearch=0）。RSS 蜜罐已弃。
- **多级容错解析器**：Bing 做 A/B 动态渲染，同一请求返回经典/富/移动多种 DOM。按 `b_algo` 切块，块内多级锚点（桌面 `<h2><a>` → 移动 `<a><h2>` → `a.tilk` 兜底），跨 12 个变体样本验证全过。全页 h2 不可靠（混相关搜索/图片导航）。
- **子进程隔离（根治 GIL 冻结）**：primp 可能持 GIL 卡在 native 代码，线程超时永不触发、整个 Hermes 冻死。搜索跑一次性子进程，父进程硬墙钟超时 terminate→kill（官方 ddgs 同款模板）。GIL 钩子测试证明 4s 超时击杀成立。
- **伪装层**：伪装头（MUID/随机 Accept-Language/Sec-Fetch）防御性保留——实测无质量增益，只降封禁概率。**首页预热默认关**——实测触发富页面变体（体积 3-4 倍、h2 锚点几乎全灭、结果数不增），推翻 v1.1「预热有益」。
- **蜜罐仍在**：2026-11-12 实测 HTML 通道长查询仍会返回「含查询词但全是泛泛热门页」的第二副面孔（如 fix 词典页）。垃圾检测器验货；可选截词回退（默认关，见下表）。

## Settings 开关（Settings→Plugins 面板，plugins.entries.web-bing-search.settings）

缺键由 register 时逐键自动补齐（走官方 writer）；你显式存的 ""/false/0 不会被覆盖。

| 开关 | 默认 | 改什么 | 实测依据 | 会引发什么 |
|---|---|---|---|---|
| `stealth_headers` | true | 发请求带 MUID/随机 Accept-Language/Sec-Fetch 全套 | A/B 实测与裸发质量相同（10/10） | 关掉画像更裸，封禁/限速概率可能上升；不提质量 |
| `warm_homepage` | **false** | 先 GET 首页收 cookie 再搜索 | 预热触发富变体：体积 190-460KB（3-4倍）、h2 锚点几乎全灭、结果数不增 | 开了纯浪费 token+解析锚点失效；仅硬封禁换画像时手动开 |
| `subprocess_isolation` | true | 搜索跑一次性子进程+硬超时击杀 | GIL 钩子测试：4s 超时、子进程确认死亡 | 关掉=进程内直跑，primp 卡死会冻死整个 Hermes（不建议） |
| `result_cache` | **false** | 同查询短 TTL 缓存 | 二次调用 0ms（缓存命中） | 开了 TTL 内拿稍旧结果；降频率墙压力 |
| `cache_ttl_secs` | 300 | 缓存有效期（秒） | — | 仅 result_cache 开时生效 |
| `search_timeout_secs` | 45 | 子进程墙钟上限（含退避重试） | 冷连接 5s+退避最坏 ~30s | 太小会误杀慢查询；超时 terminate→kill 并报错 |
| `honeypot_truncation_fallback` | **true**（v1.3.1 起） | 判垃圾时降档：截3词→最值钱单词→纯中文长句前3字 | 三轮实测：关=12/70 相关，开=53/78；蜜罐不止打长查询，3-4词也给首词homepage垃圾 | 开了长查询质量稳，代价是垃圾查询多几秒+查询语义被削；显式关=不截词原教旨，长查询可能拿泛泛结果 |

## extract 说明

按 URL 抓原始 HTML 直接回 `raw_content`（伪装版：桌面指纹+伪装头+Bing Referer），不做正文解析。模型通常能自己啃 HTML，但 token 消耗比正经阅读器后端大。单个 URL 失败只标记该项 `error`，不整批失败。**返回纯列表**（包 success/data 外壳会炸掉 web_extract 消费端）。

## 合法合规提示

这是对公开搜索端点的模拟浏览器访问，请自行评估是否符合你的使用场景与 Bing 的服务条款；被限流或封 IP 属预期内风险。验证码/JS Challenge 场景本插件不过（回真浏览器，非本插件职责）。
