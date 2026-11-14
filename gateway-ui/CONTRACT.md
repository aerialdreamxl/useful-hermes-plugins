# gateway-ui 内部接口契约 v1（宿主侧 ⇄ worker 侧）

> 本文件是宿主包（Gateway 进程内）与 worker 包（side-env 独立 Qt 进程）之间的
> 唯一接口权威。两侧代码与测试都以此为准。需求权威 = 本目录 PROMPT.md。

## 0. 命名与落位（终态）
- 插件 id：`custom-plugins/gateway-ui`；描述口径「适用于 Hermes Gateway 的 Qt GUI 组件」。
- 总线目录：`<HERMES_HOME>/cache/gateway-ui/`（`HERMES_HOME` = Hermes 数据家目录）。
- side-env 名：`gateway-ui-qt`（gen-hash 不含环境名，目录改名零重装）。
- **测试纪律：一切测试只用 `<HERMES_HOME>/cache/scratch/` 下的隔离总线目录，绝不读写
  真实总线 `cache/gateway-ui/`。绝不改 config.yaml/.env，绝不杀现存 gateway/worker
  进程。pets/ 素材只读。**

## 1. 文件布局
```
gateway-ui/
  plugin.yaml  __init__.py  config.py  statemachine.py  bus.py  supervisor.py
  worker/
    __init__.py(空)  main.py  tray.py  pet_menu.py  pet_window.py  ground.py  sprites.py
  tests/
    test_statemachine.py test_config.py test_supervisor.py      # 宿主 python 跑
    test_ground.py test_pet_menu.py test_spawn_pos.py test_sprites.py test_worker_lease.py  # side-env python 跑
  README.md  deploy.py  CONTRACT.md(本文件)
```
- 宿主侧模块间用**包相对导入**（`from . import config`）——官方加载器以包形式加载
  （spec_from_file_location + submodule_search_locations，plugins_loader.py:834 实锤）。
- worker 侧：`main.py` 顶部 `sys.path.insert(0, str(Path(__file__).resolve().parent))`
  后用**平级导入**（`import pet_window`）。生产 spawn 用 `python -I`（隐含 -P，脚本目录
  不进 sys.path——桌宠落地事故根因，必须显式 insert）。
- worker 各模块**不得 import 宿主侧模块**（config/bus/supervisor），反之亦然。
  共享的只有本契约的文件格式。

## 2. 解释器
- 宿主 python（跑宿主测试）：Hermes 自带解释器，`<HERMES_HOME>/tools/python-3.14*/python.exe`
  （glob 展开取真路径，禁止手拼）。
- side-env python（跑 Qt 测试/生产 worker）：
  `<HERMES_HOME>/environments/gateway-ui-qt/gen-*/venv/Scripts/python.exe`
  （测试里用 glob `environments/gateway-*-qt/gen-*/venv/Scripts/python.exe` 更稳）。
- Qt 测试脚本内必须 `os.environ["QT_QPA_PLATFORM"] = "offscreen"`（在 import PySide6 之前）。
- Windows git-bash 下给原生程序的路径参数用盘符正斜杠直写（MSYS 会弄坏 /d/... 参数）。

## 3. tray_meta.json（宿主写，worker 只读；每次 spawn 前重写）
```json
{
  "gateway_pid": 1234,              // 宿主进程 pid（命锁 owner）
  "spawn_token": "hex32",           // 每次 spawn 唯一（uuid4().hex），租约回传核对
  "bus_dir": "<home>/cache/gateway-ui",
  "state_path": ".../state.json",
  "log_dir": "<home>/logs",
  "hermes_home": "<home>",
  "repo": "<home>/hermes-agent",
  "version": "0.23.1",
  "pet": {"enable-pet": false, "slug": "", "scale": 0.7, "roam": true, "topmost": true},
  "restart_cmd": ["<python>", "-m", "hermes_cli.main", "gateway", "restart"],
  "stop_cmd":    ["<python>", "-m", "hermes_cli.main", "gateway", "stop"],
  "cwd": "<repo>",
  "env": {"PATH": "...", "SYSTEMROOT": "...", "USERPROFILE": "...", "APPDATA": "...",
          "LOCALAPPDATA": "...", "COMSPEC": "...", "PYTHONPATH": "<repo>"}
}
```
worker 启动命令行：`<env_python> -I -X utf8=1 <plugin>/worker/main.py --meta <meta_path>`
（stdin=PIPE 真实管道，stdout/stderr→bus/worker_stderr.log 追加，spawn 前 >2MB 截断）。

## 4. tray_lease.json（worker spawn 写一次静态内容；心跳=文件 mtime，5s utime；启动自检读）
```json
{"pid": 5678, "owner_pid": 1234, "spawn_token": "hex32"}
```
**硬件友好写盘**：JSON 无 ts、不每秒重写（省数据页+rename 目录日志）；新鲜度一律看
文件 mtime（`os.utime` 只动 inode 元数据）。读侧 mtime 优先、旧版 ts 字段仅作过渡回落。
**单实例协议（A3 根除）**：
- worker 启动自检：租约新鲜（now-mtime < 8s）且 pid 存活（OpenProcess 0x1000）且 pid != 自己
  → **无条件 self-exit**（不再区分 owner——旧版"同宿主陈旧租约自家接手"规则放行了双 worker）。
  陈旧租约（>=8s）= 持有者已死/卡死 → 直接接手覆写。
- 启动后立即写一次租约（QApplication 建立后、tray.show() 前），tick 循环每 5s `touch_lease`
  utime 续心跳（内容不变）。
- supervisor 判活 = 进程活着 **且** 租约新鲜(<8s) **且** lease.spawn_token==本次 token
  **且** lease.pid 存活（OpenProcess）。**venv 蹦床坑（实测）**：side-env python.exe 是
  launcher，Popen.pid≠真实解释器 pid——lease.pid 是 worker 自报 os.getpid()，为权威身份，
  绝不与 Popen.pid 比对。进程活但租约陈旧 >12s = 卡死 → stdin EOF 命锁+terminate→重拉。
- worker 自带看门狗线程：tick 每拍更新 last_tick_ts；看门狗每 5s 检查，
  now-last_tick > 20s → trace("WATCHDOG: event loop wedged") + os._exit(3)（A4 根除：
  卡死 worker 自毁，supervisor 心跳判活后重拉，而不是永久装死）。
- 命锁（保留旧契约）：stdin readline()=="" → os._exit(0)；owner_pid OpenProcess 失败
  （2s 轮询）→ os._exit(0)。Windows select 不支持管道，用阻塞线程。

## 5. state.json（宿主 bus.py 写，worker 只读；§5 姊妹契约，既有字段不得增删改名；事件反应纯增量 notice）
```json
{
  "pet_state": "idle|listening|thinking|acting|busy|happy|error",
  "active_sessions": 0,
  "active_source": "authority|events",
  "verbose": false,
  "writer_pid": 1234,
  "boot_ts": 1794630855.1,
  "last_event_seq": 42,
  "notice": null | {"seq": 1, "kind": "done|failed|stopped|await_input",
                    "ts": 1794630867.47, "platform": "desktop|tui|cli"},
  "tray": {
    "light": "green|orange|red",
    "tier": "暂无异常|轻微波动|需要留意|更新可用！",
    "degrade": null | {"from": "host缩写", "to": "host缩写", "from_url": "...",
                       "to_url": "...", "to_provider": "...", "ts": 1794630867.4},
    "degrade_window_s": 300,
    "version": "0.23.1",
    "update_available": false,
    "bus": true
  }
}
```
- 原子写 tmp+rename，**内容无变不重写**（签名去重），空闲期每 5s `touch_state` utime 维持
  读侧 mtime TTL。uptime 由消费侧自算 `now - boot_ts`（boot_ts 为常量锚；旧
  gateway_uptime_s/updated_ts 每拍变化字段已移除，updated_ts 本无消费侧）。
- 写者租约 `writer_lease.json` = 静态 `{"pid":1234}`（pid 变化才重写）+ 每 2s utime 心跳。
- worker 读端校验（写者租约规则保留）：state.writer_pid 必须匹配 writer_lease.json
  （租约文件 mtime < 4s）才接受；不匹配=幽灵写者 → 丢弃 + vlog REJECT。
- state 文件 mtime > 8s（STATE_TTL）→ 灯灰（失联）。
- events.jsonl：`{seq,ts,kind,session_key?,platform?,tool?,model?,ok?,base_url?}`，
  kind ∈ inbound|turn_start|post_api|tool|turn_end|error|degrade|await_input；滚动保留 2000 条。
  **字段白名单：绝无消息正文/prompt/密钥。**

### 5.1 notice 字段（事件反应：完成/失败/等人类输入；纯增量，dashboard 忽略未知字段不受影响）
- `notice` = 最后一个会话事件通知 `{seq,kind,ts,platform}`，无则 `null`。
  **只带这四字段**——绝无消息正文/命令内容/密钥（approval 的 command/description 不入总线）。
- `seq`：notice 单调序号（独立于事件 last_event_seq，从 1 起）；worker 按 seq 去重。
- `kind` 派生（宿主 statemachine，平台白名单门控）：
  - `done` ← on_session_end completed=True；
  - `failed` ← on_session_end failed=True（且非 interrupted）；
  - `stopped` ← on_session_end interrupted=True（**用户 /stop 主动打断，不算失败**，
    优先于 failed）；
  - `await_input` ← pre_approval_request（source=approval）或 pre_tool_call 截
    tool_name=="clarify"（source=clarify）。
- `platform` 白名单 = `desktop|tui|cli`（本机交互面）。实锤：
  desktop/tui = tui_gateway/server.py `_resolve_session_platform()`；
  cli = hermes_cli/cli_agent_setup_mixin.py platform="cli" + gateway LOCAL→"cli"。
  **其余平台（telegram/discord/qqbot/api_server/cron/subagent…）一律不产生 notice。**
- 幂等：同 turn_id 的 on_session_end 重发只产生一条 notice；await_input 同
  (source,session_key) 短窗（30s）压一条（coalesced 连发）。
- await_input 平台归因（宁漏报不误报）：事件自带 platform → session→platform 表
  （turn_start 实锤）→ gateway session_key 的 platform 段（agent:ns:platform:…）。
  surface="gateway" 不当平台用（背后可能是消息平台）。
- worker 消费：`worker/notices.py` NoticeTracker.feed(state)→新 notice（seq 去重、
  启动首轮不回放陈旧、过期>120s 忽略）→ reaction 映射 done/failed/await_input/stopped
  → PetWindow.play_reaction(kind) + tray.TrayFlash.start_flash(kind)。
  开关 `pet.event_reactions`（默认 true，缺键不落盘 A1）。最终 tick 接线见 §7。

### 5.2 宿主 hook 清单（事件反应 7→9）
| hook | 回调 | 入队 kind | 用途 |
|---|---|---|---|
| pre_gateway_dispatch | _on_inbound | inbound | listening |
| pre_api_request | _on_pre_api | turn_start | 活跃记账+降级基线+session→platform |
| post_api_request | _on_post_api | post_api | 活跃刷新+降级 |
| post_llm_call | _on_post_llm | turn_end(ok=True) | 旧 happy 瞬时（无结局标志，不产生 notice） |
| pre_tool_call | _on_pre_tool | tool (+await_input if clarify) | acting 阶段；clarify 截获 |
| api_request_error | _on_api_error | error | 红灯 |
| agent_loop_stopped | _on_loop_stopped | turn_end(ok=False) | 旧 error（无结局标志，不产生 notice） |
| **on_session_end** | _on_session_end | turn_end(completed/failed/interrupted) | **事件反应：per-turn 结局 notice** |
| **pre_approval_request** | _on_pre_approval | await_input(source=approval) | **事件反应：等人类审批 notice** |
- 所有回调只 `_put` 入队 O(1)，绝不阻塞 asyncio 主循环；派生/门控/去重全在消费线程状态机。

## 6. settings 总线协议（设置界面 F7：官方 writer 通路，pet_cfg.json 退役）

> **作者特许（设置界面）**：设置功能模块的修改直接落真实 config.yaml，但**必须走官方
> 接口**（`ctx.set_config` → `hermes_cli.plugins_state.save_plugin_setting`：跨进程
> `.config.yaml.lock` 字节锁 + ruamel 保注释 + 原子替换 + managed 拒绝）。
> worker 进程**绝不打开 config.yaml**——官方接口住在 Gateway 进程的 hermes_cli 里，
> side-env 没有它。故通路 = worker 发请求文件 → 宿主调官方 writer → 回执。
> **pet_cfg.json 中转退役**：config.yaml 是唯一事实源；旧文件留盘不读不写（死文件，
> 作者可手删）。A1 铁律不变：默认值永不自动落盘，只有用户显式操作才写。

三个文件（全在总线目录，原子写 tmp+rename）：

### 6.1 settings_req.json（worker 写，宿主消费）
```json
{"id": "hex8", "ts": 1794630867.47, "key": "pet.scale", "value": 1.0,
 "source": "settings|petmenu|petwindow"}
```
- `key` 白名单（宿主校验，越界拒绝）：顶层 5 键 `tray_enabled/bus_enabled/
  degrade_window_s/verbose_log/package_index_url` + pet 块 5 键（前缀 `pet.`）
  `enable-pet/slug/scale/roam/topmost`。
- `id`：worker 生成（uuid4().hex[:8]），回执按 id 对账；同 id 重复请求幂等
  （宿主记最近 64 个已处理 id）。
- 宿主 watcher 0.25s 轮询 mtime：校验 key/value 类型与范围（scale 钳 0.1-3.0、
  degrade_window_s 钳 60-1800、bool 强转、slug 限已安装库存或空串、url 限
  http(s)）→ 合法则 `ctx.set_config(key, value)`（官方 writer）→ 写回执；
  非法则回执带 error，不写 config。
- **value 校验失败 ≠ 崩溃**：宿主照常运行，仅该请求被拒。

### 6.2 settings_ack.json（宿主写，worker 读）
```json
{"id": "hex8", "ok": true, "key": "pet.scale", "value": 1.0,
 "error": "", "ts": 1794630867.5}
```
- worker 每 tick 读；`ok=true` → 更新内存 cfg + 刷新 UI；`ok=false` → InfoBar
  报错 + 卡片回滚到快照值。设置窗口据此给"已保存/保存失败"反馈。
- 只保留最近一条回执（单槽）；worker 发请求前记 pending id，收到匹配回执即清。

### 6.3 settings_view.json（宿主写，worker 读；官方 get_config 快照）
```json
{"ts": 1794630867.5,
 "top": {"tray_enabled": true, "bus_enabled": true, "degrade_window_s": 300,
         "verbose_log": false, "package_index_url": "https://..."},
 "pet": {"enable-pet": true, "slug": "jue", "scale": 0.7, "roam": true, "topmost": true},
 "explicit": {"top": ["tray_enabled", "..."], "pet": ["slug", "..."]}}
```
- 宿主每拍（state 写盘同拍）用官方 `ctx.get_config` 出快照；`explicit` 列出
  config 里真实存在的键（缺键不在列）——设置页据此显示"默认值"角标，
  enable-pet 三态语义保留（缺键≠false，运行时默认 false 但显示"未设置"）。
- worker 每 tick 按 mtime 读：pet 运行时真值 = 快照 pet 块（config 唯一事实源）；
  设置窗口卡片初值 = 快照；宿主侧改动（作者手改 config/其他入口）自动同步到 UI。
- **左键单击托盘 = toggle 余额气泡（托盘左键改绑，与桌宠左键同款；纯运行时，
  绝不发 enable-pet 请求）——A2 根除保留。** 桌宠可见锚头顶、不可见锚托盘图标。

### 6.4 pet 菜单/桌宠窗口写路径改道
- PetMenu/PetWindow 的 `set_cfg(key, value)` 回调 → worker `request_setting(key, value)`
  → settings_req.json（不再写 pet_cfg.json）。菜单勾选即时乐观更新内存，
  回执 ok 确认 / 失败回滚。
- worker `reload_pet_cfg()` 改名 `reload_settings()`：读 settings_view.json
  （mtime 驱动），不再读 pet_cfg.json。meta.pet 仅作首启快照缺失时的回落。

## 7. worker 行为契约（Qt 侧）
- 托盘：QSystemTrayIcon + RoundMenu（parent=None，托盘非 QWidget）。图标右下状态灯
  绿/橙/红/灰。tooltip：`gateway-ui · <pet_state> · <tier> | uptime XhYYm | 最近降级...`。
- 托盘菜单扁平 8 项（顺序固定）：
  1 `Hermes Gateway v{version}`（灰）2 `状态监控: {tier}`（灰）3 `桌宠▸`（共享 PetMenu）
  4 `打开日志`（explorer log_dir，目录缺失禁用）5 `设置`（**设置界面启用**：打开
  SettingsWindow，已开则激活置前）6 `设置(VSCode)`（code 探测不到禁用）7 `重启` 8 `退出`。
  重启/退出 = DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP 逃逸壳执行 meta 里的
  restart_cmd/stop_cmd，showMessage 即时反馈，8s 后 app.quit()。**worker 绝不 spawn worker。**
- **SettingsWindow（设置界面 F7，worker 进程内，不新起进程）**：FluentWindow 左侧导航
  三页（常规/桌宠/关于），与托盘/桌宠共用同一 QApplication。单实例：已开则
  activateWindow+raise；关闭即销毁（预览定时器一并释放）。
  - 常规页：SwitchSettingCard tray_enabled（**关闭前 MessageBox 二次确认**：会连
    设置窗口自己一起停掉，因窗口住在 worker 里）；SwitchSettingCard bus_enabled；
    RangeSettingCard degrade_window_s 60~1800s；SwitchSettingCard verbose_log；
    ComboBoxSettingCard package_index_url（清华/阿里/官方+自定义 LineEdit）。
  - 桌宠页：SwitchSettingCard enable-pet；ComboBoxSettingCard slug（实时扫描
    sprites.installed_pets 库存）；RangeSettingCard scale 0.1~3.0；SwitchSettingCard
    topmost；SwitchSettingCard roam；**活体预览**：内嵌当前宠物 idle 动画
    （复用 sprites.PetSheet），换宠/拉大小即时变。
  - 关于页：版本/更新状态/uptime（读 state.json）；PushSettingCard 打开日志目录/
    打开总线目录/VSCode 打开 HERMES_HOME。
  - 每张卡片副标题按 DIY 规范写"改什么/会引发什么"，与 README 口径一致。
  - 读写全走 §6 总线协议：初值=settings_view 快照；改动→settings_req→等回执
    （ok=InfoBar"已保存"，fail=InfoBar 报错+卡片回滚快照值）；窗口开着时按
    settings_view mtime 自动刷新（blockSignals 防回写环）。
  - 卡片控件信号必须 blockSignals 包裹程序化赋值，防"刷新→触发→再写"回环。
- 菜单弹出位置：左下角对准鼠标（sizeHint 换算 y-h，屏顶翻转/屏右钳制）。
- PetMenu（托盘槽位与桌宠右键**同一实例**）：一级 显示桌宠✓/游走（含爬窗）✓/置顶✓
  （CHECK 对勾）；二级 切换宠物▸{Petdex▸{库存 RADIO 圆点} | Shimeji/DaoCute/eSheep/
  DesktopMascot（未支持禁用位）}、大小▸{小0.5/中0.7/大1.0 RADIO}。
  QAction 生于父菜单永不搬移；aboutToShow 只刷 checked；库存变化只重建自己的叶子。
  驻留过滤器装 view.viewport()（不是 menu 对象），view.itemAt().data(UserRole) 取项，
  勾选项 trigger() 后消费 release（禁止手动 setChecked 双翻转）；子菜单入口放行。
- 桌宠窗口：FramelessWindowHint|WindowStaysOnTopHint(topmost 可关)|Tool，
  WA_TranslucentBackground，petdex spritesheet 渲染。
- **精灵契约（mirror agent/pet/constants.py，整体重写修正）**：FRAME_W=192 FRAME_H=208，
  **FRAMES_PER_STATE=6**（官方 steps(6)；旧代码取 8 帧是错的），LOOP_MS=1100，
  空白尾帧裁切 alpha<=8，Codex 9 行/legacy 8 行自适应，方向跑行+镜像。
  sprites.py 镜像常量，tests/test_sprites.py 在宿主 python 里 import 官方
  agent.pet.constants 断言镜像一致（防漂移）。
- 地面系统（十八/菜单驻留七条规则，原样移植不得改判定）：线段制
  [(x1,y1,x2,y2),...]；仅 roam=true 时运行；Win32 枚举剔除（最小化/不可见/cloaked/
  自身 pid+HWND/owned/GA_ROOT 非根/桌面壳类/太小）；每窗口上下沿两条线段+头顶空间
  过滤（seg_y - pet_h >= 屏 top）；z 序遮挡剔除；并入屏幕下界+任务栏上界
  （Shell_TrayWnd/Shell_SecondaryTrayWnd）；确认计时器 0.75-1.25s 随机（拖动归零暂停、
  移动归零但每帧实时刷新判地面、与 Agent 忙闲无关）；自由落体 gravity=5200px/s²
  （子线段±2px 判定不掉，否则落正下方第一条）；攀爬在落体判定后（更高+穿过身体+
  **全包含**，CLIMB_DUR_S=0.22 线性插值无瞬移）；站立=底边贴线 GROUND_STAND_EPS=3
  （无 FEET_DROP_PX 偏移）；漫步目标夹 [x1, x2-pet_w]；无落点先夹 x 回屏内再落。
- 新建窗口一律屏内右下角地面（不读 pet_pos.json，屏内出生）；resize 路径（old_h>0）
  沿用存档+高度差微调，双向 clamp。拖拽落点/游走落点原子写 pet_pos.json。
- host 状态映射：idle→idle listening→waiting thinking→review acting/busy→run
  happy→wave error→failed；fall/climb→jumping 行；walk→方向跑行。
  at_rest = active_sessions==0；忙时掐断 walk 转 pause 演 host 动作。
- verbose：从 state.json.verbose 读；true 时 WK 前缀写 bus/debug.log、PET 前缀写
  bus/pet_debug.log（400KB 滚动 2000 行）；false 零写入。构造异常限频写 pet_error.log。
- worker_trace.log：常开。**main() 第一行（Qt import 之前）就 trace("boot pid=...")**，
  每个关键阶段（meta ok/lease check/app created/tray shown/tick start）打点，
  CRASH 全 traceback——A4 取证通道，启动期卡死必须留证。

## 8. 宿主侧契约（我负责，worker 侧不实现）
- config.py：三态解析（缺键≠false）；逐键补齐走官方 writer，但**补齐出来的默认值
  永不参与任何回写**。设置界面起 `validate_request(req, explicit, installed_slugs)`
  纯函数校验 settings_req（白名单 key + 类型/范围 + 幂等 id），`apply_request`
  调官方 ctx.set_config；`build_view(ctx)` 出 settings_view 快照（含 explicit 键表）。
- **settings watcher（设置界面，替代 pet_cfg watcher）**：0.25s 轮询 settings_req.json
  mtime → validate_request → 合法走官方 writer → 写 settings_ack；每拍（state 同拍）
  build_view 写 settings_view.json（仅内容有变才落盘，不空转）。
  **config.yaml 只有官方 writer 碰**；宿主绝不自己解析/文本替换 config 文件。
- statemachine.py：纯函数状态机（十四/十五/写者租约判据原样）：session_id 幂等记账、
  权威源 gateway.run._gateway_runner_ref()._running_agent_items()（连续 3 拍不可达退
  事件兜底）、阶段直读 agent._last_activity_desc、降级优先读 agent._provider_fallback_active
  （真失败切换链才置位，手动 /model 切换不置位）；权威源不可达时退回 base_url 端点启发式。
- supervisor.py：永不退出循环；tray_enabled=false 只杀 worker 不终止线程；
  心跳判活（§4）；失败指数退避 3→6→12→30s 封顶，成功心跳清零；**绝无永久放弃**。
- 更新检查：官方 hermes_cli.banner.get_latest_release_tag()（本地 tag，带缓存），
  与当前版本数值比较；900s 一次；fail-safe 查不到=不亮。
- 宿主门控（站立判定）：argv 含 gateway+run/start 或 gateway.run 已在 sys.modules，
  否则 register() 打一条日志即 noop。

## 9. 测试要求（两侧都必须实跑交结果）
- 旧 harness 断言全量移植且必须 PASS：ground18（31 断言）、menu_stay19（13）、
  spawn_pos20（8）、busy_light（24+6）。旧文件在 cache/scratch/ 同名 .py，可读可抄。
- 新增：A1 配置三态/不回写默认值、A2 左键不写配置、A3 租约无条件让位、
  A4 卡死判活+看门狗、sprites 6 帧+官方常量对齐。
- 每个测试脚本独立可跑，末尾打印 PASS/FAIL 汇总+逐条明细；FAIL 必须修到 PASS。
- 测试里的 PetWindow 构造用真实 pets 库存（hermes_home=`HERMES_HOME` 环境变量，
  slug 取已安装库存），只读。
## 10. balance 总线协议（余额气泡 BalanceBubble：查询归宿主，worker 只读缓存）

> **额度纪律 + Token Plan 实测 18-28s** 决定的架构：worker 点击气泡**零网络调用**，
> 只读宿主刷好的快照。宿主 balance watcher 周期拉取（默认 300s，可配 60-3600）+
> 手动刷新请求；binding 子进程隔离执行，崩溃/超时不拖垮其余 provider。

### 10.1 balance_view.json（宿主写，worker 只读；原子 tmp+rename）
```json
{"ts": 1794630867.5, "refresh_s": 300,
 "providers": [
   {"id": "deepseek", "name": "DeepSeek", "unit": "CNY",
    "ok": true, "error": "",
    "value": 12.34, "display": "¥ 12.34",
    "peak": {"is_peak": false, "label_peak": "峰", "label_valley": "谷",
             "next_boundary_ts": 1794661200.0, "server_authoritative": true},
    "extra": {"today": "今日已用 ¥ 0.50"},
    "lines": [{"text": "[用户名] 剩余月额度", "style": "title"},
              {"style": "bar", "fraction": 0.75, "text": "75%"},
              {"text": "Standard  距到期N天", "style": "info"}],
    "fetched_ts": 1794630800.0}
 ],
 "default_id": "deepseek"}
```
- `peak: null` = 该 provider 无峰谷概念，气泡峰谷行整行不渲染。
- `peak.next_boundary_ts`：下一次峰/谷切换的 epoch 秒（宿主算好；binding 只交规则
  或现值，倒计时由 worker 每秒本地重算，跨边界后 worker 用规则自行推进——规则见 10.3）。
- **规则透传**：view 的 peak 块须原样透传 binding 交的 `rule`/`windows`/`holidays`
  字段（worker 跨边界推进要用同一份规则，单一事实源 = worker/peak_rules.py，
  宿主经 importlib 复用同一文件，禁止两处各写一份规则逻辑）。
- 倒计时格式纯 HH:MM:SS；>24h 小时位累加（25h→25:00:00，**无 Nd 前缀**，作者修订裁定）。
- `extra`：可选一行小字（如今日已用）；缺省不渲染。
- `lines`（修订新增，可选）：结构化多行排版 `[{text, style}]`，style∈title/amount/info/bar。
  有 lines 且 ok=true 时气泡按 lines 顺序逐行渲染取代单行金额行（文本槽超宽缩字号不截断）；
  normalize 只透传 text 为串的条目并打码。**bar 行**（进度条修正：作者原型图第二行是进度条
  不是文本）额外带 `fraction`（0~1，normalize 钳位/非法归零），气泡画左条右百分比的圆角
  进度条（填充 fluent 蓝，过期变灰），text 为右侧百分比串。Token Plan 出厂适配器用它。
- `ok=false`：金额行显示 error 截断 24 字符；providers 空列表=气泡显示"未配置余额源"。
- worker 读取：mtime+内容签名双判（§6 同款）；无文件/损坏=气泡"等待宿主数据"。

### 10.2 balance_refresh_req.json（worker 写，宿主消费）
```json
{"id": "hex8", "ts": 1794630867.47, "provider_id": "" }
```
- provider_id 空=全量刷新；非空=只刷该 provider。宿主 0.25s 轮询 mtime+签名，
  幂等（最近 64 id），执行后不写回执——刷新结果体现在 balance_view.json 本身。
- **手动刷新有节流**：同 provider 距上次 fetch <10s 的请求忽略（防连点烧钱）。

### 10.3 Python Binding 协议（宿主侧 <home>/balance-providers/*.py）
```python
NAME = "DeepSeek"        # 必需：气泡标题
UNIT = "CNY"             # 必需
ID = "deepseek"          # 可选：缺省取文件名 stem
def fetch() -> dict:     # 必需：返回下述结构
    return {
      "ok": True, "error": "",
      "value": 12.34,               # 数值（可为 None 表示无数值型额度）
      "display": "¥ 12.34",         # 必需：格式化好的展示串
      "peak": {                     # 可选：None=无峰谷概念
        "is_peak": False,           # 当前状态（服务端字段优先）
        "next_boundary_ts": 1794661200.0,  # 可选：宿主直接用
        "rule": "ds_cn",            # 可选：内置规则名，宿主/worker 推边界
        "server_authoritative": True
      },
      "extra": {"today": "今日已用 ¥ 0.50"},  # 可选
      "lines": [{"text": "…", "style": "title"}]  # 可选：多行排版（10.1）
    }
```
- **内置规则 `ds_cn`**（DS 官方北京时间计费时段，实锤自 whale-widget.js:18083）：
  工作日 9:00-12:00 与 14:00-18:00 为峰，其余为谷；周末全谷；法定节假日全谷
  （节假日表 binding 可交 `holidays: ["2026-10-01", ...]`，缺省不判节假日，
  README 注明此局限）。边界候选 = 每日 {0,9,12,14,18} 时（北京时间）。
- 峰谷时段表**由 binding 自定义**：交 rule 名 / 直接交 next_boundary_ts / 交
  `windows` 列表 `[{"days":[1..5],"ranges":[[9,12],[14,18]],"tz_offset_h":8}]`
  三选一（优先级 next_boundary_ts > windows > rule）。
- 执行环境：**宿主 python -I 子进程**（stdlib only 起步；binding 需要第三方库时
  自行 sys.path 注入或走 ensure_environment，宿主不代管）。超时钳 10s；
  qianwen CLI 类慢源 binding 内部自设更长超时并声明 `SLOW = True`（宿主放宽到
  45s，且刷新走后台不阻塞 watcher）。stdout 只许一个 JSON（协议结果），
  stderr 进日志（脱敏：宿主对 error 串做密钥样式打码后才入 view/log）。
- **密钥纪律**：binding 自查 os.environ/.env；宿主配置面永不过明文；
  view/日志出现 `sk-` 样式串一律打码。
- 投放目录空/`balance.enabled=false` → watcher 不起、view 不写、worker 气泡入口禁用。

### 10.4 settings 白名单扩展（§6 增补）
- 新顶层键（全部修改类语义，缺键按默认读、绝不自动落盘）：
  `balance.enabled`(bool, 默认 **false**)、`balance.refresh_s`(int, 默认 300, 钳 60-3600)、
  `balance.providers_dir`(string, 默认 ""=用 <home>/balance-providers)、
  `balance.token_plan_edition`(string, 默认 "individual"，枚举 individual|team，修订新增)。
- REQ_KEYS 增 `balance.enabled/balance.refresh_s/balance.providers_dir/balance.token_plan_edition`；
  build_view 快照增 `balance` 块 + explicit.balance 键表。
- 设置页余额卡片组（开关/刷新周期/Token Plan 版本下拉/投放目录/打开目录）。
  edition 经 `HERMES_BALANCE_TOKEN_PLAN_EDITION` 环境变量注入 binding 子进程
  （run_provider extra_env kwarg；binding 只读 env，不碰 config.yaml）。

### 10.5 worker 交互契约（§7 增补）
- **左键单击桌宠本体**（press→release 位移 ≤6px，spike 实测定值 CLICK_MAX_PX=6）
  = toggle BalanceBubble；位移 >6px = 拖拽（既有行为不变，release 的确认计时器
  照旧）。气泡开着时单击本体=收起。**托盘左键同款 toggle**（取代旧显隐
  语义；桌宠不可见时锚托盘图标几何，A2 不变）。
- BalanceBubble：无边框透明 Qt.Tool 窗，~220px 宽，锚定宠物头顶上方 8px 居中；
  无头空间翻转到脚下；clamp 进屏。宠物移动（walk/climb/drag）逐帧跟随；
  桌宠隐藏/未启用时改锚托盘图标；门控关/无任何锚/worker 退出 → 气泡必收。
- 布局四段：标题行（< 平台名 >，箭头 24×24 热区）、金额行（大号）、
  峰谷行（[峰]红/[谷]绿 + HH:MM:SS 倒计时，QTimer 1s 本地走）、
  底行小字（"缓存于 X 分前" / extra.today）。
- 箭头循环切换 provider（内存态，重启回 default_id）；仅 1 个 provider 隐藏箭头；
  切换**不发任何 settings_req**（A2：点击≠指令）。
- 气泡右键菜单（扁平两项）：立即刷新 / 关闭气泡。
- 新鲜度：age > 2×refresh_s → 金额行灰 + "数据可能过期"角标。
