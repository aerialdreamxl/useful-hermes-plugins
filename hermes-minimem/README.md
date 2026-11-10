# hermes-minimem

一个 Hermes Agent 记忆提供商插件：全部记忆落在一个 SQLite 文件里，稠密向量存 int8 BLOB，
检索融合「进程内 numpy 矩阵（稠密）+ BM25 或模型稀疏」，权重随 query 形态浮动。

v0.8 的 embedding 后端可在 OpenAI-compatible、DashScope SDK 和本地 fastembed 之间切换。
默认仍是 `openai`，因此 v0.7 数据可以直接继续使用。

## 它替代什么

`memory.provider` 这个位置（同时只能有一个外部 provider）。**本 README 不表示插件已经启用。**
插件通过 `on_memory_write` 被动镜像内置记忆工具的写入，不替换 `MEMORY.md` / `USER.md`。

## ⚠️ 安装位置：memory provider 不认一级子目录

通用插件扫描器支持 `plugins/custom-plugins/<name>` 这类一级分类目录递归发现，**记忆 provider
的发现器不支持**——`plugins/memory/__init__.py` 只认 `$HERMES_HOME/plugins/<name>/` 平铺一级目录。
所以本插件必须满足下面两条之一：

1. 把目录直接放在 `$HERMES_HOME/plugins/hermes-minimem/`（推荐，最直白）；
2. 想留在分类子目录里，就在 `$HERMES_HOME/plugins/` 下建一个同名 junction/符号链接指向实际目录
   （Windows: `mklink /J`，Linux/macOS: `ln -s`）。已实测：链接就位后
   `discover_memory_providers()` 能发现它，`memory.provider` 按名激活正常。

## 启用

```bash
hermes plugins list                    # 看它在不在
# 在 Settings → Plugins 里启用，或在 config.yaml 里加 plugins.entries.hermes-minimem.enabled: true
# 然后把 memory.provider 改成 hermes-minimem，重启 gateway
```

共享 `pip_dependencies` 只有 `numpy>=2.0`，供稠密矩阵使用；没有 numpy 也能跑，稠密路关闭退回关键词。
`dashscope` SDK 与 `fastembed` **不在** `pip_dependencies` 中：选择相应后端后经 PM 隔离环境按需安装
（需 `allow_pm_install: true` 授权），不污染 Hermes 主 venv。

## 五个工具

| 工具 | 作用 |
|---|---|
| `minimem_remember(text, kind, tags, importance)` | 写一条 |
| `minimem_recall(query, limit)` | 显式检索，返回带 id 的结果 |
| `minimem_update(id, text, importance)` | 改一条（改文本会按当前后端重算向量） |
| `minimem_forget(id)` | 精确 id 删除 |
| `minimem_stats()` | 条数、体积、backend、output_type、api_key_source、stale 向量、模型稀疏状态、计数器 |

另有 CLI：`hermes hermes-minimem {stats|search|reindex|reembed|export|path}`。

`reembed` 是换后端后的批量洗白命令：默认只重嵌「与当前 backend+model+dim 不匹配」的
stale 行（`--dry-run` 先数不动手，`--every` 全部重嵌，`--batch N` 每请求条数，flash 侧
sidecar 上限压到 10——服务端 20 条整批会随机 500 "Receive batching backend response failed"，
2026-11-10 实测 20/3 必挂、切 10 全过）。
带位置参数时是**导入模式**：`hermes hermes-minimem reembed file.jsonl` 逐行读
`{text, kind?, importance?, source?}`，用当前后端嵌好再插入（外部库的向量一律不带，
跨空间混嵌是明令禁止的），按文本去重所以中断后可直接重跑。mnemosyne 迁移需先自行把旧库
导出成该 JSONL 形态（`{text, kind, importance, source}` 逐行），映射规则见 PROMPT.md §15。

命令名就是 provider 名（框架用 `active_provider` 做子命令名），所以是 `hermes-minimem`
不是 `minimem`；而且**只有 `memory.provider` 指向它时命令才存在**——provider 不在位时敲这个命令
会得到 "invalid choice"。

## 配置（Settings → Plugins）

| 字段 | 默认 | 说明 |
|---|---|---|
| `embedding_backend` | `openai` | `openai` / `dashscope` / `local` 三选一 |
| `embedding_base_url` | DashScope 兼容端点 | openai 必填（只填到 `/v1` 级，带 `/embeddings` 自动剥掉）；dashscope 可留空走官方默认（compatible URL 会被自动转成 native `/api/v1`）；local 忽略 |
| `embedding_model` | 留空（按后端取默认） | openai=`qwen3.7-text-embedding-flash`；dashscope=`qwen3.7-text-embedding`；local=`BAAI/bge-small-en-v1.5`；换模型/换后端会触发重嵌入 |
| `embedding_dim` | `1024` | API 后端按 13.3 档位表校验；local 由 fastembed 模型决定，手填值被忽略 |
| `embedding_output_type` | `dense` | openai 仅 dense；dashscope `dense`/`sparse`/`dense&sparse`；local `dense`/`sparse`/`dense&sparse`（sparse 需 `local_sparse_model`） |
| `sparse_mode` | `bm25` | `bm25` 本地词法路；`model` 使用后端输出的模型稀疏向量；`off` 仅稠密 |
| `embedding_api_key` | 空 | **明文 Key 会落进 config.yaml 并被同机进程可读**——非空时优先于一切环境变量，介意安全请留空改走下方变量 |
| `embedding_api_key_env` | `MINIMEM_EMBEDDING_API_KEY` | 明文为空时按此名读环境变量 |
| `dashscope_workspace` / `dashscope_region` | 空 | 仅 dashscope 后端：SDK 首选接法，自动拼 `https://{id}.{region}.maas.aliyuncs.com`；留空回退官方域名（此时可用 `embedding_base_url` 覆盖） |
| `local_sparse_model` | 空 | local 含 sparse 输出时必填（如 `prithivida/Splade_PP_en_v1`）；`Qdrant/bm25` 类需外部 IDF 统计，本插件不支持 |
| `local_cache_dir` | 空 | 空则用 `$HERMES_HOME/hermes-minimem/models` |
| `allow_pm_install` | `false` | 关掉时后端依赖缺失只报错不自动安装；开启后才允许 PM side-env 按需装包 |
| `package_index_url` | 空 | 只传给安装子进程（如任意 PyPI 镜像），不永久改进程环境 |

逐键默认补齐规则（v0.8，覆盖旧 seed-once）：启动时只处理 `key not in settings`，用户显式存的
`""` / `false` / `0` / `null` 一律保留不覆盖；每个缺失键经官方 writer
`plugins_settings.save_plugin_settings` 单独写入，绝不手改 YAML。删单键重启会被重新补回，是有意行为。

密钥查找顺序：settings 明文 → `embedding_api_key_env` 指定变量 → `MINIMEM_EMBEDDING_API_KEY` →
`MNEMOSYNE_EMBEDDING_API_KEY` → `DASHSCOPE_API_KEY`。日志与 stats 只报来源
（`plaintext` / `env:<name>` / `missing`），绝不回显值。

## 后端边界

- `openai`：标准库 urllib 走 `/v1/embeddings`，保持 v0.7 行为，零额外依赖。
- `dashscope`：官方 SDK 2.x `client.embeddings.create`，参数是 **`dimension`（单数）**；可用
  `text_type` 区分 query/document，`output_type=sparse|dense&sparse` 一次同返两种向量。
- `local`：fastembed ONNX 常驻 helper，稠密用 `TextEmbedding`、模型稀疏用 `SparseTextEmbedding`，
  `dense&sparse` = 双份模型下载与内存；无需任何 API Key。
- 后端连续失败 3 次：本会话停用该后端、退回关键词路；**绝不静默切换到另一个后端**（跨向量空间混用是 13.1 铁律）。
- 后端依赖缺失且 `allow_pm_install: false`：明确报错提示，不自动装包。

## 结构

| 文件 | 职责 |
|---|---|
| `provider.py` | `MemoryProvider`：生命周期、稠密矩阵、模型稀疏索引、预取、镜像写入、工具分发 |
| `store.py` | SQLite schema v2：WAL、重试、int8 打包、稠密+稀疏持久化、stale 统计、v1→v2 自动迁移 |
| `bm25.py` | 中英混合分词 + 倒排索引 + 带剪枝的 BM25 打分 |
| `rank.py` | min-max 融合、α 自适应、关键词兜底 |
| `embed.py` | OpenAI-compatible HTTP 客户端（标准库 urllib，重试 + 失败计数） |
| `sidecar.py` | PM side-env 发现/按需安装、常驻 helper 子进程（JSON/stdio）、DashScopeEmbedder / LocalEmbedder |
| `sidecar_worker.py` | 跑在隔离解释器里，调 DashScope SDK 或 fastembed，模型跨请求常驻 |
| `results.py` | `EmbeddingResult` 统一 dense/sparse 结果 + sparse JSON 编解码 + 稀疏点积 |
| `config.py` | 逐键补默认、三后端字段校验、按后端默认模型映射、密钥来源解析 |
| `cli.py` | `hermes hermes-minimem` 子命令 |
| `plugin.yaml` | 清单 + v0.8 全量 `config_schema` + `pip_dependencies`（仅 numpy） |

## 三个容易踩的点（写在这里省得以后重新发现）

1. **provider 拿到的 `ctx` 不是 `PluginContext`**。`plugins/memory/__init__.py` 的
   `_ProviderCollector.__getattr__` 只转发 `register_*`，`ctx.get_config` 会抛
   `AttributeError`。所以 `config.py` 走三级阶梯：先试 `ctx.get_config`，再直接读
   `config.yaml` 的 `plugins.entries.<id>.settings`，最后用 schema 默认值；补默认同理直接调
   `plugins_settings.save_plugin_settings`。
2. **矩阵行号、模型稀疏行号和 BM25 行号是两套独立索引空间**。融合必须在 BM25 行号空间做，
   稠密/稀疏命中要经 id 映射翻译，否则删过条目之后必然错位。
3. **写入路径不能依赖 numpy**。`pack_vector` 和向量归一化都用纯 Python 实现：Hermes 的
   PM venv 里真的没有 numpy，如果写入依赖它，那"缺依赖"就变成了"只能读不能写"。
4. **DashScope 用 `dimension`（单数），OpenAI-compatible 用 `dimensions`（复数）**；三个接口
   不能共用一套参数，详见 PROMPT.md 13.5。

## 验收与实测

设计契约、逐条裁定与验收标准（v0.7 15 条 + v0.8 A16–A22）见同目录 `PROMPT.md`
（第 11/14 节）。离线测试套件在开发工作区，未随本仓库发布；真实 DashScope /
本地 fastembed 烟测结论以 PROMPT.md 的实测记录为准。
