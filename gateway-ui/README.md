# gateway-ui — 适用于 Hermes Gateway 的 Qt GUI 组件

> 需求书：本目录 `PROMPT.md`（设计契约与裁定记录）
> 内部接口契约：本目录 `CONTRACT.md`

随 Gateway 进程启动：hooks 只进事件队列（O(1)，绝不阻塞主循环），消费线程维护
纯函数状态机并写文件总线（`<HERMES_HOME>/cache/gateway-ui/state.json` + `events.jsonl`，
供姊妹项目 gateway-dashboard 消费，§5 契约字段不变）；托盘+桌宠本体是 side-env
独立 worker 进程（PySide6 + Fluent 圆角菜单），崩溃隔离于 Gateway。

## 旧版四宗 bug 的根除方式（A1-A4）

| 旧病 | 根因 | 本版根除 |
|---|---|---|
| A1 重启 enable-pet 必翻 false | `_pet_cfg()` 缺键补默认 false 并**立即回写固化**；左键误触也写配置 | config.py 三态解析：缺键≠false，enable-pet 默认值**永不落盘**；watcher 只写真实差异（缺键+文件 false 不写）；左键不碰配置 |
| A2 左键误触关桌宠 | Trigger 直接翻转 enable-pet 写 pet_cfg.json | 左键=气泡 toggle（纯运行时）；开关权只属于菜单勾选与用户手改 config |
| A3 worker 套娃 | 租约自检"同宿主陈旧租约自家接手"放行同宿主第二个 worker | 租约让位**不区分 owner**：新鲜+pid 活=无条件让位；supervisor spawn 前同款检查；每次 spawn 带唯一 token，心跳 pid+token 双匹配 |
| A4 托盘 26 分钟后消失 | worker 卡死在启动期（stderr=DEVNULL 无痕）但进程活着，supervisor 只查 poll() 永不重拉；5 连败永久放弃 | supervisor 心跳判活（租约>12s 陈旧=卡死→kill+重拉）；worker 看门狗 20s 无 tick 自毁；失败指数退避 3→6→12→30s 封顶**永不放弃**；stderr 重定向 worker_stderr.log + 启动全阶段 trace 留证 |

另修：petdex 帧数契约对齐官方 `FRAMES_PER_STATE=6`（旧版误取 8 帧，动画节奏一直不对）。

## 安装与启用

本插件已在仓库里，随 `custom-plugins` 一起 clone 即到位。启用分两步：

1. 编辑 `config.yaml`，在 `plugins.enabled` 列表加入 `custom-plugins/gateway-ui`
   （套了子目录就写带前缀的名）。**重启 Hermes**（桌面端整个应用退出重开）。
2. 首次启动会自动创建 side-env `gateway-ui-qt` 并安装依赖
   （PySide6-Essentials + PySide6-Fluent-Widgets + Pillow，约 1~3 分钟，一次性）。
   之后托盘图标出现，桌宠/余额气泡默认关，在设置页或托盘菜单按需打开。

**可选：跑 `deploy.py` 做迁移/自检。** 从旧插件名 `gateway-fluent-gui` 升级、或想要
一键编译自检 + 余额适配器播种时，停掉 Gateway 后执行：

```
# 需先设置 HERMES_HOME 环境变量（指向 Hermes 数据家目录）
python "<本插件目录>/deploy.py"
```

脚本幂等、自动备份：复制插件（若已在目标位置则跳过）、side-env 改名零重装、
config 旧键替换（全新安装则提示手动 enable）、dashboard 总线指针同步、
出厂余额适配器清单制播种（你改过的不覆盖、删过的不复活）、编译自检。
config.yaml 的人工改动始终归你自己。

## 状态灯（图标右下小圆点）

| 灯色 | 含义 | 判定 |
|---|---|---|
| 绿 | 空闲 | 活跃 session 数 = 0 |
| 橙 | 忙碌 | ≥1 活跃；1 个时桌宠演该会话阶段（thinking/acting），≥2 一律 busy |
| 红 | 需要留意 | 观察窗（默认 5min）内 LLM 降级（**读 Gateway 内部降级链路标志**判定：只有真走失败切换链才算，手动 `/model` 切供应商不误判；权威源不可达时退回 base_url 端点启发式）或错误连发≥2 |
| 灰 | 失联 | state.json 停止更新 >8s（Gateway 已停） |

活跃计数以 session_id 为键幂等记账（tool-loop 不虚增）；权威源 =
`runner._running_agent_items()`（含桌面端 turn），连续 3 拍不可达退事件兜底；
阶段直读 `agent._last_activity_desc`（内部一手数据）。

悬停 tooltip：`gateway-ui · 状态 · 档位 | uptime | 最近降级 host->host`。
字段走 §5 白名单——**不含消息正文与密钥**。

## 菜单

托盘右键（扁平 8 项，左下角对准鼠标）：
版本号（灰）/ 状态监控档（灰）/ **桌宠▸** / 打开日志 / **设置**（设置界面启用：打开
Fluent 设置窗口）/ 设置(VSCode) / 重启 / 退出。

**左键单击 = toggle 余额气泡（托盘左键改绑，与桌宠左键同款；不写配置）**。
桌宠可见时锚头顶、桌宠隐藏/未启用时锚托盘图标上方。`balance.enabled=false` 时
左键无操作。桌宠显隐改由 桌宠▸「显示桌宠」勾选（enable-pet）控制。

桌宠▸ 二级子菜单（特许）与桌宠本体右键**同一实例**：
显示桌宠✓ / 游走（含爬窗）✓ / 置顶✓ + 切换宠物▸（Petdex▸库存 RADIO + 四平台禁用位）
+ 大小▸（0.5/0.7/1.0 RADIO）。勾选项点击后菜单驻留（鼠标移开自然折叠）。

**重启/退出作用于 Gateway 本体**（DETACHED 逃逸壳，不连坐自身；气泡即时反馈）。

## 桌宠

- 素材只读消费 `hermes pets install` 库存（`<HERMES_HOME>/pets/`），与 Hermes
  内置 display.pet 配置完全隔开。192×208 帧、**每状态 6 帧**、loop 1100ms、
  空白尾帧裁切、Codex 9 行/legacy 8 行自适应、方向跑行+镜像（官方 constants.py 对齐）。
- 透明底浮窗、拖拽、位置持久化（pet_pos.json）。**新建窗口一律屏内右下角地面**
  （出逃存档不复活）；运行中换宠/改大小沿用存档+高度差微调，双向 clamp。
- 地面系统（地面系统七条规则，线段制）：仅游走开启时运行；Win32 枚举窗口上下沿
  成线段（剔除最小化/不可见/cloaked/自身/owned/桌面壳/太小 + z 序遮挡剔除 +
  头顶空间过滤），并入屏幕下界+任务栏上界；0.75-1.25s 确认计时器（拖动暂停、
  移动实时判地面、与 Agent 忙闲无关）；自由落体 5200px/s²（子线段判定不掉，
  否则落正下方第一条）；攀爬 0.22s 线性插值（更高+穿过身体+全包含，无瞬移）；
  站立=底边贴线（无 FEET_DROP_PX 偏移）。**关游走=普通浮窗，零物理代码。**
- 忙时停下演宿主状态（thinking→review、acting/busy→run、error→failed、
  happy→wave）；空闲才游走。
- **高 DPI 清晰渲染（事件反应）**：物理像素渲染+devicePixelRatio 标记+小数缩放
  PassThrough（150% 不圆整、Windows 不做位图二次放大）；换屏自动按新 dpr 重建。
  清晰度上限=素材原生 192×208（放大是 LANCZOS 插值，非超分辨率）。
- **三事件反应（事件反应，`pet.event_reactions` 默认开）**：活跃会话来自
  桌面端/TUI/CLI 时——会话完成→桌宠挥手+托盘闪绿；会话失败→桌宠 failed 动作+
  托盘闪红；等人类输入（审批/clarify）→桌宠挥手+托盘闪烁提醒。用户 /stop 只闪
  托盘不做桌宠动作。闪动 6s 自动恢复、桌宠动作 4s 后恢复游走；worker 重启不补闪
  旧事件。消息平台（群聊等）的事件不触发。
- **余额气泡（余额气泡，`balance.enabled` 默认关）**：左键**单击桌宠本体**（位移
  ≤6px 判单击，>6px 仍是拖拽）弹出/收起四段式气泡——`< [平台] >` / `¥ 金额` /
  `[峰/谷] HH:MM:SS 倒计时` / `缓存于 X 分前`。无边框透明小窗锚定头顶，随宠物
  走/爬/拖跟随，出屏自动翻转。左右箭头在已注册平台间循环（**仅存内存，不写配置**，
  重启回默认；单平台隐藏箭头）。右键气泡=立即刷新/关闭。**点击只读宿主缓存快照，
  绝不直连 API**；查询归宿主周期刷新（默认 300s）+手动刷新。峰谷规则由适配器
  自定义（DS 服务端字段透传/本地北京规则回退：周中 9-12/14-18 峰、周末节假日全谷；
  百炼按量无峰谷；Token Plan 每日 8-22 峰其余谷）。倒计时纯 `时:分:秒`（>24h
  小时位累加，无天数前缀）。>2 倍刷新周期未更新→金额变灰+错误角标。事件反应动画与气泡独立
  共存，不互斥。**气泡开着时桌宠暂停游走、钉在原地演 review 行（同模型思考动画），
  收起即恢复游走**——纯运行时标志不写配置；一次性事件反应（挥手/failed）播放期
  仍优先于聚焦态。详见下"余额气泡"节。

## 余额气泡

**查询归宿主，点击零网络**（额度纪律：Token Plan 实测 18-28s，绝不让点击触发付费
调用）。宿主 `BalanceWatcher` 线程周期拉全部适配器+消费手动刷新请求，结果写
`balance_view.json`；worker 气泡只读这份缓存。

```
宿主 balance-providers/*.py（子进程隔离,普通 10s/SLOW 45s 超时钳,崩溃不连坐）
   → balance_view.json（atomic,签名去重）→ worker 气泡读快照渲染
气泡右键"立即刷新" → balance_refresh_req.json → 宿主重拉（同平台 <10s 节流）
```

**Python Binding 协议**（作者点名可自定义的核心）：投放目录
`<HERMES_HOME>/balance-providers/*.py`，每文件声明 `NAME`/`UNIT`/`fetch()->dict`，
返回 `ok/error/value/display/peak` 块，可选 `lines`（结构化多行排版，见下）。峰谷时段表由 binding 自定义（`peak:null`=
该行不渲染）。**密钥只走环境变量**（os.environ/.env），宿主配置面与总线/日志永不
过明文（`sk-***` 打码贯穿）。投放目录空/开关关=功能静默禁用。

**出厂三适配器**（deploy.py 清单制播种：你改过的不覆盖、删过的不复活）：
- `deepseek.py` — GET /user/balance，服务端 isPeak 透传（~3s）
- `bailian_balance.py` — 百炼按量 billing balance summary，peak=null（~2.9s）
- `bailian_token_plan.py` — 百炼 Token Plan subscription status，SLOW（18-28s，
  后台慢拉不阻塞快源）。**版本可选**（设置页"Token Plan 版本"：个人版默认/团队版，
  经 `HERMES_BALANCE_TOKEN_PLAN_EDITION` 环境变量注入；所选版本无生效订阅报错不回落）。
  排版按作者原型图：`[用户名] 剩余月额度` / `[进度条] 剩余%` / `档位名  距到期X天` /
  `[峰/谷] 时:分:秒`（百炼每日 8-22 峰）。团队版按 weeklyCredits→fiveHourCredits→
  seatSummary 顺序取席位额度，取不到=报错不造数据。

百炼两路依赖 qianwen CLI（npm 包）：CLI 缺失→ok=false+提示安装，不引入宿主硬依赖。

## 设置界面（F7）

托盘"设置"项打开 **Fluent 设置窗口**（FluentWindow 左侧导航三页），与托盘/桌宠
共用同一 worker 进程，单实例（已开则激活置前），关闭即销毁。

**改配置走官方接口，不自己动文件**（作者特许）：官方 writer
（`ctx.set_config` → `save_plugin_setting`：跨进程锁 + ruamel 保注释 + 原子写）
住在 Gateway 进程里，设置窗口在隔离 Qt worker 进程调不到它。故通路：

```
卡片改动 → settings_req.json（总线请求）→ 宿主校验 → 官方 writer 落 config.yaml
         → settings_ack.json 回执 → 界面"已保存/保存失败"反馈
读取：宿主官方 get_config 出 settings_view.json 快照 → 设置窗口/桌宠读快照
```

worker 进程从头到尾不打开 config.yaml；中文注释逐字节保全（端到端实测）。
**pet_cfg.json 中转已退役**，config.yaml 是唯一事实源（旧文件留盘=死文件，可手删）。

| 页 | 卡片 |
|---|---|
| 常规 | tray_enabled（关闭二次确认：会连设置窗口一起停）/ bus_enabled / degrade_window_s（60~1800s）/ verbose_log / package_index_url（清华/阿里/官方/自定义） |
| 桌宠 | enable-pet / slug（实时库存）/ scale（0.1~3.0）/ topmost / roam / **事件反应**（事件反应：完成/失败/等输入时桌宠动作+托盘闪动） + **活体预览**（当前宠物 idle 动画，换宠/拉大小即时变） |
| 余额 | **启用余额气泡**（余额气泡，默认关）/ 刷新周期（60~3600s）/ 投放目录（更改…弹文件夹选择，留空=默认 HERMES_HOME\balance-providers）/ 打开投放目录 |
| 关于 | 版本 / 更新状态 / uptime + 直达：打开日志目录 / 打开总线目录 / VSCode 打开 HERMES_HOME |

每张卡片副标题写明"改什么/会引发什么"。缺键显示"默认值/未设置"角标
（enable-pet 缺键≠false）。卡片改动即时发请求，回执 ok 提示已保存、fail 报错并回滚。

## 设置项（`plugins.entries.custom-plugins/gateway-ui.settings`）

DIY 规范：缺键逐键自动补齐（官方 writer）；**enable-pet 例外——三态处理，
缺键≠false，默认值永不回写**（A1 根除，见上表）。

| 键 | 默认 | 改什么 | 实测影响 | 会引发什么 |
|---|---|---|---|---|
| `tray_enabled` | true | 托盘图标本体 | false=worker 被停、无图标；supervisor 每拍直读 config，改回 true 即复活，**无需重启** | 总线照常，dashboard 不受影响 |
| `bus_enabled` | true | events.jsonl 事件流写入 | false=dashboard 事件列表断粮；state.json 照写（托盘灯/桌宠状态不受影响） | 只影响事件流，不影响状态快照 |
| `degrade_window_s` | 300 | 红灯观察窗秒数 | 越小红灯恢复越快 | 只影响灯色/档位，不影响桌宠 |
| `verbose_log` | false | 详细日志 | true=GW/WK/PET 三前缀写 debug.log/pet_debug.log（400KB 滚动）；false=零写入 | 排故时开，定位后关；只读旁路无副作用 |
| `package_index_url` | 清华镜像 | side-env 首装 pip 源 | 已装机器不触发安装，改它无感 | 仅新机器首装生效 |
| `pet.enable-pet` | false | 桌宠总开关 | true=浮窗出现；菜单/设置页改动→官方 writer 落 config（0.25s watcher 仅差异才写） | 手改 config=重启后生效；缺键≠false，绝不补 false 固化 |
| `pet.slug` | "" | 用哪只宠物 | 空=字母序第一只已安装 petdex | 菜单/设置页"切换宠物"即时生效 |
| `pet.scale` | 0.7 | 大小 | 0.7→134×145px；合法域 0.1-3.0 | 菜单/设置页"大小"即时生效 |
| `pet.roam` | true | 游走 | false=钉在原地（零物理） | 菜单/设置页"游走"即时生效 |
| `pet.topmost` | true | 置顶 | false=可被窗口盖住 | 菜单/设置页"置顶"即时生效 |
| `pet.event_reactions` | true | 三事件反应（事件反应） | false=完成/失败/等输入不闪不动；缺键按 true 读、绝不自动落盘 | 仅桌面端/TUI/CLI 会话触发；默认 true 系作者点名（DIY 默认 false 规范的已申报偏离） |
| `balance.enabled` | false | 余额气泡总开关（余额气泡） | true=左键单击桌宠弹泡；false=入口禁用、宿主 watcher 空转不拉不写 | 修改类默认关（DIY 规范）；需投放目录有适配器才显示数据 |
| `balance.refresh_s` | 300 | 宿主周期查询间隔 | 60~3600s 钳位；气泡只读缓存，点击绝不直连 API | 越短越新鲜但付费调用越频；Token Plan 源 SLOW 后台慢拉不阻塞快源 |
| `balance.token_plan_edition` | individual | Token Plan 查询版本 | individual=个人版月额度；team=团队版席位额度 | 所选版本无生效订阅时气泡报错，不回落另一版 |
| `balance.providers_dir` | ""（回落默认） | 适配器投放目录 | 空=`<home>/balance-providers`；显式路径优先 | 设置页"更改…"弹文件夹选择；密钥只走环境变量，配置面永不过明文 |

注：`bus_enabled=false` 时 state.json 与 events.jsonl 都停写（消费者断供），
托盘灯 8s 后转灰——这是预期行为，不是 bug。

## 日志地图

| 文件 | 位置（cache/gateway-ui/） | 内容 | 开关 |
|---|---|---|---|
| agent.log | Data/logs/ | Gateway 侧（前缀 `gateway-ui:`） | 常开 |
| worker_trace.log | 总线目录 | worker 生死/spawn/启动全阶段/CRASH | 常开 |
| worker_stderr.log | 总线目录 | worker stderr（Qt 报错/traceback），>2MB 截断 | 常开 |
| debug.log | 总线目录 | GW/WK/PET 状态迁移/灯色/权威集 | verbose_log |
| pet_debug.log | 总线目录 | 桌宠物理决策 | verbose_log |
| pet_error.log | 总线目录 | 桌宠构造失败/sheet 缺失（限频） | 出错才有 |

## 测试（tests/，全部独立可跑）

宿主 python（`<HERMES_HOME>/tools/python-3.14*/python.exe`）：
- test_config.py — A1 三态/不回写/10 轮重启不翻（20 断言）
- test_statemachine.py — 忙线判据+降级（内部链路标志/手动切换不误判/兜底启发式）+权威源+notice 派生/平台门控/幂等（81 断言）
- test_supervisor.py — A3/A4 心跳判活/永不放弃/退避/venv 蹦床回归（30 断言）
- test_register.py — 宿主总装冒烟：门控四形态/9 hook 接线/settings watcher 回写/幂等/非法拒写（40 断言）
- test_settings_bus.py — settings 协议：validate_request 白名单/钳制 + build_view 快照/三态 + watcher 集成 + balance 四键链（93 断言）
- test_notice_host.py — 事件反应 notice 宿主管线：on_session_end/approval/clarify→notice 集成（24 断言）
- test_notices.py — NoticeTracker 纯逻辑：seq 去重/首轮不回放/过期/seq 回退基线（32 断言）
- test_official_writer_e2e.py — 真·官方 writer 端到端：scratch 假 HERMES_HOME 实测 save_plugin_setting 保注释/锁/回读/A1（16 断言；依赖根自动探测 uv archive）
- test_integration_smoke.py — 真集成：Supervisor 真实 spawn side-env worker，蹦床心跳 15s 无误杀+优雅停止无孤儿（12 断言，约 30s）
- test_balance_host.py — 余额气泡宿主余额：provider 发现/子进程隔离/超时钳/normalize/密钥打码/手动刷新节流/SLOW 慢拉/签名去重/lines 透传(含 bar fraction)/extra_env 注入/Token Plan 版本选择（133 断言）
- test_peak_rules.py — 余额气泡峰谷纯函数：ds_cn 边界/节假日/windows 优先/next_boundary 严格大于/倒计时格式（64 断言，宿主+side-env 双跑）

side-env python（`<HERMES_HOME>/environments/gateway-ui-qt/gen-*/venv/Scripts/python.exe`，offscreen）：
- test_ground.py — 线段制地面物理（39 断言）
- test_pet_menu.py — 菜单驻留真实事件路由（19 断言）
- test_spawn_pos.py — 屏内出生/出逃存档不复活（8 断言）
- test_sprites.py — 6 帧契约+官方常量对齐+高 DPI 物理像素/DPR/LANCZOS（81 断言）
- test_dpi.py — 事件反应高 DPI：PassThrough 契约/物理尺寸/DPR 标记/换屏重建（55 断言）
- test_pet_reaction.py — play_reaction 三 kind/钉住恢复/重复刷新/隐藏 no-op（42 断言）
- test_tray_flash.py — 闪动交替帧/自动恢复/刷新/纯函数像素断言（46 断言）
- test_notice_wire.py — 事件反应收口接缝：路由/门控/stopped 只闪/tick guard（20 断言）
- test_worker_lease.py — 租约让位三态+左键气泡 toggle 不写配置+静态租约/mtime 心跳（35 断言）
- test_worker_settings.py — worker settings 协议：请求/回执/快照/改道/A2 保留/单实例（27 断言）
- test_settings_window.py — Fluent 设置窗口：卡片→请求映射/刷新不回写/回执回滚/活体预览/事件反应卡片（91 断言）
- test_pet_click.py — 余额气泡单击/拖拽门槛：CLICK_MAX_PX=6/on_click 回调/None 字节级旧行为/5 位置参构造（37 断言）
- test_balance_bubble.py — 余额气泡窗：四段式渲染/箭头循环/峰谷倒计时跨边界/新鲜度变灰/错误截断/跟随翻转/立即刷新请求/lines 图一排版(含进度条 bar)（121 断言）
- test_balance_cards.py — 余额气泡设置余额页：四键请求映射/缺键角标/refresh 钳位/目录弹窗/版本选择/回执回滚/乐观值最后一条胜出（36 断言）
- test_bubble_wire.py — 余额气泡收口接缝：门控三态/toggle/跟随/无锚必收/零 settings_req/balance 块进 worker/sync_pet 接 on_click/托盘左键改绑同款+托盘锚定（34 断言）
- test_bubble_focus.py — 气泡聚焦增强：气泡聚焦钉住演 review 行/reaction 仍最高优先/解除恢复游走/main 接缝置位/零 settings_req（21 断言）

测试纪律：测试根目录由 `HERMES_HOME` 环境变量解析（`tests/_env.py`，未设置即拒绝运行）；
一切测试只用 `<HERMES_HOME>/cache/scratch/` 下隔离总线目录，绝不碰真实总线与 config。

## 硬件友好写盘

空闲期总线**零数据页重写**：tray_lease/writer_lease 内容静态（只留 pid/token，spawn 写
一次），心跳用 `os.utime` 只动文件 mtime（tray 5s / writer 2s）；state.json 内容签名
去重，无变化不重写、5s utime 维持读侧 TTL。卡死判定（A4）改看 mtime 陈旧，语义不变。
旧版每秒 3 次 tmp+rename 全文件重写降为空闲期纯元数据操作；数据页写只发生在内容真变时。
uptime 由消费侧自算 `now - boot_ts`（state 里是常量锚，不再每拍写变化值）。

## 已知局限（如实申报）

- Win11 托盘默认收纳：图标可能落在溢出区（▲），系统策略无法强制外露。
- worker 常驻 RSS≈134MB / USS≈62MB（渲染改案作者观感裁定主动接受）。
- in_process 插件崩溃理论上连坐 Gateway：全量 try/except 缓解，无法根除
  （根除需 isolation: host，v1 不走）。
- 降级判定优先读 Gateway 内部 `_provider_fallback_active` 标志（真失败切换链才置位，
  手动切供应商不置位）；权威源不可达时退回端点启发式，基线=主配置端点，解析失败退化 first-seen。
- 更新检查走官方 source_check.check_for_updates(passive=True)（banner 同源，
  尊重 updates.check=false），900s 一次，fail-safe 查不到=不亮。
- 设置窗口住在 worker 进程里：在设置页关 tray_enabled 会连设置窗口自己一起停掉
  （已加二次确认）；这是架构边界，不是 bug。
- 旧总线 cache/gateway-fluent-gui/ 与旧 side-env 名残留由作者处置；
  dashboard 姊妹项目的总线指针已随部署脚本同步改到 cache/gateway-ui/。
- 高 DPI 清晰度上限=素材原生 192×208：scale×dpr>1 是 LANCZOS 插值放大，
  比旧"逻辑渲染+系统二次放大"清晰，但不是超分辨率。
- await_input 平台归因宁漏报不误报：裸 CLI session 尚无 turn_start 前科时
  不产生 notice（CLI 交互面通常先有 turn_start，实际覆盖良好）。
- 余额气泡峰谷倒计时：binding 的 peak 块若既无 rule 又无 windows，倒计时到
  next_boundary_ts 后钉 00:00:00 等宿主新 view（worker 无规则不能自行推进）——
  自定义 binding 应至少给 rule 或 windows。
- 余额查询是宿主进程内子进程执行 binding：binding 崩溃/超时被隔离（ok=false），
  但 binding 自身若死循环占 CPU，超时钳只杀进程不限制其 CPU——投放目录只放可信脚本。
- 百炼两路适配器依赖 qianwen CLI（npm 包）：未装→ok=false+提示，不自动安装
  （DIY 规范：不静默装软件）。Token Plan 源实测 18-28s，归 SLOW 后台慢拉。