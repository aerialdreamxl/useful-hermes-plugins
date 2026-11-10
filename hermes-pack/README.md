# hermes-pack — Hermes 数据家目录一键搬家打包

换机、重装、迁移实例时，把 `$HERMES_HOME`（Hermes 的数据目录）打成一个迁移 zip：自动剔除**可再生环境**（源码树、venv、工具链、缓存）、**运行时残渣**（pid/lock/tmp/心跳/日志）和**过期快照**（`.bak` 全家桶），其余全部收进包里。

设计核心是**黑名单语义**：只杀规则里列出的东西。你以后新装的插件、新建的数据目录**默认自动进包**，不需要维护任何"要带上什么"的清单——这是它和"白名单打包"的根本区别，也是它不会在半年后悄悄丢东西的原因。

## 安装与启用

放进 `$HERMES_HOME/plugins/`（或一级分类子目录），`plugins.enabled` 加入 `hermes-pack`（子目录放置时写 `custom-plugins/hermes-pack`），重启。

启用后它注册一条 CLI 子命令，**没有工具、没有钩子**——对 Agent 行为零影响，纯运维工具。

## 用法

```bash
hermes package backup.zip              # 打包当前实例到 backup.zip
hermes package out.zip --dry-run       # 只统计与报告，不写文件（迁移前先跑这个看清单）
hermes package out.zip --home /path/to/other-hermes-home   # 打包别的实例
hermes package out.zip --exclude-file my-rules.txt         # 追加自己的黑名单
hermes package out.zip --no-default-excludes --exclude-file mine.txt  # 只用你的规则
hermes package out.zip --store         # 不压缩直接存（更快、更大）
hermes package out.zip --force         # gateway 在跑也照打（WAL 一致性风险自负）
```

## 行为细节

- **热库保护**：检测到 gateway 在跑时默认中止（exit 3）并提示先停进程——SQLite 热拷会撕裂。`--dry-run` 只读不拦，`--force` 明示自担风险。
- **拒绝覆盖**：目标 zip 已存在时直接拒绝，不悄悄覆盖。
- **不吃掉自己**：输出文件在 home 内也会自动跳过。
- **链接与 junction 跳过**：不跟随符号链接/目录联接打包（防环、防越界）。
- **包内清单**：zip 根部有 `_hermes-pack-manifest.json`，记录来源 home、主机名、文件数、原始字节、打包时 gateway 是否在跑——新机恢复时先看它。
- `*.db-wal` / `*.db-shm` **绝不在黑名单里**：WAL 旁文件必须随主库同迁，丢了等于丢最近的写入。这是内置规则刻意的设计，你自定义规则时也建议别杀它们。

## 黑名单规则语法

内置默认 + 你的 `--exclude-file` 追加，gitignore 风格的子集，后匹配优先：

| 写法 | 语义 |
|---|---|
| `/name/` | 锚定 home 根的目录（连子内容整棵剪枝） |
| `/a/b*.json` | 锚定根的相对路径 glob |
| `*.lock` | 任意深度的文件名 glob |
| `!<pattern>` | 否定规则，放行前面误杀的 |

内置黑名单按三类组织：①可再生大件（`hermes-agent/` 源码树、`installs/`、`tools/`、`cache/`、venv、gateway 服务目录——通常是体积大头）②运行时状态目录（logs、telemetry、locks、各类缓存 json、会话暂存）③死物快照（`state-snapshots/`、`backups/`、`*.bak*`、`*.orig`、`*.tmp`）。清单以 `pack_engine.py` 的 `DEFAULT_EXCLUDES` 为准，一眼能审。

## 新机恢复

解压到新的 `$HERMES_HOME`，装 Hermes 本体（pip/uv 都行），启动。可再生部分会自动重建；`config.yaml`、`.env`、插件、技能、记忆库、会话库随包到位。

## 已知局限

- 黑名单是按 Hermes 典型数据布局盘点的（2026-10 实测盘点产物），如果你的 home 里有别的实例级大件（比如自己塞的数据集），用 `--exclude-file` 追加，或先用 `--dry-run` 的清单确认体积构成。
- 打包不含跨机密钥轮换逻辑——`.env` 里的 Key 原样进包，zip 本身不加密，**保管好它**。
