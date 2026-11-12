# web-bingsearch 需求书（公开版）

版本：v0.4（三目标调研 + v1.3 实现 + v1.3.1 质量实测大修）
日期：2026-11-12
状态：**v1.3.1 完成，离线+真网质量实测+e2e回归+官方 validate 全过；部署+改 config 由作者执行**
维护规矩：本文件是项目需求书（PROMPT.md）。实现阶段踩的坑追加到文末第 12 节。

---

## 0. 三目标与基线警告

本次升级三目标：
- **目标 1**：客户端选型——primp vs curl_cffi vs 其他轻量 TLS 伪装工具，哪个好。
- **目标 2**：HTTP 伪装层——哪些手法真有用、哪些是 placebo 甚至有害。
- **目标 3**：硬挖 HTML 前端——把 Bing 结果页 DOM 挖透，做出跨变体稳的解析器。

**基线警告**：升级起点是 **v1.0**（RSS 三档阶梯）。项目实际已演进到 **v1.2**（纯 HTML），但 v1.1/v1.2 源码随早期开发目录删除而丢失、从未部署。升级站在 v1.2 设计肩膀上，别退回 v1.0 的 RSS 老路。三个名字：目录名 `web-bingsearch` 管开关、manifest 名 `web-bing-search` 也能当开关、选后端只认 provider id `bing-search`。

**环境纪律（实测裁定）**：
- 依赖调研与测试一律在**独立 venv** 里做，不往主 Python 环境/Hermes 运行环境装东西。本次调研 venv 装了 primp 2.0.1 + curl_cffi 0.17.0。
- 主环境是否已有 primp 以实测为准，别信旧笔记（本次主环境只有 pip，primp 早没了；lxml 在但搜索解析不用它）。
- pypi.org 直连不稳时走镜像（如清华 `https://pypi.tuna.tsinghua.edu.cn/simple`）；但镜像的 JSON 元数据可能滞后（primp 曾显示 2024 年的 0.5.5），**版本号以实际 pip 装到的为准**。

---

## 1. 一句话定位

把 Bing 公开搜索结果用 TLS 指纹伪装 HTTP 客户端直采，封装成 Hermes 原生 web search provider（`bing-search`），接管 `web_search` 与 `web_extract`。零 Key、零账号、零第三方聚合，纯白嫖兜底（主力是付费 API 后端）。

---

## 2. 版本沿革

| 版本 | 日期 | 通道 | 关键动作 | 现存 |
|---|---|---|---|---|
| v1.0 | 2026-08-31 | RSS 优先 | 三档截词阶梯 + 指纹轮换 | 磁盘现存（升级基线） |
| v1.1 | 2026-10-02 | RSS+伪装层 | 移植 EnhancedBing HTTP 伪装 | 源码丢失，设计在记录 |
| v1.2 | 2026-10-02 | 纯 HTML | RSS 退役，b_algo 解析+不截词退避 | 源码丢失，设计在记录 |

v1.0→v1.2 动因：RSS 蜜罐静默降级（≥4 词甚至 2 词返回长得像结果的垃圾），伪装治不了；HTML 无词数限制、失败诚实（0 条=显式）。

---

## 3. 目标 1：客户端选型（实测裁定）

### 3.1 候选盘点
| 库 | 内核 | 本次实测版本 | 裁定 |
|---|---|---|---|
| **primp** | Rust（rquest） | 2.0.1 | **首选**：快、轻、ddgs 官方背书 |
| **curl_cffi** | curl-impersonate | 0.17.0 | 备选：UA 稳，但本测试网络冷连接极慢 |
| tls-client | Go 绑定 | 1.0.1（镜像） | **淘汰**：最后推送 2024-07，指纹过时、维护停滞 |
| httpx / niquests | 纯 Python | — | **淘汰**：无 TLS 指纹伪装，JA4 露馅、停留 HTTP/1.1（2026-08-28 笔记结论） |
| rnet / scrapling | Rust | 镜像 404 | 未验证（镜像无包），不纳入 |

### 3.2 实测数据（venv，Python 3.14.8，cn.bing.com，2026-11-12）
测试条件：4 查询（短英/KB号/6词长句/中文）× 2 轮，预热 client 复用，每枪间隔 1.2s；内存用 tasklist 按 PID 读 WorkingSet（ctypes 结构体法曾返回 0，已弃）。

| 指标 | primp 2.0.1 | curl_cffi 0.17.0 |
|---|---|---|
| import 耗时 | 0.056s | 0.124s |
| 内存增量 | +10.5MB（13.6→24.1） | +9.1MB（24.1→33.2） |
| 预热后单枪 | 0.09–0.13s（均 0.11s） | 0.14–0.17s（均 0.15s） |
| 冷连接单枪 | 0.25–10s 波动 | **19–25s，第 4 次 25s 超时** |
| JA4 | t13d1516h2_8daaf6152771_d8a2da3f94cd | t13d1517h2_8daaf6152771_cb7bf5808d99 |
| 指纹命名 | `chrome_152`（带下划线） | `chrome152`（无下划线） |

### 3.3 关键陷阱：primp 2.0.1 的 `impersonate="chrome"` 随机解析
- 裸别名 `chrome`/`edge` 会**随机解析到不同平台**，本次实测多次落到 **iPhone 移动版 Chrome（CriOS）**→ Bing 返回**移动版页面**（5 条结果、标题用 `tptt`/`tilk` class 而非 `<h2><a>`）。
- **钉死版本号 `chrome_152` 后不再掉移动版**（4/4 桌面、10 条），但 UA 的**平台部分仍随机**（Linux/Windows/Mac 轮换）——版本号锁住了 Chrome 主版本与移动/桌面形态，平台子串没锁。
- **v1.0 代码里 `impersonate="chrome"` 的写法在 2.0.1 下会随机拿移动版页面，解析器直接失效**——这是升级必须修的头号坑。
- curl_cffi 的 `chrome` 别名稳定解析到 Win Chrome/152，UA 不随机。

### 3.4 目标 1 裁定
- **主通道 primp，钉死桌面版本指纹**（`chrome_152` 或 `chrome_144`/`chrome_146`，实测都给桌面 10 条）。理由：预热后最快、内存轻、ddgs 官方同款引擎、本测试网络冷连接远比 curl_cffi 快。
- **curl_cffi 留作可选兜底**（UA 更稳），但本测试网络冷连接 19-25s 是硬伤，不作主力。
- 指纹池轮换（v1.0 的 chrome/edge/safari）要改成**桌面版本号池**（如 chrome_152/chrome_146/safari），**禁用裸别名**，避免随机掉移动版。
- 待裁决：是否要锁 UA 平台子串（primp 能否自定义 UA 覆盖，需实现阶段验证；若不能，接受平台轮换——实测平台轮换不影响结果数）。

---

## 4. 目标 2：HTTP 伪装层（A/B 实测裁定）

### 4.1 实验设计
钉死 `chrome_152`，三臂各 5 查询 × 2 轮 = 10 枪，测有结果率/h2 锚点数/页面体积/耗时：
- **A 裸发**：只钉指纹，不加任何头。
- **B +伪装头**：伪造 MUID cookie + 随机 Accept-Language（3 变体）+ Sec-Fetch 全套 + DNT + Upgrade-Insecure-Requests（**故意不设 UA/Sec-Ch-Ua**，避免与 impersonate 的 TLS 指纹矛盾——v1.1 教训）。
- **C B+首页预热**：先 GET cn.bing.com 首页收会话 cookie（实测收 8 个），再同 client 带 Referer 发搜索。

### 4.2 实测结果
| 臂 | 有结果 | h2 锚点总数 | 平均耗时 | 页面体积 |
|---|---|---|---|---|
| A 裸发 | 10/10 | 99 | 0.12s | ~104KB 经典 |
| B +伪装头 | 10/10 | 100 | 0.11s | ~104KB 经典 |
| C +预热 | 10/10 | **7（几乎全灭）** | 0.09s | **190–460KB 富变体（3-4 倍）** |

### 4.3 目标 2 裁定（含一个反直觉推翻）
- **伪装头（B）无害但也无质量增益**：与裸发几乎一样。印证 v1.1 结论「伪装治不了蜜罐」——它的价值是**降低封禁/限速概率、画像更像真人**，不是提质量。可保留作防御性默认，但别指望它救长查询。
- **首页预热（C）有害，推翻 v1.1「预热有益」**：预热拿到的会话 cookie 让 Bing 返回**富页面变体**——结果数没多（仍 10 条上限）、h2 锚点几乎全灭、页面体积暴涨 3-4 倍纯浪费 token。**升级应默认关闭首页预热**，或仅在硬封禁恢复时用作换画像手段。
- **真正的质量杠杆是「钉死桌面指纹」（目标 1），不是头伪装**。裸发+钉指纹已 10/10 干净。
- 频率墙软降级（200+容器在+b_algo≈0）与硬封禁（验证码）仍需结构探测 + 退避换指纹（继承 v1.2，见第 6 节）。本次 A/B 未撞墙（间隔 0.8-1.2s、量小），退避逻辑沿用 v1.2 实测参数（等 5s+换指纹+新 MUID→满血）。

---

## 5. 目标 3：硬挖 HTML 前端（跨变体解析器）

### 5.1 核心发现：Bing 做 A/B 动态渲染，同一请求返回多种 DOM 变体
本次抓到的变体（样本已存档备查）：
| 变体 | 触发条件 | 体积 | 结果数 | 标题结构 |
|---|---|---|---|---|
| 经典桌面 | 钉桌面指纹裸发 | ~104KB | 10 | `<h2><a href>` 标准 |
| 富页面 | 首页预热 cookie | 190–460KB | 10 | h2 时有时无，结构不稳 |
| 移动版 | 裸别名随机到 iPhone | ~64KB | 5 | `tptt`/`tilk` class，无 h2>a |

**v1.2 钉死单一 h2 结构的正则会在富/移动变体上失效**——这是目标 3 要根治的。

### 5.2 锚点可靠性实测
- **全页 h2 计数会超过结果数**：游戏名查询 algo=10 但全页 h2a=14，多出的是相关搜索/图片导航的 h2。**全页 h2 不可靠**。
- **`a.tilk` 稳定等于结果数**（10/10），是比 h2 更可靠的结果标题锚点。
- `b_algo` 块数 = 真实结果数（桌面 10、移动 5），**按 b_algo 切块再块内取锚点**最稳。

### 5.3 多级容错解析器（已写探针验证，EnhancedBing 容错链思路）
策略：`按 <li class="b_algo" 切块` → 块内多级标题锚点（`h2>a` → `a.tilk`（href 在前/在后两种）→ 块内首个外链兜底）→ 摘要（`b_lineclamp` → `b_caption>p` → 首个 `<p>`）→ 滤 bing.com 站内链 + 洗面包屑。

**跨 12 个已存变体样本验证**：
- 桌面变体（经典/富/cffi/primp 钉指纹）：**10/10 提取，标题+摘要齐全**。
- 移动变体：**5/5 提取**，但标题混入面包屑（`microsoft.comhttps://...`）需洗。
- 结论：**多级容错解析器跨变体稳**，可作为 v1.3 解析核心。

### 5.4 目标 3 待办（实现阶段）
- 移动版标题面包屑清洗：剥 `域名+URL›路径` 前缀（v1.2 已有「翻译此结果」+面包屑清洗逻辑，扩展到移动变体）。
- 标题站名后缀清洗（继承 v1.2 正则，后缀 ≤16 字符才砍）。
- 摘要去重/截断策略（富变体摘要可能更长）。
- 解析器对「软降级阉割页」（b_algo≈0）返回显式空，不掺垃圾。

---

## 6. 架构与契约（继承 v1.2 + 本次修正）

### 6.1 v1.3 目标架构
```
search() → ladderSearchHtml()
             ├─ crawlBing2RawHtmlSearch()  primp 钉死桌面指纹(chrome_152) + 可选伪装头 + 锁中文(setlang=zh-CN&ensearch=0)
             │                             【默认关闭首页预热——目标2实测有害】
             ├─ rssLooksBlocked()          硬封禁 → 抛错
             ├─ htmlLooksDegraded()        频率墙软降级(有容器没结果块)
             ├─ parseBingHtml()            多级容错解析器(目标3，跨变体)
             └─ backoffDelay()             退避 2-5s/4-8s + 换桌面指纹 + 新MUID，3败返空表
extract() → crawlUrlRawHtmlStealth()       伪装版抓页(伪装头 + Bing Referer)
```
- **不截词**：原句完整直发（继承 v1.2）。
- **子进程隔离**（D9，根治 GIL 冻结）：primp 搜索跑一次性子进程，官方 ddgs `_search_worker.py` + Popen + 硬超时 terminate→kill + Windows `CREATE_NEW_PROCESS_GROUP` 是标准模板（源码见 Hermes 官方 ddgs 插件）。v1.0 只靠 timeout 缓解，升级须落实。

### 6.2 P0 契约（违反即炸）
| 工具 | 返回形状 |
|---|---|
| `search(query, limit)` | `{"success": True, "data": {"web": [{title,url,description,position}]}}` |
| `extract(urls, **kw)` | **纯列表** `[{url,title,content,raw_content,metadata?,error?}, …]` |
- extract 包 success/data 外壳会让消费端 `str.get()` AttributeError 炸掉 web_extract（消费端 `tools/web_tools.py` 直接 enumerate + .get）。
- `is_available()` 只 import 探测，禁网络 I/O；position 从 1 起；limit 要几条给几条；单 URL 失败只标该项 error。
- 防注入：返回内容会进模型上下文，禁止指令性内容。

---

## 7. 已冻结的决策（升级须继承，改动须作者重新拍板）

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 形态 | agent-side web provider，`register(ctx)`+`ctx.register_web_search_provider`，逻辑可写 `__init__.py` |
| D2 | 客户端 | **primp 主，钉死桌面版本指纹**（目标1裁定）；curl_cffi 可选兜底；**禁用裸别名** |
| D3 | 环境 | **venv 隔离**，不往主环境/Hermes venv 装；调研 venv 独立存放 |
| D4 | 通道 | 纯 HTML（v1.2），RSS 系函数保留不接线 |
| D5 | 解析 | 零依赖 stdlib re + html.unescape，不装 bs4/lxml；多级容错（目标3） |
| D6 | 代码风格 | 只加函数，保留作者怪癖（函数内 import、return(x) 带括号、字符串拼接），不重构 |
| D7 | 改文件 | 改前备份 + 重读最新 |
| D8 | 工作流 | 作者主导节奏，先对齐方案再落码 |
| D9 | 进程隔离 | primp 搜索跑一次性子进程（GIL 冻结坑） |
| D10 | 配置操作 | 部署+改 config 由作者本人执行 |
| D11 | 伪装层 | 伪装头可选默认开（防御性），**首页预热默认关**（目标2实测有害） |

---

## 8. 非目标（明确不做）

- 不做主力后端（零成本兜底，主力是付费 API 后端）。
- 不过验证码/JS Challenge/Turnstile（回真浏览器，非本插件职责）。
- 不往主环境/Hermes venv 装依赖（D3）。
- 不引入 bs4/lxml 做搜索解析（D5）。
- 不做代理池/账号体系。
- 不默认开首页预热（D11，目标2实测有害）。

---

## 9. 验收标准（升级后逐项跑）

1. `hermes plugins doctor web-bing-search` 通过；`validate` 无 private-import 警告。
2. 真发 search：短查询/4词KB号/6词长句/中文各一次，success 且相关（基线 6 词长句 5/5）。
3. **钉死桌面指纹后不再随机掉移动版**（连续 10 枪 UA 均非 Mobile，结果数稳定 10）。
4. 多级解析器在经典/富/移动变体上都能提取（桌面 10、移动 5，标题+摘要齐全）。
5. 连发 5 枪撞频率墙：软降级被 htmlLooksDegraded 探测并退避恢复，不掺垃圾。
6. extract 返回纯列表，raw_content 真实，单 URL 失败不整批炸。
7. 子进程隔离生效：primp 卡死时父进程超时 terminate，Hermes 不冻死。
8. 重启后 provider 仍注册，is_available 不发网络请求。
9. 若加 settings 开关：缺键按默认补齐、显式 ""/false/0 不覆盖、走官方 writer。
10. 部署+改 config 由作者执行后 `web.search_backend: bing-search` 真生效（bing 定位是已启用的零成本兜底，非现役主力）。

---

## 10. 升级前实例核查清单（只读）

动手升级前，逐项核实自己的实例（以下为核查方法，具体值因机器而异）：

- **基线版本**：确认部署目录与开发拷贝是否逐字节一致、是哪个版本；读 plugin.yaml 的 `name`/`version`/`kind`/`provides_web_providers`。
- **config 现状**：`web.backend` 当前是谁、`keyless_fallback` 开关；`plugins.enabled` 是否含本插件、`entries` 下有无 `allow_tool_override` 等设置。分清「已启用」与「现役」。
- **Python 环境**：主环境版本与 site-packages 实际清单（primp 在不在，别信旧笔记）；调研 venv 位置与已装库版本。
- **历史源码下落**：早期开发目录是否还在；v1.1/v1.2 若丢失，从会话记录/归档笔记还原设计。
- **参考实现留档**：EnhancedBing 等参考源码是否留档可查。
- **实测产物归档**：探针脚本、bench/A-B 结果 JSON、HTML 变体样本集中存档，供解析器回归回放。

---

## 11. 待裁决的开放问题

| 编号 | 问题 | 建议 |
|---|---|---|
| 11.1 | 升级基线先还原 v1.2 再往上加？ | 是，别在 v1.0 RSS 老路加料 |
| 11.2 | 客户端双轨（primp 主+curl_cffi 兜底）还是单 primp？ | 倾向单 primp 起步（本测试网络 curl_cffi 冷连接太慢），双轨留接口 |
| 11.3 | 要不要锁 UA 平台子串？ | 先接受平台轮换（实测不影响结果数），实现阶段验证 primp 能否自定义 UA |
| 11.4 | 伪装头默认开还是关？ | 默认开（防御性、无害），但 README 写明它不提质量 |
| 11.5 | extract 要不要正文解析、零依赖还是引库？ | 零依赖正则先行，重依赖走 pm.ensure_environment 隔离 side-env |
| 11.6 | 是否加 settings 开关（按 DIY 插件规范：逐功能独立 bool、修改类默认 false）？ | 加：客户端选择/伪装头/预热/子进程隔离各一个 bool |

---

## 12. 坑位记录（实现阶段追加，格式：现象 / 根因 / 规避）

- **primp 2.0.1 裸别名随机掉移动版** / `impersonate="chrome"` 随机解析到 iPhone CriOS，Bing 返回移动页（5条、无h2>a） / 钉死桌面版本号 chrome_152，禁用裸别名，指纹池用桌面版本号。
- **首页预热触发富变体** / 预热会话 cookie 让 Bing 返回 190-460KB 富页面，h2 锚点几乎全灭、token 暴涨 / 默认关预热（推翻 v1.1「预热有益」）。
- **全页 h2 混入导航** / 相关搜索/图片导航也是 h2，计数>结果数 / 按 b_algo 切块、块内取 a.tilk（稳定=结果数）。
- **Bing A/B 动态渲染** / 同一请求返回经典/富/移动多种 DOM / 多级容错解析器，不钉死单一结构。
- **curl_cffi 测试网络冷连接极慢** / 19-25s 甚至超时 / 不作主力，primp 主通道。
- **curl_cffi 指纹命名无下划线** / chrome152 非 chrome_152 / 两库命名分别处理。
- **RSS 蜜罐静默降级** / ≥4词甚至2词返回像结果的垃圾 / 纯 HTML（v1.2）。
- **伪装治不了蜜罐** / 蜜罐是服务端内容降级 / 伪装只降封禁概率。
- **HTML 频率墙软降级** / 200+容器在+b_algo≈0，关键词表抓不到 / 结构探测+等5s+换指纹+新MUID。
- **challenge 误报** / PoWChallengeSolver.br.js 是 JS 资源名 / captcha/verify 同现才判验证码。
- **primp GIL 冻结** / native 持 GIL 阻塞，线程超时不触发 / 一次性子进程隔离+terminate→kill。
- **extract 包外壳炸** / 消费端直接 .get / 必须返回纯列表。
- **标题服务端截断** / Bing 返回前就截（HTML~63/RSS~72） / 解析器救不回。
- **移动版标题混面包屑** / `microsoft.comhttps://...` / 剥域名+URL›路径前缀。
- **镜像元数据滞后** / 清华 JSON 显示 primp 0.5.5（2024） / 版本以实际 pip 装到为准（2.0.1）。
- **ctypes 内存结构体返回0** / PROCESS_MEMORY_COUNTERS 字段对齐错 / 用 tasklist 按 PID 读 WorkingSet。
- **URL「残缺」错觉** / demo 打印 url[:80] 截断显示 / 诊断先全量打印不截断。
- **header 顺序指纹** / 自定义 headers 覆盖顺序 / 故意不设 UA/Sec-Ch-Ua，避免与 impersonate 矛盾。

### v1.3 实现阶段新增（2026-11-12）

- **移动版标题锚点方向相反** / 移动页标题是 `<a href><h2>`（b_algoheader 内），桌面是 `<h2><a>`；且移动版 a.tilk 是面包屑域名锚点（aria-label=域名）不是标题 / 容错链加移动方向 a>h2 且排在 tilk 前；只匹配桌面方向会取到域名当标题。
- **HTML 通道蜜罐仍在（推翻 v1.2 乐观结论）** / 2026-11-12 实测长查询返回含查询词的泛泛热门页（fix 词典页/Python 官网），与域名/语言参数无关；v1.2 时代「6词 10/10 相关」在当前 Bing 不成立 / 垃圾检测器验货 + 可选截词回退（honeypot_truncation_fallback）；单词档实测 10/10 相关，3词档仍垃圾。
- **钉死版本号不保证 100% 桌面** / chrome_152 固定后仍偶发移动页（algo=5） / 解析器必须跨变体容错（已做）；结果数 5 vs 10 是变体差异不是 bug。
- **pyproject 注释含魔法字符串** / PM 用子串匹配 "GENERATED by pm" 判生成物，注释里提到这个短语也会让整个 pyproject 被跳过（依赖读成 none） / 注释里别写出完整标记短语；用 read_python_declaration 验证而不是只看文件存在。
- **PM 新格式依赖声明** / 作者手写 pyproject.toml [project].dependencies 优先于 manifest python_dependencies；带 [project] 表=uv workspace 成员，name+version 必填，不写 build-system=metadata-only 只装依赖 / hermes-lcm 同款；validate 的 "python dependencies" 行会显示 "N requirements from pyproject"。
- **tools.interrupt 不在公开白名单** / private-import 警告项（官方 ddgs 也用它） / try/except 守卫降级，中断探测失败不拦搜索；警告可接受。
- **venv 测试需 PYTHONPATH 指 Hermes 源码** / agent.web_search_provider 基类在 Hermes 源码树，隔离 venv 没有 / 测试时 PYTHONPATH 指向 Hermes 源码 workspace；PM 读取函数还需 ruamel（用 Hermes 自己的 venv 跑）。

### v1.3.1 质量实测阶段新增（2026-11-12 晚）

- **蜜罐不止打长查询** / 3-4 词查询也返回首词 homepage 垃圾（python primp→python.org，obsidian dataview→obsidian.md，英伟达RTX5090→nvidia 首页）；v1.3 默认配置 8 查询仅 12/70 相关 / 回退阶梯必须默认开（v1.3.1 改 true），出厂 48/68。
- **中文无空格分词盲区** / 「英伟达RTX5090显卡价格」整串一个 token，截词/挑词全失效；纯中文长句「酸面团酵头怎么培养」同样拆不开 / CJK↔拉丁/数字边界切分 + 品牌型号合并（RTX+5090→RTX5090）+ 纯中文长 token 前3字兜底候选（实测酸面团单发 10/10）。
- **选词评分长度方向** / v1.0 scoreTokenInfo 按长度加分，长泛词（impersonate/显卡价格）碾压短稀有词（primp/dataview）；蜜罐恰拿泛词给 homepage / HTML 通道独立 htmlTokenScore：标识符最值钱、普通词逆长度（越短越稀有）。同长词（dataview/obsidian）离线分不出是理论上限，交阶梯多发候选。
- **垃圾判定器不能堆规则** / 「任意一条沾任意词就放行」太松（homepage 标题带首词）；「锚词必须命中」会误杀真结果（KB 查询只有 kb 编号命中是正常的）；「泛词命中」垃圾与「泛但真」教程合集离线无法区分 / 只做高置信度双闸：标识符全灭=垃圾、覆盖率<1/3=垃圾；其余交阶梯提精度。12 真实样本回放验证。
- **标识符匹配要压空格** / 品牌合并造出 RTX5090，真结果标题是 "RTX 5090" 带空格，标识符闸和覆盖率闸都得用压空格 blob，否则误杀 10/10 真结果。
- **primp 在 Bing 中文索引真没有** / 单发 primp 返回抖音垃圾（不是插件 bug，是 Bing 无此内容）；这类查询回退也救不回，属合理无解。

---

## 脱敏记录（公开版 vs 私有主版）

本文件是私有 PROMPT.md 的公开版，技术内容逐节一致，仅以下脱敏差异（按主版章节列类别，不引用被删字面值）：

| 主版位置 | 移除/替换类别 | 公开版处理 |
|---|---|---|
| 全文 | 私有称呼（11 处） | 中性化为「作者」 |
| 头部 | 私有交接书文件名引用 | 删引用，部署说明自包含 |
| 头部 | 私有备份文件名 | 删 |
| §0 | 本机日期目录名、「拷贝=部署逐字节一致」实例断言 | 泛化为「升级起点 v1.0」 |
| §0 | 主 Python 安装绝对路径（盘符+目录） | 删路径，保留版本事实 |
| §0 | 调研 venv 绝对路径（数据目录相对路径） | 泛化为「独立 venv」 |
| §0 | 私有指令引述（「用户本次明确指示」句式） | 改写为环境纪律陈述 |
| §1/§8/§9 | 主力后端供应商名（3 处） | 泛化为「付费 API 后端」 |
| §3/§11/§12 | 「本网络」本机网络指纹表述（4 处） | 改为「本测试网络」，数据保留 |
| §5.2 | 测试查询中的具体游戏名 | 泛化为「游戏名查询」 |
| §6.1 | 官方 ddgs 插件源码的本机安装绝对路径 | 改为「Hermes 官方 ddgs 插件」 |
| §10 | 整节本机实例状态：config 具体值、Python/venv 路径、私有开发目录名（2 个）、私有源码留档目录、探针样本目录 | 重写为通用「升级前实例核查清单」，只留方法不留机器指纹 |
| §11.6 | 私有规范裁决日期与裁决人称呼 | 泛化为「DIY 插件规范」，规则内容保留 |
| §12 | v1.3.1 回退开关「默认关」旧描述 | 与 v1.3.1 实际默认（开）对齐 |

保留项说明：清华镜像 URL 为公开公共服务地址，非私有信息，保留；EnhancedBing/ddgs/hermes-lcm 为公开项目名，保留；JA4 指纹哈希为公开技术观测值，保留。
