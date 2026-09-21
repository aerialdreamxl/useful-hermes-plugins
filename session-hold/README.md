# session-hold v2

`/hold` 挂起会话——**模型全程不知道停过**；`/unhold` 恢复，不用你说"请继续"；状态落盘，Hermes 重启后依然有效。

## 用法

| 命令 | 作用 |
|---|---|
| `/hold` | 挂起本会话（冻结点在**下一次 LLM API 调用前**；下一个工具照常执行完，其结果模型看不到） |
| `/hold status` | 状态（parked / 武装 / replay 排队中 / tool-block 降级） |
| `/hold off` | 解除（含唤醒 parked 线程并丢弃其冻结点） |
| `/unhold` | 恢复 + 自动续跑（进程内秒级唤醒；跨重启走快照回放） |
| `/hold <key>` / `/unhold <key>` | 多窗口桌面端显式指定会话 |

## v2 是怎么"骗过"模型的（三层机制）

**① PARK（默认，进程活着）**：`llm_execution` 中间件在"最终请求已组装、还没出网"的那一帧拿到完整请求体，把它落盘成快照，然后**阻塞回合线程**轮询等待。`/unhold` 置位事件，同一个请求对象原样递给 `next_call()`——回合循环只是经历了一次"慢 API 调用"。零伪造消息、零提示词痕迹、零历史手术。park 期间轮询 agent 的中断标志，所以 `/stop`、steer 照常工作（操作员开口则挂起 drain 到 idle）。park 有 8 小时 TTL，到期放行请求并降级为 ③。

**② FREEZE/REPLAY（parked 期间进程死亡）**：parked 回合的消息只在回合结束时才落盘——所以重启后恢复的会话历史**恰好等于冻结点请求的前缀**，没有假尾巴要清。`/unhold` 标记 `replay=pending` 并踢一个续跑回合；`llm_request` 中间件把该会话下一个请求的 messages **整段替换为冻结快照**（合成 kickoff 行落在被替换侧，模型看不到），后续帧 = 冻结前缀 + 循环自行追加的行（纯 append 重构，前缀缓存保热）。模型视角：连续的 N+1 次 API 调用，中间什么都没发生。剧集由 `post_api_request` 观察者在模型给出无工具调用的最终回答时自动收尾；若你回放期间插话（真实用户消息），剧集即刻让位，绝不吃掉你的话。

**③ TOOL-BLOCK（v1 兜底）**：请求体含不可序列化成员存不了快照、或回放剧集遇到异常时，降级为 `pre_tool_call` 拦截 + 可见收尾。恢复语义不变。

## 边界与代价（诚实清单）

- park 占住该会话的回合线程（cron/子代理的 API 流同样占线程——别在无人值守作业上挂起后关机）。
- 冻结请求隔久了重放，provider 端 prompt cache 已冷，首帧按全价 input 计费。
- 回放续跑回合里**回合中途再崩溃**时，核心 auto-continue 的恢复提示不在本插件的剥离名单内——模型会看到它，属安全降级。
- 桌面端 UI 上 parked 会话仍显示"运行中"（对，它就是运行中，只是停在你眼皮底下的 API 边界）。
- 记录缺口：parked 回合若进程死亡，其中途轮次从未写入转录库——模型经回放看得见全部，会话查看器会缺一截。

## 为什么叫 hold 不叫 pause

`/pause` 与 `/resume` 都是内建注册表命令，`register_command` 拒绝重名。

## 验证

- `tests/test_hold_state.py`：44 项离线断言（隔离 HERMES_HOME）：v1 tool-block 全链路 + park 武装/唤醒/中断/降级 + 快照重启存活 + 回放帧1整换/帧N纯追加/用户插话让位/最终回答收尾/缺快照干净退出 + 复停（re-park）。全过。
- `hermes plugins doctor session-hold`：OK、0 WARN；注册面确认 `hooks=[pre_tool_call, pre_llm_call, post_api_request] middleware=[llm_execution, llm_request] commands=[hold, unhold]`。
- **生效条件**：插件代码在进程启动时导入——v2 上线需重启 Hermes 后端（桌面端退出重开），然后**新会话**里 `/hold` 才带 park 能力。
- 待真实链路验证：桌面端跑多步任务 → `/hold` → 杀进程重启 → `/unhold` → 观察是否无缝续跑（日志关键词 `parked at the API boundary` / `replayed frozen context`，在 `Data\logs\agent.log`）。
