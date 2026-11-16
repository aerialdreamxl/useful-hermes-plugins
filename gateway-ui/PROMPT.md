# gateway-ui 需求书 — 适用于 Hermes Gateway 的 Qt GUI 组件

版本：v1.0.0
日期：2026-11-14
状态：**已实现并验收（28 套测试全绿，含真集成冒烟）**。托盘状态灯 + petdex 桌宠浮窗 +
Fluent 设置界面 + 可插拔余额气泡 + 地面物理增强（窗口跟随/遮挡修复/垂直寻路，§11），全部落地。

> 本文件是设计契约与裁定记录，不是使用说明——使用见同目录 `README.md`，
> 宿主⇄worker 接口权威见 `CONTRACT.md`。公开版由私有母版脱敏生成，
> 删改清单见文末《脱敏记录》。

## 变更史（蒸馏）

私有母版按迭代轮次逐条记录裁定；公开版收敛为按主题的变更史，技术细节以正文各节为准。

- **托盘与事件总线（M0）**：随 Gateway 启动挂托盘状态灯（绿/橙/红/灰），hooks 只进
  事件队列（O(1) 不阻塞主循环），消费线程维护纯函数状态机并写文件总线
  （`state.json` + `events.jsonl`，字段白名单，无消息正文/密钥）。渲染层定为
  PySide6 + qfluentwidgets（Fluent 圆角菜单）。
- **桌宠浮窗（M1）**：petdex spritesheet 兼容（192×208、每状态 6 帧、Codex/legacy
  自适应、方向跑行+镜像），透明底、拖拽、位置持久化；独立 `pet:` 配置块与 Hermes
  `display.pet` 完全隔开。右键二级子菜单（与托盘"桌宠▸"同一实例）。
- **地面物理**：线段制地面系统——Win32 枚举窗口上下沿成线段（遮挡/自身/壳类剔除），
  自由落体 + 攀爬 + 确认计时器；关游走=普通浮窗零物理。站立=底边贴线（根除 2px 判定偏差）。
- **忙线判据**：活跃会话以 session_id 为键幂等记账（0=idle/1=该会话阶段/≥2=busy）；
  权威源 = Gateway 内部 `runner._running_agent_items()`，不可达退事件兜底。
- **整体重写（A1-A4 根除）**：旧代码废弃，模块化重写。A1 配置三态（缺键≠false、
  默认值永不落盘）；A2 左键纯运行时不写配置；A3 租约让位不区分 owner + spawn_token；
  A4 supervisor 心跳判活 + worker 看门狗 + 失败永不放弃。
- **Fluent 设置界面（F7）**：worker 发请求文件 → 宿主调官方 writer → 回执，worker
  全程不碰 config.yaml；常规/桌宠/关于三页 + 活体预览。
- **高 DPI + 三事件反应**：物理像素渲染 + devicePixelRatio + 小数缩放 PassThrough；
  会话完成/失败/等人类输入 → 桌宠动作 + 托盘闪动（`pet.event_reactions` 默认开）。
- **余额气泡（BalanceBubble）**：左键单击桌宠/托盘弹可插拔余额气泡，查询归宿主
  （子进程隔离 binding + 周期刷新 + 手动节流），worker 只读缓存零网络；峰谷倒计时、
  平台箭头切换、进度条排版、Token Plan 个人/团队版可选；气泡聚焦态钉住桌宠演思考动画。
- **硬件友好写盘**：租约内容静态化（只留 pid/token），心跳改 `os.utime` mtime；
  state.json 内容签名去重，空闲期零数据页重写。
- **降级判定精确化（BUG 修复）**：旧版「生效 base_url ≠ 主配置端点即降级」会把用户
  手动 `/model` 切换供应商误判成降级。改读 Gateway 内部降级链路标志
  `agent._provider_fallback_active`——只有 `try_activate_fallback` 真走失败切换链才置位
  （源码实锤：chat_completion_helpers.py 置位、agent_runtime_helpers.py 主恢复清零），
  手动切换走 `session.mutate` 重建 agent、不碰该标志。权威源（`_running_agent_items()`
  live agent）可达时以该标志为准，端点比较只补 from/to 展示；连续 3 拍不可达退回端点
  启发式兜底。test_statemachine +11 断言（手动切换不误判/真降级记录/主恢复/启发式门控与兜底）。

## 0. 一句话目标

随 Gateway 进程启动，先在系统托盘挂一个状态图标（M0），再拉起一个透明置顶浮窗桌宠
（M1+），由 Gateway 真实活动（入站消息、turn、工具调用、错误）驱动情绪与动画。

### 0.1 里程碑改序（早期迭代裁定 2026-11-13）

托盘图标先做：它是纯状态显示 + 菜单（隐藏/退出/开关），没有窗口、没有透明、没有动画，
是整条事件总线（§5/§6）+ 开关矩阵（F5）的最小可交付载体；浮窗是托盘之上的增量。
交付切分：

- **M0 托盘**：Qt 图标 + 右下状态灯（绿/橙/红灰）+ 扁平单层 **8 项 Fluent 圆角菜单**（菜单定稿定稿）
  （**渲染改案改案：渲染层 pystray→PySide6+Fluent，作者嫌原生菜单难看；菜单定稿定稿：
  更名 gateway-fluent-gui、删桌面端/网页后台两按钮、"设置"拆两项（Fluent 面板
  预留位+VSCode 直达）**；禁二级子菜单，桌宠段=单个"未实现"灰项）+ 事件总线生产者侧 + 逃逸式生命周期按钮 spike。托盘常驻可接受，RSS 增量预期
  <5MB（仅消息泵线程，无窗口）。
- **M1 浮窗桌宠（桌宠落地已交付，改案：pygame-ce→Qt 同进程）**：与托盘同 QApplication
  独立 worker 进程；petdex spritesheet 兼容（192×208、8×9 Codex/9×8 legacy 自适应、
  空白尾帧裁切、方向跑行+镜像，契约照抄 agent/pet/constants.py 与桌面端 pet-sprite.tsx）；
  透明底（WA_TranslucentBackground，屏幕截图实锤无黑底）、6 帧循环动画（loop 1100ms）、
  游走（loaf 为主+指数 dwell+stroll 偏宽敞侧，照抄 roam-behavior.ts；屏幕底边 ledge）、
  拖拽+位置持久化（bus/pet_pos.json）、右键二级菜单（特许）。实测：spike 三件套+集成
  回归全绿（窗口/动画/游走 743px/二级菜单/配置回写）。
- **M2 状态机动画接线补全** + **M3 气泡** + **M4 格式适配层扩展**（见 F2'）。
- **F7 Fluent 设置界面**：后置于 M1 键面稳定（桌宠落地裁定先桌宠）。

## 1. 可行性裁定（源码取证，v0.23.1，安装树 <HERMES_HOME>\hermes-agent）

| 前提 | 裁定 | 证据 |
|---|---|---|
| Hermes 是否禁止插件跑 GUI | **无禁令** | `gateway/ agent/ hermes_cli/ tools/` 全树 grep：tkinter/PyQt/PySide/wx 零引用，无任何策略代码 |
| 插件代码跑在哪 | Gateway 进程内（默认） | `hermes_cli/plugin_isolation.py:103-109`：`plugins.isolation` 默认 `in_process` |
| Gateway 用的解释器有没有 tkinter | **有，实测可用** | `Data\tools\python-3.14.9+202****1021-win32-x64\python.exe`（ps 确认 PID 19312 = `hermes_cli/main.py gateway run`）实跑 `tkinter.Tk()` 成功，Tk 9.0 |
| GUI 线程能否看到桌面 | 当前能（桌面端拉起 Gateway，交互式会话） | **假设 A1**：Gateway 必须运行在交互式登录会话；改服务/计划任务（Session 0）浮窗不可见——启动时须探测并降级 |
| "随主循环的挂点" | `gateway:startup` 等用户事件钩子 | `gateway/hooks.py:5-9`（事件表）、`gateway/run_startup.py:1597`（主循环上发射）；插件侧 `pre_gateway_dispatch`（`hermes_cli/plugins.py:164-165`） |
| 官方 dashboard 第三方挂载接缝 | **存在**（勘误 2026-11-13 修订取证：首版曾误记为"无"）——dashboard plugin 经 `<home>/plugins/*/dashboard/manifest.json` 发现，`api` 文件的 FastAPI router 自动挂 `/api/plugins/<name>/`。浏览器入口是独立 `hermes dashboard` 进程（本机 9119，实测 kanban 同链路 200）；8642 是 api_server 不是 dashboard（四查定案，详见姊妹需求书 §1）。姊妹项目 gateway-dashboard 已改走此路线，本 pet 插件的总线生产者身份不变 | `hermes_cli/web_server_dashboard.py:586-648, 1055-1131`；详见 `gateway-dashboard/PROMPT.md` §1 |
| 与官方 petdex 的区别 | 官方 petdex 是聊天窗内置吉祥物；本项目是 OS 级浮窗 + 真实 Gateway 活动驱动 | skills 清单 `productivity/petdex` |
| 桌面端内嵌悬浮方案（对照，非本项目） | 桌面端无插件级透明浮层，20261106 鲸鱼娘项目已裁定走 `placement:'floating'` 自绘 fixed 层 | `<姊妹移植项目，未随仓库发布>/PROMPT.md`；本项目明确选 OS 级浮窗路线，与之互补不冲突 |

## 2. 工程约束

1. **作者 DIY 插件规范（2026-11-09 定）**：
   - 每个功能一个独立 bool 开关，放 `plugins.entries.<id>.settings`，修改类行为默认
     `false`，读到缺键按 `false`；配置写入走官方 writer（`ctx.set_config` /
     `save_plugin_settings`），逐键自动补齐默认值。
   - README 必须写每个选项改什么/实测影响/会引发什么。
   - 可选重依赖用 `pm.ensure_environment` 隔离 side-env 按需装，不塞共享 venv。
     **早期迭代实测定案（2026-11-13）：渲染引擎 = pygame-ce，托盘 = pystray，均走 side-env**
     （wheel：pygame-ce 9.4MB + pystray 0.2MB + 已有 Pillow；tkinter 零安装但 +16MB 且
     子线程 Tcl 崩溃风险硬约束，见 §2.2/附录实测表）。Gateway 解释器是
     python-build-standalone cp314 win_amd64，两家均有 cp314 wheel（已装验证通过）。
2. **主循环不得阻塞**（最高工程纪律）：
   - GUI mainloop（pygame 帧循环 / pystray 消息泵）是阻塞泵，**绝对禁止**在 Gateway 的
     asyncio 循环线程或任何 hook 回调内联跑。
   - 线程模型（早期迭代更新）：
     - hook/事件回调（运行在主循环线程）只做 `queue.put(event)`，O(1) 返回；
     - **pygame 守护线程**独占窗口与 SDL（SDL2 对子线程宽容，实测 cp314 线程内起窗无碍
       ——这是它相对 tkinter 的决定性优势：tk/customtkinter 在子线程抛
       `main thread is not in main loop` / `Calling Tcl from different apartment`）；
     - **pystray 独立守护线程**跑 `icon.run()`（实测 daemon 线程内 run/stop 干净，
       Windows 原生 `_win32` 后端，无需额外 Win32 代码）；图标更新用
       `icon.notify`/`icon.icon = new_img` 的线程安全通道；
     - 回传 Gateway（若有交互）用 `asyncio.run_coroutine_threadsafe`，主循环引用在
       `gateway:startup` 处 `asyncio.get_running_loop()` 捕获并保存（该 hook 恰在主循环
       上发射，`run_startup.py:1597`，这是唯一合法的取 loop 时机）。
3. **崩溃隔离**：in_process 模式下插件异常会连坐 Gateway。所有回调、GUI 线程主循环
   全量 try/except + logger，任何失败只降级不抛出；GUI 线程死亡不得影响 Gateway 功能。
4. 假设 A1 探测：启动时校验交互式窗口站（`user32.GetProcessWindowStation` 或试建窗口
   即毁）；不可见则静默禁用窗口与托盘、只留状态输出，README 写明。
5. DPI：pygame 侧 SDL 自带 DPI 感知；进程级 DPI aware 设置若仍需，注意进程是
   Gateway 的，影响同进程其它 GUI 使用者（v1 接受并写进 README）。
6. 落盘：状态快照/位置持久化只写 `<HERMES_HOME>\cache\gateway-ui\`（整体重写改名），
   不碰其它目录；禁改任何配置文件（isolation 保持默认 in_process）。

### 2.6 Win32 colorkey 真透明配方（M1 渲染层，已端到端实测 2026-11-13）

pygame 自身**没有**窗口透明 API（社区桌宠教程口径一致："这一步无法在 pygame 里完成"），
必须拿原生句柄调 Win32；无需 pywin32，ctypes 三调用，实测通过（截图判据：窗口区域内
black=0、key 色=0、前景像素≈图形面积）：

```python
hwnd = pygame.display.get_wm_info()["window"]          # set_mode(..., NOFRAME) 之后
ex = user32.GetWindowLongW(hwnd, -20)                   # GWL_EXSTYLE
user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x8)    # WS_EX_LAYERED | WS_EX_TOPMOST
user32.SetLayeredWindowAttributes(hwnd, RGB(255,0,255), 0, 1)  # LWA_COLORKEY=1，洋红抠像
```

配套纪律：
- 每帧先 `screen.fill((255,0,255))`；**抠像色禁止出现在素材调色板**（petdex/鲸鱼等第三方
  素材须过一遍色板冲突检查，冲突时改抠像色而非改素材）。
- 精灵边缘留 1px 安全色防色边；洋红抠像下 alpha 渐变边缘会发品红——素材按硬边像素风
  准备最稳。
- 摆放窗口 `SetWindowPos` 禁带 SWP_NOMOVE(0x1)——实测静默忽略坐标、窗口掉屏幕正中央。
- pygame-ce 的 `pygame.Window` 对象有 `opacity`/`borderless`/`always_on_top` 属性
  （实测存在）但**无透明底属性**，colorkey 路线不变。

## 3. 功能需求

- F0 **托盘图标（M0，先行交付）**：pystray 原生 Win32 后端，`_win32` 实测可用。
  - **状态灯**：图标右下角叠加小圆点（Pillow 现绘，16/32px 两套）。三色制（三色定稿定稿
    2026-11-13）：
    | 灯色 | 条件 |
    |---|---|
    | 绿 | 空闲：活跃 session 数 = 0 |
    | 橙 | ≥1 个 session 活跃。**忙线判据定稿（作者裁定，替换旧逐事件闪烁制）**：活跃
      计数以 `agent.session_id` 为键（`pre_api_request`/`post_llm_call` 载荷同源实锤），
      入账/销账幂等——0=始终 idle；1=按该会话阶段 busy/thinking 或 busy/acting
      （tool 事件切 acting，下一 API 请求回 thinking）；≥2=一律 busy。tool-loop 一次
      turn 的多个 API 请求不再虚增计数（旧 bug：按 turn_id 计数灯迟迟不熄）；多活跃
      时未知键 turn_end 不瞎销账（旧 FIFO 乱销是闪烁源）；error/happy 只在零活跃窗
      口作瞬时展示层，TTL 兜底防漏销泄漏。桌宠 `at_rest` 门控=活跃数 0（观察2 一致）。 |
    | 红 | 观察窗（默认 5min，settings `degrade_window_s` 可调）内发生过 **LLM 降级**。
      **降级判定改判：以 base_url 端点判定，非 provider 名**——生效请求的 base_url ≠ 主配置
      端点（`model.provider` → `providers.<name>.base_url` 解析）即为降级；同端点换名
      不算、轮内换道（pre=主端点、post=备端点）要算。`pre/post_api_request` 载荷均带
      base_url（fire-site kwargs 实锤）。实测三场景回归通过。 |
  - **左键单击** = 显示/隐藏桌宠浮窗（Qt 版走 activated(Trigger) 信号；M0 阶段浮窗
    未出生时点击无操作，README 注明 M1 生效）。
  - **悬停 tooltip**：`gateway-ui · <状态档> · 最近降级: <主→备> <HH:MM>`（整体重写改名；字段全走
    §5 白名单，无消息内容/密钥；无降级记录时省略末段）。
  - **右键菜单：扁平单层（三色定稿铁律：不许二级子菜单——作者原话"类目表不是二级菜单，
    二级菜单会很坐牢"）**。**弹出位置定稿弹出位置：菜单左下角对准鼠标**（popup 默认是
    左上角锚点，Qt 原生把菜单弹在鼠标右下/旁边；按 sizeHint 换算 y-h 后手动 move+exec，
    屏顶翻转、屏右钳制兜底）。原"桌宠/直达站"类目标题取消，条目按语义顺序直接平铺：
    1. `Hermes Gateway v{VERSION}` —— 禁用项（灰），启动时取版本常量/包元数据。
    2. `状态监控: {档}` —— 禁用项（灰）。四档：`暂无异常`（窗内无错误无降级）/
       `轻微波动`（有错误但已重试恢复）/ `需要留意`（正在降级或错误连发≥2）/
       `更新可用！`（存在待装新版。通道实锤：`hermes_cli/banner.py:193
       get_latest_release_tag()` 返回 `(tag, release_url)`，本地 git tag 比对，CLI
       启动 banner 同源、miss 有缓存。检查放低频异步任务，禁止在菜单回调里同步等
       网络）。
    3. `桌宠▸` —— **桌宠落地更正：原"未实现"槽位=桌宠二级子菜单**（作者特许，
       非扁平勾选项；首轮误放成 checkable 项，作者纠正"原来那个未实现的位置是给
       桌宠二级菜单的，右键菜单排板改一下即可"）。子菜单一级：显示桌宠✓/游走✓/
       置顶✓；二级：切换宠物▸（库存逐项勾选）/大小▸（0.5/0.7/1.0）。与桌宠本体
       右键共用 `build_pet_menu`；每次展开重建（勾选态跟实时配置）；Fluent 子菜单
       走 `_subMenus` 不走 `actions()`，搬运须两路都抄。
    4. `打开日志`：拉起系统文件管理器到 Gateway 日志目录（已实装：HERMES_HOME/logs）。
    5. `设置` —— **菜单定稿定稿新增**：预留 Fluent 设置界面入口（作者规划中的图形化
       配置面板）。设置界面产出前 = 禁用灰项；产出后接拉起命令。与下一项语义区分：
       本项=图形化面板，下项=直接开代码目录。
    6. `设置(VSCode)`：拉起 VSCode 打开 HERMES_HOME 根目录（作者定稿："直接打开装着
       HERMES_HOME 的 VSCode"；`code` 命令探测不到 → 禁用态）。
    7. `重启` —— 等效于命令行的整网关重启子命令。**实现红线**：禁止从本进程直接
       spawn 后等待——Gateway 收到 SIGTERM 会连坐杀掉命令子进程（官方守卫
       `terminal_tool_guards.py` 同源问题；手动内联重启另有 #110637 监管 bug）。
       必须 DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP 起脱离进程树的壳执行，点击后
       即时反馈"重启指令已发出"，成败由下次托盘重建自证。M0 spike 第一项验证
       逃逸重启真能拉回。
    8. `退出` —— 等效于命令行的整网关停止子命令，同一逃逸纪律；Qt 版带
       showMessage 即时反馈。语义=停整个 Gateway（与 F6"只退 GUI"不同层，作者
       定稿如此，README 加粗写明）。
    （菜单定稿删除：`打开桌面端APP`、`打开网页后台` 两按钮——作者裁定不要了；
    `dashboard_url` settings 键随之退役。）
  - 状态刷新走 §5 事件队列消费端（托盘线程内），禁止 hook 回调直接碰 icon 对象。
  - 已知平台坑（写 README）：Win11 托盘默认收纳，图标可能落在溢出区。
- F1 浮窗规格（**桌宠落地改案：pygame-ce→Qt，与托盘同 worker 进程**）：无边框
  （FramelessWindowHint）、置顶（WindowStaysOnTopHint，`pet.topmost` 可关）、可拖拽、
  逐像素透明（WA_TranslucentBackground，屏幕截图实锤）、尺寸=petdex 原生帧×`pet.scale`
  （默认 0.7→134×145）、位置持久化 `bus/pet_pos.json`（拖拽落点/游走落点原子写）。
- F1' **桌宠右键菜单（桌宠落地特许二级）**：一级 `隐藏桌宠`/`游走`(勾选)/`置顶`(勾选)；
  二级 `切换宠物`（已安装 petdex 库存逐项勾选）、`大小`（0.5/0.7/1.0）。改配置写
  `bus/pet_cfg.json`→Gateway watcher 回写官方 writer（worker 不碰 config.yaml）。
  pet_cfg.json=pet 块运行时单一事实源，meta 仅首启播种；config.yaml 手改=重启后生效。
- F2 形象（**桌宠落地：petdex 优先兼容，M4 其余源后置**）：状态行映射
  idle→idle / listening→waiting / thinking→review / acting→run / happy→wave /
  error→failed；游走时方向跑行 running-left/right+镜像（legacy sheet 回退 running 行）。
  素材只读消费 `<HERMES_HOME>/pets/<slug>/`（作者已 hermes pets install 的库存）。
- F2' **格式适配层（M4 扩展；M0 只需产出统一 `SpriteProvider` 接口）**：宠物源四类，
  解析器各自实现同一接口（给出 状态→帧序列 映射），渲染与状态机不感知来源：
  | 源 | 格式实况 | 适配要点 |
  |---|---|---|
  | petdex | `<HERMES_HOME>/pets/<slug>/pet.json`（`id/displayName/version/spritesheetPath`）+ `spritesheet.webp` 动画图集；官方加载器在 `agent/pet/{constants,manifest,store}.py`，状态集 idle/running/review/error/done——**先读这三个文件对齐状态映射与图集切分参数，不逆向猜** | 只读消费作者已 `hermes pets install` 的库存；192×208 原生帧需 scale |
  | dsh-pet / dsh-whale | 20261106 鲸鱼娘项目的自绘 fixed 层素材（见 `<姊妹移植项目，未随仓库发布>/`） | 素材目录与帧命名以该项目 PROMPT.md 为准；抠像色冲突检查（§2.6）必做 |
  | bongocat | 拍立得桌宠社区素材（GIF/APNG 帧序列为主） | Pillow 解 GIF 帧即可；循环与帧延迟字段保留 |
  | 自生成 | 本插件原生约定：`assets/<state>/<frame:%03d>.png` | 基准格式，其余源向它归一 |
- F3 情绪状态机（输入=§5 事件总线）：
  - 入站消息 → listening（3s 超时回）
  - turn 开始 → thinking；工具事件 → acting
  - turn 结束成功 → happy 2s → idle；失败/error 事件 → error 直到下次活动
  - 空闲 ≥N 分钟（settings 可调，默认 5）→ 打瞌睡帧
- F4 气泡（可选开关，默认 false）：头顶小气泡显示当前工具名/模型名，截断 40 字符，
  **绝不显示消息正文与密钥**（字段白名单见 §5）。
- F5 开关矩阵（**桌宠落地：桌宠键独立成 `pet:` 块**）：顶层 `tray_enabled`(托盘，默认
  true), `bus_enabled`(事件总线生产者，默认 true), `degrade_window_s`(红灯观察窗秒数，
  默认 300), `package_index_url`(side-env pip 源，默认清华镜像)。
  **`pet:` 块**（与 Hermes `display.pet` 完全隔开）：`enable-pet`(桌宠总开关，默认
  false，原顶层 `enabled` 一次性迁移更名), `slug`(空=第一只已安装), `scale`(0.7),
  `roam`(游走，默认 true), `topmost`(默认 true)。`show_bubble`/`sleep_ani` 留待 M2/M3。
  缺键逐键自动补齐（DIY 插件规范②，写走官方 writer）。
  （菜单定稿：`dashboard_url` 随"打开网页后台"按钮退役。桌宠落地：旧顶层 `enabled` 留作死键，
  官方 writer 无删键 API，README 注明可手删。）
- F8 **跨宿主单实例（桌宠落地实测发现补裁）**：gateway run 与 dashboard --isolated 都会
  加载本插件→曾双托盘。tray_lease.json 租约互斥：supervisor spawn 前查租约（新鲜+pid
  活+非本进程=不 spawn），worker 启动自检同款；持租者死=8s 内另一宿主接管。
- F7 **Fluent 设置界面（菜单定稿新增，规划项）**：图形化配置面板，接管本插件全部
  settings 键的读写（仍走官方 writer；这是"设置"菜单项的落地目标）。技术栈 Fluent
  由作者指定；产出前菜单项=禁用灰态。界面细节另开需求轮次，本条只登记职能与接缝。
- F6 退出：Gateway shutdown 路径销毁窗口与托盘（`on_session_end` 不可靠，用 atexit/loop
  取消信号兜底；pystray `icon.stop()` 实测可从其他线程干净调用）；窗口/托盘线程侧被杀
  不得拖垮 Gateway。

## 4. 非目标

- 不做点击宠物唤起会话/输入框（v2）；不做多显示器跟随；不做 Linux/macOS；
- 不做桌面端内嵌悬浮层（那是 whale-widget 的路线）；不在 v1 引入 PyQt；
- 托盘不做气球通知（balloon toast）刷屏——只作状态灯与菜单，M0 明确排除；
- 不在 side-env 里做素材转换工具链（GIF→帧序列等离线预处理归作者脚本）。

## 5. 与姊妹项目共享的数据契约

- 位置：`Data\cache\gateway-ui\state.json`（整体重写改名；原子写：tmp+rename）+ `events.jsonl`
  （滚动保留最近 2000 条）。选择文件总线而非进程内共享模块：对 isolation 模式切换、
  重启顺序、崩溃隔离都更鲁棒。
- 事件行 schema（v1）：`{seq:int, ts:float, kind:"inbound|turn_start|tool|turn_end|error",
  session_key:str, platform:str, tool:str?, model:str?, ok:bool?}` ——
  **字段白名单，禁止出现消息正文、prompt、密钥**。
- state schema：`{pet_state:str, active_sessions:int, gateway_uptime_s:float,
  last_event_seq:int, updated_ts:float}`。
- 生产者归属：事件订阅与总线写入实现在本插件（gateway-ui，整体重写重写改名）内；`enabled` 只管窗口
  不管总线，总线自身开关独立（`bus_enabled` 或由 dashboard 侧只读消费，实现时按
  最小心智负担裁定并写 README）。gateway-dashboard 是纯消费者，不写总线。

## 6. 事件源清单（hook 锚点，全部现成、无需改核心）

| 事件 | 来源 | 锚点 |
|---|---|---|
| 主循环引用/启动 | `gateway:startup` | `gateway/run_startup.py:1597` |
| 入站消息 | `pre_gateway_dispatch`（observe-only） | `hermes_cli/plugins.py:164`；`gateway/run_inbound*.py` |
| turn 生命周期 | VALID_HOOKS：`pre_llm_call`/`post_llm_call`/`api_request_error` | `hermes_cli/plugins.py:111+`，fire-site 在 agent 循环 |
| 工具调用 | `pre_tool_call`/`post_tool_call` | 同上 |
| 会话边界 | `on_session_start/end/reset` + 用户钩子 `session:*` | plugins.py / `gateway/hooks.py:5-9` |
| 中断 | `agent_loop_stopped` | plugins.py:170 附近 |

实现纪律：**注册了 hook ≠ 有能力**——每个锚点动手前先读 fire-site kwargs 与调用顺序
（hermes-source-investigation 技能 Pitfalls），载荷里没有的字段不许硬凑。

## 7. 风险与局限（如实申报）

1. in_process 插件崩溃连坐 Gateway：全量 try/except 缓解，无法根除；根除需
   `plugins.isolation: host`，host 模式部分 ctx 接缝不支持（`plugin_isolation.py`
   HOST_UNSUPPORTED 表）且 GUI 落 plugin-host 子进程——v1 不走，v2 评估。
2. 假设 A1 失效场景：Gateway 转服务/无人值守远程机——浮窗与托盘静默降级。
3. 洋红抠像对素材约束（§2.6：调色板禁现抠像色；渐变边缘发品红）——硬边像素风素材最稳。
4. 进程级 DPI aware 影响同进程其它 GUI 使用者（当前无，见 §2.5）。
5. pygame SDL 初始化在守护线程：RSS/功能实测全部通过，但官方从未承诺"非主线程"是
   supported 配置——M1 spike 阶段先在真 Gateway 进程内跑一次最小窗口确认，再铺功能。
6. pystray 菜单回调在 pystray 自己的消息泵线程执行，回调内只做 `queue.put`，禁止
   直接触碰 pygame/asyncio 对象。
7. 菜单"重启/退出"按钮触发的是全 Gateway 生命周期（作者早期迭代定稿语义）：进程内直发
   生命周期命令必被 SIGTERM 连坐（官方守卫 `terminal_tool_guards.py`，另有 #110637
   监管 bug）——唯一安全实现是 DETACHED 逃逸壳；此路径必须在 M0 spike 实测通过才准
   进菜单，未通过则两按钮保持禁用态发布。

## 8. 验收清单

**M0（托盘先行）**：
- 开 Gateway → 托盘出现，右下灯=绿，且不动主循环（响应延迟无可测变化）；
- 发一条消息 → 灯 绿→橙（turn 中）→绿；`state.json`/`events.jsonl` 按 §5 落盘；
- 人为制造降级（断主 provider 或注入错误，测试钩子）→ 观察窗内灯转红、窗过自动回；
- 菜单 9 项扁平逐项核对：三禁用灰项（版本/状态监控/未实现）文案正确；四个"打开X"
  按钮在目标缺失时为禁用态；"更新可用！"档在人为改低本地 tag 时能点亮；
- 逃逸重启 spike：点"重启"→ Gateway 真的完成重启且托盘重建（此项不过=禁止发布）；
- "退出"二次确认后 Gateway 停止，无僵尸进程；
- `tray_enabled=false` → 无图标，`bus_enabled` 照常。
- （菜单定稿）菜单 8 项终版核对：`打开桌面端APP`/`打开网页后台` 不存在；`设置`（灰）与
  `设置(VSCode)` 并列；插件名 gateway-fluent-gui 下 enable/加载/托盘三处一致。
- （整体重写）改名 gateway-ui 后同款核对：插件 id/enable 键/加载日志前缀/托盘 tooltip
  四处一致；**A1-A4 根除判据**：①菜单勾选 enable-pet 后重启 Gateway 不翻 false；
  ②左键单击只隐藏/显示浮窗、config 不变；③任务管理器仅一个 tray worker；
  ④挂 30 分钟托盘不消失（worker_trace.log 心跳续租可查）。

**M1+（浮窗）**：
- 开 Gateway → 5s 内浮窗出现且透明验证过 §2.6 判据（截图数像素，非肉眼）；
- 从 QQ/桌面端各发一条消息 → 宠物依次 listening→thinking→acting→happy；
- 拖拽后重启 Gateway → 位置保留；
- `enabled=false` → 无窗口，托盘与总线照常；
- 注入异常杀 GUI 线程（测试钩子）→ Gateway 功能无损；
- Session 0 模拟（或代码路径审查）→ 探测降级生效。

## 9. 建议排产顺序（早期迭代改序）

1. **M0**：事件订阅 + 文件总线（§5/§6）+ pystray 托盘状态灯与菜单——无窗口，最先可测，
   且交付即可日常用；
2. **M1**：side-env（`pm.ensure_environment`：pygame-ce + pystray）+ pygame 最小窗
   （静态一帧 + §2.6 透明配方 + 拖拽 + 位置持久化）；
3. **M2**：状态机与动画帧（自生成基准格式）；4. **M3**：气泡与开关补全；
5. **M4**：SpriteProvider 适配层逐个扩（petdex → whale → bongocat）；6. 验收逐项跑。

## 10. 桌宠余额气泡需求书（BalanceBubble）——已实现（2026-11-14，26 套件全绿）

### 10.1 dsh-whale 调研结论（源码实锤，assets/whale-widget.js 22320 行，v0.4.2 副本）

- **交互语义**：点鲸鱼→思考泡泡弹出；再点=翻页（bubbleNext，行 18735）：手动序列
  一轮一轮翻（bubbleShowSeqNext），翻完收起。默认"首次点击"泡泡=模块列表
  （bubbleDefaultFirstModules，行 7960）：**标题文本 → 余额(大号) → 今日已用(小号)
  → 峰谷时段行**——即作者原型图的四段式雏形。
- **峰谷机制**（行 18012 注释区）：isPeak 优先取**服务端余额接口的字段**；未就绪
  （首拉/失败）时回退**本地北京时间规则**（bubbleCountdownIsPeak）先显示、就绪后
  校正。显示样式三档：峰谷时段(文字)/简洁(峰|谷单字)/时段倒计时(HH:MM:SS)。
  高峰红 #e0433f、空闲绿 #2fa24c，各自可配底色。
- **模板占位符体系**（行 10273/17451）：`{balance_ds}` `{expense_ds}` `{status}`
  `{countdown}`（仅峰谷模块）；峰谷/余额/今日/随机语句均为可增删"内容模块"。
- **左右箭头**：dsh-whale 本体**没有**平台切换箭头（翻页=点宠物本体）。作者原型图的
  `<  ¥ 99999.99  >` 是作者融合 dsh-pet 式平台切换的**新语义**，非上游既有——按作者
  要求落进我们的设计（切换单位=余额平台，非内容页）。

### 10.2 需求条目（R25-1 ~ R25-8）

- **R25-1 触发**：左键**单击桌宠本体**（与拖拽用位移阈值区分，沿用事件反应 AI 浮窗
  设计点的同一接缝；**不改**托盘左键显隐语义）弹出/收起 BalanceBubble；气泡已开时
  再点本体=收起。事件反应（play_reaction）不与点击气泡互斥：动画照播，气泡独立层。
- **R25-2 气泡布局**（对标图1，Qt 无边框透明小窗，宽度 ~220px，锚定宠物头顶，随
  宠物移动保持贴合，出屏边界自动翻转到身体另一侧）：
  ```
  ┌ <  [平台标题]  > ┐
  │   ¥ 99999.99    │   ← 金额行（大号）
  │  [谷] 11:45:14   │   ← 峰谷行（时段状态+HH:MM:SS 到下一时段倒计时）
  └ 缓存于 X 分前 ──┘   ← 数据新鲜度角标（小字）
  ```
- **R25-3 平台切换**：左右箭头在**已注册 provider 列表**内循环切换；切换状态仅存
  worker 内存（重启回默认第一个），**不写配置**（A2 纪律：点击≠指令）。仅一个
  provider 时隐藏箭头。
- **R25-4 Python Binding 规格**（作者点名可自定义的核心）：宿主侧
  `<HERMES_HOME>/balance-providers/*.py`，每文件协议：
  ```python
  NAME = "DeepSeek"          # 显示标题
  UNIT = "CNY"
  def fetch() -> dict: ...   # {"value": 99999.99, "status_line": {...}, ...}
  ```
  返回值必含：`ok`/`error`/`value`/`display`(格式化金额)/`peak` 块
  `{"is_peak": bool, "label_peak": "峰", "label_valley": "谷", "next_boundary":
  "HH:MM"|规则表达式, "countdown_from": epoch_s}`——**峰谷时段表由 binding 自定义**
  （DS 与服务端字段、百炼按量/Token Plan 各自规则不同；binding 无峰谷概念时返回
  `peak: null`，气泡该行整体不渲染）。密钥由 binding 自查环境（os.environ/.env），
  宿主配置面永不过明文（死规矩）。宿主**子进程隔离执行**+超时钳 10s+崩溃不拖垮其余
  provider+结果写缓存。投放目录为空/开关关=功能静默禁用（DIY 规范①默认 false）。
- **R25-5 查询归宿主**（额度纪律+Token Plan 实测 18-28s）：宿主 balance watcher
  周期刷新（默认 5 分钟，可配）+ 手动刷新请求（气泡右键"立即刷新"经总线触发）。
  worker 点击**只读缓存快照，绝不直连 API、绝不开网**。总线新增第四文件
  `balance_view.json`（atomic tmp+rename，mtime+内容签名双判，沿用 §6 模式）：
  `{"providers": [{name, unit, ok, value, display, peak, error, fetched_ts}],
  "default": "…", "ts": …}`。
- **R25-6 内置三适配器** = usage-bridge 已实测路由（2026-11-06 定型）：
  DS `/user/balance`（含服务端 isPeak 透传）；百炼按量 billing balance summary
  （availableAmount CNY）；百炼 Token Plan subscription status（monthlyCredits
  usedPct/resetTime——峰谷行用 reset 倒计时替代）。三适配器是投放目录的出厂示例，
  用户可删可改可加。
- **R25-7 新鲜度呈现**：气泡底行"缓存于 X 分前"；>2 倍刷新周期未更新→金额行变灰+
  错误角标；provider `ok=false` 时金额行显示短错误（截断 24 字符），不弹全屏错误。
- **R25-8 配置键**（走 §6 settings 总线，全部修改类默认保守）：
  `balance.enabled`(false=整个气泡功能关)、`balance.refresh_s`(默认 300)、
  `balance.providers_dir`(默认 `<home>/balance-providers`，DIY 规范③重依赖走
  ensure_environment 思路——实际查询在宿主进程，无 side-env 依赖)。设置页"桌宠"页
  加余额气泡卡片组（开关+刷新周期+投放目录"打开目录"按钮）。

### 10.3 验收断言（动工时逐条转测试）

1. 单击/拖拽判定：位移 <阈值 弹泡、≥阈值 拖拽不弹泡（复用 AI 浮窗 spike 常量）。
2. 气泡锚定：宠物走/爬/被拖时气泡跟随，出屏翻转；宠物隐藏(enable-pet off)时气泡必收。
3. 箭头循环：N=3 provider 左移右移遍历回原点；N=1 箭头隐藏；worker 重启回默认。
4. binding 协议：合法返回渲染；超时 10s→该 provider error 态；崩溃/语法错/无
   fetch()→隔离报错不影响其余；密钥字面量不出现在任何总线文件与日志（脱敏断言）。
5. 峰谷三态：binding peak=null→行不渲染；有 peak→[峰]/[谷]+倒计时逐秒走；跨边界
   翻转正确（用假时钟测）。
6. 缓存纪律：点击 0 网络调用（mock 计数断言）；手动刷新请求→宿主重拉→view 更新
   →气泡 ack 式刷新。
7. A1/A2 保留：点击切换平台不产生任何 settings_req；缺配置键不自动落盘。
8. 官方 writer E2E：新配置键仍走 req→validate→set_config→ack 链路（扩展
   test_settings_bus 白名单）。

### 10.4 风险与排产

- **Spike 先行**（作者"先小实验再扩展"习惯）：①透明宠物窗上叠第二个无边框窗的
  层级/跟随/穿透行为；②单击 vs 拖拽 vs 双击（预留给 AI 浮窗的位移阈值常量是否
  兼容两功能并存——**与事件反应 AI 浮窗需求合并设计**，避免两次改 main.py）。
- 与 hermes-whale-widget 移植项目（20261106）关系：那边受"行为等价移植"裁定①约束
  不许加新交互；本项目是 gateway-ui 新功能。R25-4 binding 协议设计成可回移植形态，
  是否共用待作者裁定（建议：本轮只做 gateway-ui 侧）。
- 百炼两路依赖 qianwen CLI（npm 包），适配器须处理 CLI 缺失的降级（ok=false+提示
  安装），不引入宿主硬依赖。
- 建议排产：spike（0.5 天）→ 宿主 provider 执行器+bus（1 天）→ worker 气泡窗+点击
  接缝（1 天）→ 三适配器+设置页（0.5 天）→ 回归+文档回写。

### 10.5 实现记录（2026-11-14 收口，全量回归 27 套件全绿）

> 本节按时间顺序记录余额气泡从首版交付到最终态的历次修订；**最终断言数以下方
> 「最终回归」为准**，早期表格里的数字是首版交付时的快照，已被后续修订更新。

**交付清单**：

| 层 | 文件 | 职责 |
|---|---|---|
| 协议 | `CONTRACT.md §10` | balance 总线：`balance_view.json`(host→worker) / `balance_refresh_req.json`(worker→host) / binding 规格 / 峰谷规则透传 / 密钥打码 |
| 宿主 | `balance_host.py` | provider 发现 / 子进程隔离执行(runpy 注入,普通 10s/SLOW 45s 超时钳) / normalize / `BalanceWatcher` 周期+手动刷新+签名去重写 view |
| 宿主 | `config.py` | `BALANCE_DEFAULTS`+`REQ_KEYS` 三键+validate balance 分支+`read_balance_explicit`+build_view balance 块（top/pet 零改动） |
| 宿主 | `__init__.py` | register 起 `gui-balance` 线程；`_apply_setting` balance.* 整块合并；`_write_view` 同拍调 `write_view()` |
| 宿主 | `plugin.yaml` | config_schema 增 balance 对象块（enabled/refresh_s/providers_dir） |
| 出厂 | `balance-providers/{deepseek,bailian_balance,bailian_token_plan}.py` | 三适配器（DS 服务端 isPeak 透传；百炼按量 peak=null；Token Plan SLOW 月额度+reset 倒计时）；deploy.py 清单制播种（改过不覆盖/删过不复活） |
| worker | `peak_rules.py` | 峰谷纯函数单一事实源（零 Qt 零 IO）；宿主经 importlib 复用同一文件 |
| worker | `balance_bubble.py` | 四段式气泡窗 + `bubble_pos()` 纯函数；箭头内存切换；1s 倒计时跨边界推进；右键立即刷新/关闭 |
| worker | `pet_window.py` | `CLICK_MAX_PX=6` 单击/拖拽门槛 + `on_click` 回调（None=字节级旧行为） |
| worker | `main.py` | `_on_pet_click` toggle / `_sync_bubble` 门控+刷新+跟随+隐藏必收 / reload_settings 收 balance 块 |
| worker | `settings_window.py` | 新增"余额"导航页（开关+刷新周期滑杆+投放目录更改/打开）；`_effective` 修"最后一条 pending 胜出" |

**验收断言映射**（§10.3 逐条 → 测试套件，全绿）：

| 断言 | 套件 | 断言数 |
|---|---|---|
| 1 单击/拖拽判定 | test_pet_click | 37 |
| 2 锚定跟随/翻转/隐藏必收 | test_balance_bubble + test_bubble_wire | 104+22 |
| 3 箭头循环/N=1 隐藏/重启回默认 | test_balance_bubble | （含上） |
| 4 binding 隔离/超时/脱敏 | test_balance_host | 111 |
| 5 峰谷三态/跨边界（假时钟） | test_peak_rules | 64 |
| 6 缓存纪律 0 网络/手动刷新 | test_balance_host + test_balance_bubble | （含上） |
| 7 A1/A2 点击零 settings_req | test_bubble_wire + test_balance_cards | 22+29 |
| 8 官方 writer E2E balance 白名单 | test_settings_bus | 84 |

**全量回归**：宿主 11 套件（config/statemachine/supervisor/register/notice_host/notices/
official_writer_e2e/integration_smoke/settings_bus/balance_host/peak_rules）+ worker 15 套件
（sprites/ground/dpi/spawn_pos/pet_menu/pet_reaction/pet_click/tray_flash/worker_lease/
worker_settings/notice_wire/settings_window/balance_bubble/balance_cards/bubble_wire）= 26 全绿。

**实现偏差与裁定**（如实申报）：
- 设置页余额卡片做成**独立"余额"导航页**（非塞进"桌宠"页）——三键+目录两按钮共四卡片，
  独立页更清爽；功能等价 R25-8。
- `_effective` 原实现返回**第一条** pending，与文档"最后一条"不符；快速拖滑杆产生多条
  同键 pending 时乐观值取旧值。本轮修为"最后一条胜出"（哨兵 `_MISSING` 区分无 pending
  与值为 None），91 条既有设置窗口测试无回归。
- 气泡未开时每拍仍 `refresh_view()` 预读快照（开泡即显不空白），只是不 `follow`（省几何空转）。
- peak 块既无 rule 又无 windows 时：倒计时到 `next_boundary_ts` 后钉 00:00:00 等宿主新 view
  （worker 无规则不能自行推进）——binding 应至少给 rule 或 windows。
- 百炼两路依赖 qianwen CLI：CLI 缺失→ok=false+提示安装，不引入宿主硬依赖（隔离烟测已验）。

**气泡聚焦增强（作者建议，2026-11-14 同日）：气泡聚焦态**
- 需求：余额气泡打开时暂停游走，用模型思考时同款动画（review 行）把桌宠定在原地。
- 实现：`pet_window.bubble_focus` 运行时标志（默认 False）。`_step` 把它并入"非闲"
  判定（`roaming = at_rest and not dragging and not bubble_focus`）→ 掐断游走转 pause；
  `_advance_frame` 气泡聚焦时演 review 行（优先于 host_state，即便宿主 idle 也演思考态）。
  **优先级链：reaction（一次性事件反应）> 物理姿态(fall/jump/climb/walk) > bubble_focus(review) > host_state**。
- 接线：`main._set_bubble_focus(on)` 置位；`_on_pet_click` 弹/收即时置位（零延迟）；
  `_sync_bubble` 每拍按气泡可见性驱动（开=聚焦，关/隐藏/门控关=恢复）。纯运行时，零 settings_req（A2）。
- 测试：test_bubble_focus.py 21 断言全绿；worker 全套件回归无破坏（walk 行解包曾遭
  模糊匹配误改 `, False`，已修回 `row, mirror = sheet.walk_row(self.dir)`——walk_row 返回元组）。

**余额气泡修订（作者三点修改+峰谷补充裁定，2026-11-14 同日，27 套件全绿）**
1. 倒计时格式：`fmt_countdown` 去掉 `Nd ` 前缀，纯 `时:分:秒`，>24h 小时位累加
   （25h→25:00:00）。peak_rules 单一事实源，宿主/worker 同改同测。
2. Token Plan 版本选择：新配置键 `balance.token_plan_edition`（individual 默认/team，
   修改类缺键不落盘 A1）。设置页余额组加 ComboBoxSettingCard「Token Plan 版本」
   （个人版/团队版，独立 ConfigItem+断槽同款）。宿主 BalanceWatcher 经
   `HERMES_BALANCE_TOKEN_PLAN_EDITION` 环境变量注入 binding 子进程（run_provider
   新增 extra_env kwarg；binding 只读 env 不碰 config）。所选版本无生效订阅→ok=false
   报错不回落另一版。团队版取数 weeklyCredits→fiveHourCredits→seatSummary 顺序，
   取不到=报错不造数据（作者账号 team=not_subscribed，结构以未来实测为准）。
3. Token Plan 排版按作者原型图：binding 交 `lines=[{text,style}]`（title/amount/info），
   normalize 透传（打码+清洗），气泡 `_render_lines` 三槽整行显示（超宽缩字号不截断），
   取代旧单行金额；峰谷行独立。真实 CLI 冒烟实锤：
   `[TestUser] 剩余月额度 / [进度条] 75% / Standard  距到期N天`。
   用户名取 qianwen auth status→user.aliyunId；档位=edition.name；天数=remainingDays。
4. 峰谷补充裁定：百炼每日 0-8 谷 8-22 峰 22-24 谷（BAILIAN_PEAK_WINDOWS，周末不例外），
   月重置 resetTime 改留 extra.reset 不占峰谷行；DS 维持 ds_cn（周中 9-12/14-18 峰、
   周末全谷，节假日列表接口给则透传、不给不造）。
- 测试：test_peak_rules 64 / test_settings_bus 93 / test_balance_host 129 /
  test_balance_bubble 116 / test_balance_cards 36，其余套件无破坏，27 套件全绿。
- 文档 README 同步（四键表/适配器节/测试清单）。

**托盘左键改绑（作者裁定：托盘左键改绑同款余额气泡，2026-11-14，27 套件全绿）**
- `on_tray_activated(Trigger)` 从 `toggle_pet_visibility()` 改绑 `_toggle_bubble()`
  （与桌宠左键同一函数）；`_on_pet_click` 变薄封装。显隐功能收归 桌宠▸「显示桌宠」
  勾选（enable-pet），运行时临时隐藏入口随改绑移除（作者裁定的取舍，README 注明）。
- 新 `_bubble_anchor()`：桌宠可见=头顶矩形；不可见（enable-pet off/隐藏）=托盘图标
  `geometry()`（TRAY_ICON 全局，_main_inner 接线）；几何无效/都无=None→不弹。
  托盘弹的泡不因桌宠隐藏被 `_sync_bubble` 误收（改锚托盘跟随）；桌宠不可见时不聚焦
  （无宠可钉），桌宠恢复可见即恢复聚焦语义。门控关仍必收。
- A2 不变：左键零 settings_req/零 config 写。test_worker_lease 29 / test_bubble_wire 34
  （+托盘改绑/锚定/无锚必收 12 断言）/ 其余套件无破坏。
- 测试坑两枚：offscreen 下真 Qt `show()` 异步映射 isVisible 不即时→用 `_timer.isActive()`
  判弹出；lease 的 FakeWin 缺 x/y/width/height 致 `_pet_rect` 抛异常被 toggle try 吞→补几何。
- README/CONTRACT 同步。

**硬件友好写盘（作者硬件友好裁定：租约去 ts、心跳改 mtime，2026-11-14，27 套件全绿）**
- 动机：每秒 tmp+rename 重写租约=纯重复数据页写+目录日志，折寿 SSD。但 ts 是 A4 心跳
  （supervisor 靠陈旧判卡死），不能简单删——改**内容静态+os.utime mtime 心跳**兼得。
- tray_lease.json：spawn 写一次 {pid,owner_pid,spawn_token}（无 ts）；tick 每 5s
  `touch_lease` utime（LEASE_TOUCH_S=5 < LEASE_FRESH_S=8）。lease_decision/lease_self_check/
  supervisor lease_held/_heartbeat 全改 mtime 优先，旧 ts 字段仅作过渡回落（新旧共存）。
- writer_lease.json：静态 {pid}（pid 变化才写），2s utime（读侧写者租约起本就 mtime TTL 4s）。
- state.json：移除 updated_ts（零消费侧死字段）；gateway_uptime_s 改 boot_ts 常量锚，
  tray tooltip/设置关于页自算 now-boot_ts（旧字段兼容回落）；内容签名去重——无变不重写，
  空闲 5s `touch_state` utime 维持 STATE_TTL 8s。
- 写盘账：旧=3 次/秒全文件 tmp+rename（tray 1s+writer 1s+state 5s）；新=空闲期仅
  utime 元数据操作（tray 0.2/s+writer 0.5/s+state 0.2/s），数据页写只在内容真变时。
- 测试：test_worker_lease 35（+静态无 ts/utime 节流/mtime 优先判定 6 断言）、
  test_supervisor 假时钟改 utime 设 mtime、register/notice_host 契约字段改 boot_ts；
  27 套件全绿含真集成冒烟。CONTRACT §4/§5 同步。

**进度条修正（作者纠正：图一第二行是进度条不是文本，2026-11-14，27 套件全绿）**
- 误读认领：修订把第二行当成文本（额度数字+`--`+百分比，连红色波浪线都误判为拼写检查标记）。
  作者两次澄清：那串 X/`--` 是**进度条**占位，整行=`[进度条] XX%`，无字面额度数字。
- 新增 `bar` line 类型贯通三层：binding 交 `{style:"bar", fraction:剩余占比0~1, text:"73%"}`；
  normalize 透传 fraction（钳位 0~1、非法/缺失归零）；气泡 `_ProgressBar(QWidget)` 自绘
  圆角进度条（左条右百分比，填充 fluent 蓝 #4cc2ff、空槽半透明白、过期变灰），
  `_render_lines` 改按 lines 顺序逐行落位（title/amount/info 文本槽 + bar 槽）。
- 真实 CLI 冒烟：fraction=0.75 text="75%"；offscreen grab 渲染目检进度条约 75% 填充、
  百分比右对齐、垂直居中无重叠（CJK 在 offscreen 显方框系缺字体，真桌面正常）。
- 测试：test_balance_bubble 121（+bar 显示/fraction/钳位/过期变灰/隐藏 5 断言）、
  test_balance_host 133（+bar fraction 透传钳位 4 断言）；27 套件全绿。
- README/CONTRACT §10.1 同步（bar 类型+示例）。

**最终回归（权威，27 套件全绿，含真集成冒烟）**

| 侧 | 套件 | 断言数 |
|---|---|---|
| 宿主 | test_config | 20 |
| 宿主 | test_statemachine | 81 |
| 宿主 | test_supervisor | 30 |
| 宿主 | test_register | 40 |
| 宿主 | test_notice_host | 24 |
| 宿主 | test_notices | 32 |
| 宿主 | test_settings_bus | 93 |
| 宿主 | test_balance_host | 133 |
| 宿主 | test_peak_rules | 64 |
| 宿主 | test_official_writer_e2e | 16 |
| 宿主 | test_integration_smoke | 12 |
| worker | test_sprites | 81 |
| worker | test_ground | 39 |
| worker | test_dpi | 55 |
| worker | test_spawn_pos | 8 |
| worker | test_pet_menu | 19 |
| worker | test_pet_reaction | 42 |
| worker | test_pet_click | 37 |
| worker | test_tray_flash | 46 |
| worker | test_worker_lease | 35 |
| worker | test_worker_settings | 27 |
| worker | test_notice_wire | 20 |
| worker | test_settings_window | 91 |
| worker | test_balance_bubble | 121 |
| worker | test_balance_cards | 36 |
| worker | test_bubble_wire | 34 |
| worker | test_bubble_focus | 21 |

测试纪律：一切测试只用 `<HERMES_HOME>/cache/scratch/` 下隔离总线，绝不碰真实总线与
config；测试根目录由 `HERMES_HOME` 环境变量解析（`tests/_env.py`），未设置即响亮拒绝。

## 11. 地面物理增强需求书（窗口跟随 + 遮挡修复 + 垂直寻路）——动工版（2026-11-16）

> 作者三项指令：①桌宠知道脚下线段属于哪个窗口并跟随其运动；②修复"A 窗口盖住
> B 窗口后桌宠仍站在 B 下沿浮空"BUG；③只能爬上不能爬下——加入向下动作与
> 跨线段寻路游走。本节为设计契约，实现记录收口后回写。

### 11.1 BUG 根因取证（探针实锤 2026-11-16，scratch/gw_ground_probe.py）

浮空**不是**"没刷新地面"——walk 每帧、pause 每 0.75-1.25s 都在重建线段。真根因：

- `ground._to_logical_point(px, py)` 要求点落在某块屏矩形内才返回换算结果；
  `build_ground_segments` 拿**窗口左上角**去换算，返回 None 即整窗丢弃。
- 最大化窗口带隐形 resize 边框，左上角是**负坐标**（本机实锤：Chrome 最大化
  物理矩形 `-7,-7 → 1542,918`）→ 左上角在所有屏外 → 整窗被丢。
- 被丢的窗口不进 `win_rects`，**不参与 Z 序遮挡剔除** → 被它完全盖住的 B
  窗口上下沿照旧注册为地面 → 桌宠站在"已被盖住的下沿"上视觉浮空。
- 合成复现：A=(-8,-8,1544,872) 盖住 B=(300,200,700,500)，B 下沿仍在产出列表
  （`BUG REPRO: True`）。多屏场景同理：跨屏/副屏负原点窗口一律被丢。

### 11.2 R26-1 遮挡修复（BUG fix，无开关，属修复不属新功能）

- 换算改**矩形相交选屏**：窗口物理矩形与各屏物理矩形求交，取交面积最大的屏
  作换算基准（左上角在屏外不再丢窗）；完全无交的窗口才丢。
- 遮挡剔除语义随之自动恢复：A 最大化盖 B → B 的沿被剔除 → 桌宠确认计时器
  （≤1.25s）内自由落体到 A 上沿/任务栏/屏底。
- 既有判定（rule6/7 容差、全包含、头顶空间过滤）一律不动。

### 11.3 R26-2 线段归属与窗口跟随（新键 `pet.follow_window`，默认 true）

- **归属**：每条线段携带来源窗 HWND（内部结构，不改变既有 4 元组对外形状——
  归属表 `seg_owner` 平行存放；屏幕下界/任务栏上界 owner=None 不跟随）。
  站定/静默改绑/落位/攀爬完成时同步记录 `cur_owner`。
- **跟随**：pause 态每拍（33ms）对 `cur_owner` 调一次 `GetWindowRect`（O(1)，
  不做全量枚举），矩形变化 → 桌宠按 dx/dy 平移贴线；x 夹进新线段可行域
  `[x1, x2-pet_w]`（BUG3 纪律）；窗口移屏时按新屏重算归属。
- **失效链**：owner 窗口关闭/最小化/cloaked/被更高窗遮挡（每拍轻量矩形查，
  遮挡确认仍走既有确认计时器全量重建）→ 立即转入既有自由落体链，绝不悬挂。
- **忙时也跟随**：与 rule5 同口径——Agent 忙、气泡聚焦、事件反应播放期间
  窗口被挪走仍要贴线移动（只是不主动游走）；拖拽中不跟随（人手优先）。
- 关 `follow_window` = 字节级旧行为（窗口挪走靠确认计时器兜底落体）。
- 跟随产生的落点写 pet_pos.json 沿用既有节流（仅内容变化才写）。

### 11.4 R26-3 向下动作与跨线段寻路（新键 `pet.vertical_roam`，默认 true）

- **向下爬（对称 rule7）**：新纯函数 `find_descend_target`——非站立、更低
  （y 更大）、x 全包含（BUG2 同口径）、且**下降路径无遮挡**（当前线段与目标
  线段之间不存在其他有效线段横在身体 x 域内）→ 平滑下移动画（复用 climb
  管线，`phase="descend"`，时长随距离线性、下限 0.22s，无瞬移），动画演
  jumping 行（与 climb/fall 同款，无新素材需求）。
- **触发**：`_plan` 决策时（游走开启、闲、非聚焦）掷大数轴骰盘
  （R26-5c 现行为，见 §11.12：[0,10000) 一次掷骰，上爬领地/下爬领地
  各 4%，其余没想法；候选试序=先 find_climb_target 后
  find_descend_target）；两者皆无 → 既有漫步。（历史注记：初版为
  VERTICAL_CHANCE=0.35 上爬优先、无则下爬的双骰链，R26-5c 退役。）
- **跨线段寻路（有限范围，不做全图 A*）**：漫步走到线段端点后，若端点外侧
  存在 x 邻接（间隙 ≤ pet_w）且高度差 ≤ 2×pet_h 的线段，按概率跳过去：
  向上=既有 climb 管线延伸（允许 x 微调到目标可行域）；向下=descend；
  同高=平移衔接。每跳之间强制回到 pause+确认计时器，杜绝连跳失控。
- **安全钳**：目标线段必须过头顶空间过滤（BUG1）；descend 落点必在屏内；
  垂直动作期间每帧刷新地面，目标线段失效（窗口关闭/被盖）→ 当帧中止转
  自由落体（半途动作可掐断纪律）。
- 关 `vertical_roam` = 只保留既有"确认计时器被动上爬"，无主动垂直动作。

### 11.5 R26-4 配置与设置页（DIY 规范全守）

- `pet.follow_window` / `pet.vertical_roam`：**显示类**（与 roam/topmost 同族，
  缺键自动补齐走官方 writer——它们改变的是既有 roam 功能的行为细节，非新增
  修改类介入；默认 true 使修复后的体验开箱即得）。
- 链路：PET_DEFAULTS + validate bool 分支 + plugin.yaml pet.properties +
  settings_window 桌宠页两卡片（「跟随窗口」「垂直游走」）+ `_apply_view`/
  ack 同步 + test_settings_bus 白名单自动覆盖（REQ_PET_KEYS 由默认表派生）。
- worker `reload()` 读两键；A2 不变：运行时点击/跟随/寻路零 settings_req。

### 11.6 验收断言（动工时逐条转测试）

1. 遮挡修复：负原点 A 盖 B → B 上下沿不注册（合成枚举，offscreen）；真机
   枚举含最大化窗口（本机 Chrome 现场回归）；正坐标窗口行为与旧版全等。
2. 归属：站立线段 owner=来源 HWND；屏底/任务栏 owner=None；改绑同步。
3. 跟随：fake GetWindowRect 平移 → 桌宠同 dx/dy 贴线；窗口右移出线段域 →
   x 夹可行域；owner 消失 → 落体；拖拽中不跟随；follow_window=false 字节级
   旧行为（哨兵断言）；忙时（at_rest=False）仍跟随。
4. 下降：find_descend_target 纯函数（更低/全包含/路径遮挡拒绝/站立排除）；
   descend 动画单帧位移有界、落位贴线、目标中途失效当帧转 fall。
5. 寻路：端点邻接线段跳转移入 pause+确认；间隙/高差超限不跳；
   vertical_roam=false 零主动垂直动作（_plan 采样断言）。
6. 配置 E2E：两新键 req→validate→ack→settings_view→worker reload 全链；
   缺键补齐走官方 writer；显式 false 不被默认碾掉（A1）。
7. 全量回归：既有 27 套件零破坏（地面判定语义未动）。

### 11.7 风险与排产

- GetWindowRect 每拍 O(1) 只查当前 owner，成本可忽略；遮挡确认仍走既有
  0.75-1.25s 全量重建（0.23ms/次实测），不加新枚举频率。
- 跟随与拖拽/游走/攀爬并发：跟随只在 pause 态生效，walk/climb/fall/descend
  期间靠既有每帧地面刷新兜底，避免双写位置打架。
- descend 与 climb 共用插值管线但方向相反，测试须覆盖"下爬中途目标被盖"
  的中止路径（半途可掐断纪律）。
- 排产：BUG 修复+回归 → 归属跟随+测试 → 垂直寻路+测试 → 配置链路+设置页
  → 全量回归 → README/CONTRACT 回写。
- 探针善后：gw_ground_probe.py 为只读探针已跑毕，无残留进程（offscreen）。

### 11.8 实现记录（2026-11-16 收口，28 套件全绿）

**交付清单**：

| 层 | 文件 | 改动 |
|---|---|---|
| worker | ground.py | `_rect_screen` 矩形求交选屏（R26-1 根修）；`window_logical_rect`/`owner_window_rect`（O(1) 跟随查询，关/最小化/cloaked→None）；`build_ground_segments(with_owners=True)` 归属表（默认返回形状不变）；`find_descend_target`（更低+全包含+路径无遮挡取最近）；`find_adjacent_segment`（间隙≤pet_w、高差≤2×pet_h、target_x 夹可行域） |
| worker | pet_window.py | `seg_owner/cur_owner/_owner_rect` 归属态；`_bind_seg` 统一绑定点（站定/改绑/落位/攀爬完成）；`_follow_owner` pause 每拍跟随（忙时/聚焦/反应期生效，拖拽不跟随，失效→fall，写盘节流 0.5s）；`_begin_descend`（时长=距离/600px/s 下限 0.22s）；climb/descend 合并插值分支+target_x 横移+每帧 `_vertical_seg_alive` 失效当帧转 fall；`_plan` 掷大数轴骰盘（[0,10000) 一次 randrange：上爬领地 400/下爬领地 400/其余没想法，R26-5c 主人裁定）；walk 抵端点 `_try_adjacent_hop`（ADJACENT_CHANCE=0.5，落地强制 pause+确认）；descend 演 jumping 行；reload 读两新键；_plan 决策前按需重建地面（防跟随挪位后旧快照误判） |
| 宿主 | config.py | PET_DEFAULTS +follow_window/vertical_roam（显示类，缺键补齐走官方 writer）；validate bool 分支；REQ_PET_KEYS 自动派生覆盖 |
| 宿主 | plugin.yaml | pet.properties 两键 schema（Settings 面板可见） |
| worker | settings_window.py | 桌宠页「跟随窗口」「垂直游走」两卡片 + _apply_view/_rollback 同步 + 本地默认表对齐 |
| 测试 | tests/test_ground_motion.py | 51 断言（§11.6 逐条映射见下） |
| 文档 | README/CONTRACT §7 | 功能节/设置页表/键表/测试清单/契约同步 |

**验收断言映射（§11.6 → test_ground_motion，51 断言全绿）**：
1 遮挡修复（负原点 A 盖 B 剔除/B 在顶保留/正坐标全等/默认形状不变）；
2 归属（边沿 owner=HWND/屏底 None/逻辑矩形换算）；3 跟随（dx 平移/底沿贴线/
线段换键/x 夹域/忙时仍随/拖拽不随/owner 失效落体/关闭哨兵零移动/_step 集成）；
4 下降（最近优先/无阻挡直落/无更低 None/部分重叠拒绝/站立排除/动画落位/
单帧<120px/中途失效转 fall）；5 寻路（邻接选取/可行域/远间隙拒/超高拒/左邻/
端点跳转集成/落地回 pause/vertical off 零主动垂直）；6 配置（默认表/显示类/
validate/白名单/缺键补齐/显式 false 不碾/build_view）。

**全量回归**：既有 27 套件 + test_ground_motion = **28 套件全绿**（含真集成
冒烟）；真机探针复测 `BUG REPRO: False`（修复前 True）。

**测试坑两枚（如实申报）**：
- offscreen 屏仅 800×800：合成窗口 x>799 被裁、y<pet_h 的顶沿被头顶过滤——
  首版测试几何照搬真机 1536 宽坐标系导致 3 断言假失败，测试几何改屏内坐标
  后全绿（生产代码无改动，非功能缺陷）。
- walk 每帧 `_screen_geo` 夹带：测试线段域越出 offscreen 屏右缘时宠物被
  判"离开地面"提前 fall——同属测试几何问题，收窄到屏内后跳转链路完整走通。

**生效窗口期**：盘上代码已收口，但**正在运行的 worker 仍是旧代码**——
Gateway 重启（或 worker 被 supervisor 重拉）后新功能才上线。两新键缺省
true，重启后宿主 watcher 自动补齐进 config（官方 writer）。

### 11.9 R26-5 共振修复（主人实测反馈，2026-11-16 二轮）

**反馈**：浮空已修复、窗口跟随正常；但新下爬与既有游走/上爬疑似共振。

**回路诊断（实锤）**：判定函数无错，振荡出在决策层三个闭环——
1. **上爬↔下爬永动机**：站窗底沿身体穿同他窗顶沿 → rule7 被动爬上去 →
   plan 掷 35% 下爬找回窗底 → 落地 → confirm 又被动爬。对照取证：场景 A
   （窗底+更高他窗顶穿身）假时钟 150s 仿真，**修复前 49 次垂直往返
   （≈3s 一振），修复后 3 次**。
2. **confirm 与 plan 双触发上爬**，节奏叠乘。
3. **端点 hop 50% 往返**：右跳→落回 pause→走回来→左跳。

**修复（决策层，判定语义不动；垂直动作全走一套"共振记忆"闸门）**：
- `VERT_COOLDOWN_S=8.0`：任一垂直转移（爬/下爬/端点跳含同高平移）落地后
  全局冷却 8s，期间禁一切主动+被动垂直动作（rule7 被动爬同样受闸）。
- `REVERSE_BAN_S=30×min(次数,4)`：同一对线段 A→B 落地后，反向 B→A 按时数
  递增封禁（30/60/90/120s），A↔B 振荡被压成分钟级偶发而非秒级共振。
- **同窗顶↔底沿互降排除**：主动下爬目标与当前线段归属同一窗口 → 直接拒绝
  （窄悬浮窗顶底穿身是最稳的共振燃料）。
- 半途中止（fall）的垂直转移不登记；`vertical_roam=false` 关闭态保持旧行为
  不受闸（R26-3 语义不变）。转移键用线段取整几何，跟随挪位后仍能对上。
- 测试：test_ground_motion 追加 6 断言（场景 A 有界+仍存活、场景 B 同窗排除
  +对照 sanity、场景 C 冷却拦截+恢复），57/57；全量 28 套件回归全绿。

### 11.10 R26-6 接触概率掷 + 屏顶空间剔除（主人裁定三轮，2026-11-16）

**裁定原文**：没必要见梯子就爬——碰到合法地面线段随机数决定爬不爬/下不下，
接触/持续接触时间过长都算一次掷；爬上去离屏顶空间不足站一个桌宠的线段自动剔除。

**实现**：
- `CONTACT_CLIMB_CHANCE=0.4`（内置常量，非配置键）：被动 rule7 爬、主动上爬、
  主动下爬三处候选过闸后各再掷一次；掷不中安静站着（下次确认/决策再掷）。
  每次确认计时器到期=一次接触（持续接触天然按 0.75-1.25s 节奏反复掷），
  无需单独计时器。与 R26-5 闸门分层：闸门管"能不能"，概率管"想不想"。
  `vertical_roam=false` 不受概率影响（旧行为必爬哨兵不变）。
- 屏顶空间剔除升级：原 BUG1 过滤只查 y（seg_y-pet_h>=屏 top），现加 x 域
  检查（站上去整个身体须在某块屏内），且 `_standable` 对**全部屏幕**成立
  即保留（`build_ground_segments(screen_geos=[...])`，worker 传
  QApplication.screens() 全量几何；缺省单屏=旧行为全等）。负原点窗口被
  屏顶裁剪后的原始顶沿（如副屏 y=-100）按裁剪前坐标补判，站得进就保留。
  剔除在注册源头生效——爬/跳/落体候选都不会再选到站不进去的线段。
- 测试：test_ground_motion 追加 8 断言（概率 0/1/中间三态×爬与下爬、
  头顶剔除、多屏保留、单屏缺省剔除），65/65；test_ground rule7 断言组
  钉死概率 1.0 保持机制语义（概率行为由 motion 套件专测）；全量 28 套件全绿。
- 生效窗口期同 §11.8：重启 Gateway 后上线。

### 11.11 R26-5b 评审轮修复（独立评审揪出 P0，2026-11-16）

**评审来源**：独立评审子代理全量审查（46 次工具调用），逐条亲手验证后采纳。

**P0（已修）：落地登记的 identity 匹配在生产是死代码**——
`_consume_vert_pending` 用 `p[1] is seg` 匹配，而 climb/descend 期间
`_vertical_seg_alive` 每帧 `_refresh_ground` 重建地面，`climb_seg` 按值
重绑成新 tuple 对象 → `is` 恒 False → **冷却/封禁从不生效**。实测
（生产同款刷新 + 150s 假时钟）：修复前 55 次转移且 `_note_vert` 零调用，
一行修复（`is` → `is or ==`）后 3 次且登记正常。
§11.9 报的"修复后 3 次"当时出自冻结 tuple 的测试装置，装置保真度不足，
本节修正为生产实测口径。

**P1（已修）**：
- 同窗排除对称化（P1-4）：被动 rule7 爬与主动上爬也拒绝爬向同 owner
  线段（窄悬浮窗"窗底沿自动爬上自己窗顶沿"是共振燃料）；owner=None
  （屏底/任务栏）不参与判定，防两个无主线段误判同窗。异 owner 不误伤。
- pair 计数衰减（P1-3）：`PAIR_DECAY_S=300s` 空置重置 + pair 条目纳入
  防胀回收——防长跑数月后全库永久 120s 封禁、字典无界增长。
- 死代码 `_vert_last` 删除（从未读取）。

**P1（已裁定，R26-5b 骰盘重造）**：`_plan` 双骰穿透——上爬候选存在但掷不中（或被
封禁/同窗拒）时继续掷下爬，两候选并存时实际垂直概率 ≈26.6%（名义 14%），
且 docstring 写"无则下爬"与实现不符。**主人裁定（2026-11-16）：改大数轴
骰盘**——`randrange(10000)` 一次掷骰，上爬领地 [0,400)、下爬领地 [400,800)、
其余 9200 格=临时起意没想法；领地闲置（候选无）或被闸拦=本轮不动，不重掷
不穿透。`VERTICAL_CHANCE` 与主动路径的 `CONTACT_CLIMB_CHANCE` 双双退役
（`CONTACT_CLIMB_CHANCE` 只保留在被动 rule7 爬）。无穿透证明进套件：同
种子序列下"双候选并存"与"仅下爬候选"的 descend 触发次数**精确相等**
（旧穿透版必然不等）。

**P0 测试教训（固化）**：`lambda: w.ground_segs` 冻结同一批 tuple 恰好
掩盖 identity 匹配缺陷；`list(...)` 浅拷贝也抓不住。生产保真度测试必须
**每帧重建值相等的新 tuple**（已进套件：72 断言含 notes>0 + 有界 + 仍
存活三连），全量 28 套件回归绿。备份 `.bak-26p0`。

### 11.12 R26-5c 主动垂直决策改大数轴骰盘（主人裁定，2026-11-16）

**需求**（主人原话口径）：改得更像临时起意——"直接在一个很大数轴区间里
掷色子，第几位是几就做什么动作"，替代"先掷想不想→再找候选→再掷→没中
换下爬"的多层双骰链。

**实现**（`_plan` 垂直段整体重写）：
- 数轴 `[0, 10000)`：`PLAN_DICE_MAX`；上爬领地 `[0, 400)`
  （`CLIMB_TERRITORY_W=400`，4%）、下爬领地 `[400, 800)`
  （`DESCEND_TERRITORY_W=400`，4%）、其余 9200 格=什么都不做。
- 候选资格先行（find_climb_target / find_descend_target + 同窗排除），
  双双无候选则整段跳过、连骰子都不掷；有候选才 `randrange` 一次，落领地
  再过共振闸门（`_vert_gate_ok`），被拦=本轮没戏。
- 领地位置固定、宽度只在该候选存在时生效——"落空"包括候选不存在与被闸
  拦两种，行为一致：安静走漫步。
- `VERTICAL_CHANCE` 删除（生产唯一引用就是旧 `_plan`）；主动路径的
  `CONTACT_CLIMB_CHANCE` 退役，该常量只保留在被动 rule7 爬（confirm 计
  时器路径）。端点跳 `ADJACENT_CHANCE` 不变（单骰本无穿透）。
- 语义分层不变：闸门管"能不能"（冷却/封禁/同窗），骰盘管"想不想"。

**行为变化**：双候选并存时垂直动作总概率从旧版实际 26.6% 降为 8%
（4%+4%，上下互斥、独立、无穿透）——平均一二十秒一个"临时起意"，
符合"没必要见梯子就爬"的既定基调。

**测试**（套件 76 断言）：无穿透证明——同种子序列下"双候选"与"仅下爬
候选"的 descend 触发次数**精确相等**（每迭代重置冷却/ban 后 `seed(i)`，
判定只看同一颗骰子的落点；旧穿透版上爬掷不中再掷下爬，序列错位必不等）；
领地清零永不降 / 领地满格必降；4% 统计落位断言；既有 50/50 满格档保持
A↔B 振荡有界+存活的对照仿真语义。全量 28 套件回归绿；生产同款刷新探针
150s 3 次转移不变。

### 11.13 R27 子代理忙线监听（主人点名，2026-11-16）

**需求**（主人原话口径）：有办法让它监听子代理吗？某个会话只有子代理在
工作也算是在工作。

**机制裁定（源码实证）**：`tools/delegate_tool_registry.py` 的模块级
`_active_subagents` 表就是宿主内部权威登记——子代理 spawn 时 `_run_single_child`
注册（status=running，注册即真跑、排队不算）、结束注销，表寿命精确等于
子代理真实运行期；后台派发（父 turn 已结束）后子代理仍挂表。插件与
gateway 同进程（既有 `gateway.run._gateway_runner_ref` 就是 in-proc
import），直读 `list_active_subagents()` 零成本。跨进程宿主
（dashboard --isolated 等）派发的子代理不可见——与权威源的既有口径
一致（那些宿主的会话本就不进本表），不算退化。

**实现**（改 5 文件）：
- `statemachine.py`：新字段 `subagents`（每拍由轮询更新）；
  `set_authority(items, subagents=0)`；`derive()` 无活跃会话且子代理>0 →
  "acting"（位于 error 30s 短窗之后、压过 happy/listening/idle——工作是
  持续事实，情绪是瞬时展示）；`active_count()` 会话数为 0 但子代理在跑
  → 按 1 计（灯橙）。
- `__init__.py` `_poll_authority`：`subagent_busy` 开才读；import 失败
  静默归零（不阻断权威源主路径）；事件兜底态（authority 不可达）计数
  字段仍每拍更新，两分支统一生效。
- `config.py`/`plugin.yaml`/`worker/settings_window.py`：`subagent_busy`
  键（修改类默认 false，DIY 规范）+ Settings 面板"托盘与总线"组新卡片。

**语义细节（有意设计）**：
- 单会话在干活 + 其子代理在跑 → 保持该会话 phase（acting/thinking），
  子代理**不虚增计数**——子代理是父会话工作的一部分，单会话不升 busy；
- 会话全空闲 + 子代理在跑 → acting（桌宠跑动、灯橙）；
- error 短窗（30s）优先展示错误情绪，窗过后子代理顶上。

**测试**：test_statemachine.py 新增 14 断言（acting/计数/灯橙/恢复/事件
兜底分支/单会话不虚增/双会话照旧 busy/error 窗优先/listening 让位），
全绿；宿主解释器实测 `from tools.delegate_tool_registry import
list_active_subagents` import 链连通；全量 28 套件 fail=0。
**生效窗口期**：Gateway 重启后上线（消费线程代码在宿主进程内，改盘上
代码不触运行中进程）。备份 `.bak-27sub`。

## 附录：取证锚点

- `hermes --version` → v0.23.1 (2026.9.24) stable, local 3b4215785882，
  Install dir `<HERMES_HOME>\hermes-agent`
- **GUI 选型实测表（2026-11-13，160×160 无边框置顶窗，同解释器系 cp314）**：

  | 库 | RSS 增量 | 安装占用 | 真透明 | 子线程 | 裁定 |
  |---|---|---|---|---|---|
  | **pygame-ce** | **+12.3~13.9MB** | 9.4MB | ✅（§2.6 配方实锤） | ✅ | **定案** |
  | tkinter | +15.9MB | 0（内置） | ✅ -transparentcolor | ❌ Tcl apartment 崩 | 备选 |
  | wxPython | +18.2MB | 71MB | 别扭 | — | 出局 |
  | customtkinter | +26.8MB | +0.3MB | 继承 tk | ❌ | 冗余出局 |
  | PyQt6/PySide6 | +42.9MB | 82/655MB | ✅ | — | 体量不配 |
  | DearPyGui | +55.6MB | 7MB | ❌ | — | 出局 |
  | pyglet | +131.4MB | 1MB | 难 | — | 出局 |

- **渲染改案改案（2026-11-13）托盘渲染层 = PySide6-Essentials + PySide6-Fluent-Widgets**：
  作者裁定换掉 pystray（原生 Win32 菜单观感差）。实测三档（同机，进程树 RSS/USS）：
  pystray 39.5/19.9MB；Qt+QSS 52.7/18.9MB；**Qt+Fluent 134.2/61.8MB（选定）**。
  磁盘 side-env：pystray 17MB vs Qt+Fluent 713MB（作者口径：不怕装怕跑；且判定
  "M1 若走 Qt 单进程则 Qt 运行时只付一次"）。分发角度选 PySide6（LGPL）弃 PyQt6（GPL）。
  **勘误：此前附录判 Qt"cp314 wheels ✅"依据要更正**——wheel 实为 `cp310-abi3`
  （3.10+ 通用 ABI，cp314 依然可用，结论不变但机制写清楚）。Qt worker 契约与
  pystray 版逐字一致（--meta/state.json/lease/EOF+owner 双命锁/DETACHED 逃逸），
  Gateway 侧零改动。RoundMenu 坑：parent 不能传 QSystemTrayIcon（非 QWidget），
  传 None；select.select 在 Windows 不支持 stdin 管道，命锁维持阻塞线程+os._exit。
- **托盘实测（2026-11-13）**：pygame-ce **无任何托盘/通知 API**（全模块 dir + `_sdl2`
  枚举 + 版本 2.5.8/SDL 2.32.10 三重确认）。pystray（纯 py，~0.2MB）Windows 原生
  `_win32` 后端，daemon 线程内 `run()/visible/stop()` 全链路实测干净。
- petdex 格式实样（本机 `<HERMES_HOME>/pets/`）：`pet.json` 含
  `id/displayName/description/version/spritesheetPath`，帧图 `spritesheet.webp`；
  官方加载器 `agent/pet/{constants,manifest,store}.py`（M4 适配层先读它）。
- tkinter 实测（2026-11-13）：Gateway 同解释器 `-c "import tkinter; r=tkinter.Tk();
  print(tkinter.TkVersion, r.winfo_screenwidth())"` → `9.0 1536`
- 事件钩子系统：`gateway/hooks.py`（`$HERMES_HOME/hooks/<name>/HOOK.yaml+handler.py`，
  事件：gateway:startup, session:start/end/reset, agent:start/step/end, command:*）
- 探针与脚本存档：`Data\cache\scratch\gui_foot2.py`（RSS 模板）、
  `pg_transparent_spike.py`（colorkey 端到端）

---

---

## 脱敏记录（公开版 v1.0.0，2026-11-14）

本公开版由私有母版脱敏生成。母版按迭代轮次逐条记录裁定与现场取证；公开版保留全部
设计契约、实测数据与技术裁定，仅移除/替换下列私有内容（按类别描述，不复述字面值）：

- **私有称呼**：母版中对所有者的私有敬称统一为「作者/用户」；私有授权表述改为中性的
  「特许」；内部协作角色代号（主协调方/子任务方/外部协作者）替换为中性表述。
- **中文轮次计数**：母版以中文数字标记迭代轮次，公开版收敛为按主题
  的变更史与功能性标签，不再以轮次编号叙事。
- **开发机绝对路径**：所有盘符绝对路径（数据家目录、开发项目目录、解释器/环境路径）
  替换为 `<HERMES_HOME>` 相对表述或「本地调查档案，未随仓库发布」。
- **私有母版头部编年史**：母版文首约 250 行的逐轮裁定流水账蒸馏为「变更史（蒸馏）」，
  技术结论以正文各节为准。
- **过时交接存档**：母版文末【交接现状】节（整体重写前的旧代码未解问题快照，已被
  A1-A4 根除取代）整节移除，不随仓库发布。
- **个人账号与素材标识**：测试数据中的真实账号显示名替换为中性占位（TestUser）；
  具体宠物 slug 示例保留为通用说明（取已安装库存）。
- **姊妹项目本机路径**：whale-widget 移植项目与 gateway-dashboard 的本机相对路径
  替换为项目名/「未随仓库发布」表述。
- 插件作者署名中的私有 Agent 名替换为「AI collaborator」（见 plugin.yaml）。

公开版仅使用 `<HERMES_HOME>` 相对路径、协议名称与匿名测试编号，不包含本机绝对路径、
个人配置、真实会话内容或 API 凭据。
