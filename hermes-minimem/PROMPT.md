# hermes-minimem 需求书

版本：v0.8（三向量接口后端、逐键默认补齐、可选模型稀疏；settings 位 `plugins.entries.hermes-minimem.settings`）
日期：2026-11-05；v0.8 增量冻结：2026-11-09
状态：**v0.7 已实现并验收；v0.8 为新增量需求（第 13 节），待实现**

变更史：
- v0.1 → v0.2：存储从"单/多 memory.json"改为"单 minimem.db（SQLite）"，实测依据 3.1 / 3.2。
- v0.2 → v0.3：Embedding 后端去掉 fastembed（用户裁定）；加入 numpy 常驻矩阵检索方案；**sqlite-vec 实测为负优化，不采用**（3.7）。
- v0.3 → v0.4：加入自适应稀疏（BM25）+ 稠密混合检索（第 5 节），确认四条最终目标。
- v0.4 → v0.5：加入 settings 配置面，按百炼 OpenAI 兼容端点文档定义字段与校验。
- v0.5 → v0.6：settings 位改为 `plugins.entries.hermes-minimem.settings`（用户纠正），`plugin.yaml` 的 `config_schema` 驱动，已实测解析通过。
- v0.6 → v0.7：纠错——百炼原生接口支持稀疏（`dense&sparse`），实测与 BM25 打平，默认仍用 BM25，模型稀疏留作 `sparse_mode: model` 选项。
- v0.7 → v0.8：用户新增需求（2026-11-09 裁定，全文见第 13 节）——embedding 后端三选一 `local`(fastembed) / `openai`(`/v1/embeddings`) / `dashscope`(官方 DashScope SDK 2.x 千问 embeddings)，各后端可配模型名、API Key 环境变量或明文、base_url（如需要）、维度（固定维度只填一个数）、向量输出类型（稠密/稀疏，如支持）且稀疏真正参与检索；可选依赖经 Hermes PM 隔离环境按需安装；配置缺键由官方 writer 逐项补默认（**覆盖旧 seed-once**）；明文 Key 经用户明确允许可进 `config.yaml`。v0.3 的"fastembed 移除"与 v0.7 的"仅 numpy"两条在本节被重新裁定，第 2/3/7.5 节相关原文按本节为准。

---

## 1. 一句话定位

一个 Hermes Agent 记忆提供商插件：全部记忆落在**一个 minimem.db**（SQLite，标准库自带），向量 int8 存 BLOB，检索走**自适应稀疏（BM25）+ 稠密（numpy 矩阵）混合**，不设字符预算、不依赖外部服务即可降级运行。

它替代的是 `memory.provider: mnemosyne` 这个位置，不是 `MEMORY.md / USER.md` 那套内置存储。

**最终版要同时满足四条**（用户 2026-11-05 定）：
1. **回忆准确**——混合检索，实测优于任何单路（5.4）
2. **自适应稀疏/稠密**——按 query 特征调权，不写死固定权重
3. **安装极轻**——只有一个 numpy 依赖，有现成 cp314 轮子，无编译、无模型下载
4. **鲁棒**——任一路失效都能降级服务，检索永不抛异常到对话层

---

## 2. 已冻结的决策

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 形态 | Hermes 记忆 Provider 插件，`memory.provider` 可直接切过去 |
| D2 | 存储 | **SQLite 单库**（v0.2 修订）。单文件、标准库 `sqlite3`、零第三方依赖 |
| D3 | Embedding | 百炼 OpenAI 兼容端点（`qwen3.7-text-embedding-flash`，1024 维）。~~fastembed 已移除~~（**v0.8 重新裁定**：fastembed 恢复为可选 `local` 后端、DashScope SDK 2.x 为 `dashscope` 后端，见第 13 节；默认后端仍是 openai 兼容端点） |
| D4 | 旧数据 | 要迁移 Mnemosyne 现有记忆，但先影子跑，指标达标才切正式 provider |
| D5 | 依赖 | **numpy 必需**（无它则稠密路退化，检索慢 200 倍）；**sqlite-vec 不采用**（实测负优化，见 3.7）；其余全部标准库。**v0.8 增量**：fastembed / dashscope SDK 不进 `pip_dependencies`，经 PM 隔离环境按需安装（13.4） |
| D6 | 检索 | **自适应混合**：BM25 稀疏 + embedding 稠密，分数归一化加权（α 按 query 特征在 0.3–0.6 间浮动）。不用 RRF（实测更差），不用 FTS5（多吃 7 MB）。**v0.8 增量**：模型稀疏升级为可选稀疏源之一（13.5） |
| D7 | 配置面 | 配置落 **`plugins.entries.hermes-minimem.settings`**，由 `plugin.yaml` 的 `config_schema` 驱动 Settings → Plugins 面板；密钥走 `.env`。**不用** memory provider 的 `config_schema.py`/flat_json 那套（避免一个字段两个来源）。**v0.8 增量**：缺键逐项经官方 writer 补默认（13.7）；API Key 允许明文存 settings（13.2） |

---

## 2.5 与现有方案的基线对比（实测，2026-11-05）

| 指标 | Mnemosyne（实测） | minimem（预测） | 倍数 |
|---|---|---|---|
| 插件代码 | 72,159 行 / 114 个 .py 文件 | ~1,000 行 / ~8 文件 | **72×** |
| 依赖体积 | 143 MB venv | ~45 MB（只有 numpy） | **3.2×** |
| 数据库 | 44.3 MB（1650 条） | ~4 MB（同条数：1.7 MB 向量 + 2 MB 正文） | **11×** |
| ├ 向量部分 | 34.3 MB | 1.7 MB | **20×** |
| ├ 向量编码 | `embedding_json TEXT` 19.3 KB/条 | int8 BLOB 1 KB/条 | **19×** |
| └ FTS + 索引 | 3.2 MB | 0（BM25 在内存重建，不落盘） | — |
| 工具面 | 42 个工具 / 29,564 字符 ≈ **7.4k tokens** | 5 个工具 / ~2,000 字符 ≈ 0.5k tokens | **省 6.9k tokens/轮** |
| import 启动 | 890 ms | ~80 ms（numpy 70 + 自身 10） | **11×** |
| 启动加载数据 | 890 ms import + SQLite 打开 | 160 ms（numpy 70 + 加载 86/20k 条） | **5.5×** |
| 单次检索 | 混合召回（向量 + FTS + 图 + 可选重排） | BM25 1.3 ms + 矩阵 1.3 ms + 1 次 embedding API | 同量级（都受 API 制约） |

依赖分布（143 MB 的构成，实为 fastembed 拖来的）：onnxruntime 40 MB、numpy 45 MB、PIL 15 MB、hf_xet 9 MB、tokenizers 8 MB、huggingface_hub 5 MB。去掉 fastembed 后只剩 numpy。

**必须承认的落后项**：minimem 没有 FTS 混合检索、没有 episodic 巩固、没有 temporal triple、没有跨会话 sync、没有 persona、没有共享记忆面。这些在 Mnemosyne 里都有实现，在 minimem 里全部列为非目标。所以「轻」是以「功能面收缩」换来的，不是纯白捡。

**为什么仍然值得做**：最大的开销不是磁盘，是**每轮 7.4k tokens 的工具描述**——42 个工具里 `shared_*`、`persona_*`、`sync_*` 大部分日常不用，却每轮都在系统提示里。其次是 890 ms 的 import 成本，每开一个 CLI 会话、每次 cron 跑批都要付一次。

---

## 3. 现实约束（实测，不是推测）

### 3.1 存储层对比：JSON vs SQLite

测试条件：1024 维 int8 向量 + 相同字段，全部真实落盘（`fsync` + `os.replace` / 事务提交），多次运行取最小值。JSON 侧向量走 base64（这是能塞进 JSON 的唯一做法），SQLite 侧走原生 BLOB。

| 条数 | 体积 JSON / SQLite | 冷加载 | **追加 1 条** | 按 id 取 1 条 | 关键词预过滤 |
|---|---|---|---|---|---|
| 1,000 | 1.62 M / 1.40 M | 66 / 63 ms | 13.5 / **3.4 ms** | 4.8 / 1.6 ms | 4.9 / 4.4 ms |
| 5,000 | 8.07 M / 6.96 M | 98 / 80 ms | 61.4 / **4.3 ms** | 25.8 / 2.4 ms | 25.4 / 13.5 ms |
| 20,000 | 32.3 M / 27.8 M | 220 / 124 ms | 245 / **4.3 ms** | 107 / 2.6 ms | 163 / 21 ms |
| 50,000 | 80.7 M / 69.5 M | 469 / 230 ms | **935** / **5.9 ms** | 394 / 2.7 ms | 390 / 19.5 ms |

读法：
- **追加是分水岭**。JSON 每写一条要把整个库读进来、改、再全量写回，成本随条数线性涨到近 1 秒；SQLite 恒定 4-6 ms。按每天 200 次写入估算，2 万条时 JSON 每天白烧 49 秒 CPU 和 6.4 GB 磁盘写入量。
- **体积小 14%**：base64 把 1 字节膨胀成 1.33 字节，SQLite BLOB 存原始字节。
- **冷加载快 2 倍**（5 万条 230 vs 469 ms）。
- **按 id 取快 145 倍**：主键索引 vs 全量反序列化。

### 3.2 并发写实测（两进程各追加 50 条，同时开火）

| 方案 | 期望 | 实得 | 结果 |
|---|---|---|---|
| JSON 无锁 | 600 条 | **550 条** | 丢 50 条，并抛 `PermissionError: [WinError 32]`（两进程同时 replace 同一文件） |
| SQLite WAL | 600 条 | **600 条** | 零丢失，耗时 0.10 s |

这条是决定性的：Hermes 侧 gateway、CLI、cron 可能同时活着并写记忆。JSON 方案必须自己实现文件锁 + 重试 + 冲突合并，那是把数据库的活重写一遍，还容易写错。

### 3.3 检索性能：numpy 矩阵 vs 纯 Python

1024 维向量，同样数据量下的单次 top-8 检索：

| 方案 | 5,000 条 | 20,000 条 | 备注 |
|---|---|---|---|
| 纯 Python 双循环 | 213 ms | 858 ms | 无 numpy 时的下限 |
| **numpy 矩阵乘** | **0.31 ms** | **1.15–1.30 ms** | BLAS/SIMD，常驻内存 |
| sqlite-vec `vec0` KNN（磁盘） | 15.8 ms | 292.9 ms | 见 3.7，已否决 |

numpy 方案的前提是向量常驻内存（20,000 条 int8 = 20 MB 原始、转 float32 矩阵 = 82 MB）。启动加载一次：int8 86 ms / float32 202 ms（20,000 条）。

推论：**numpy 是硬依赖**，不是可选加速。没有它，检索慢 200 倍，prefetch 的后台预取会被拖到接近秒级。

### 3.4 Hermes 侧硬约束（读源码确认，写错就白干）

1. **插件目录必须是一级目录**：`$HERMES_HOME/plugins/<name>/`。
   `plugins/memory/__init__.py::find_provider_dir()` 只在 `plugins/` 的直接子目录里找，且 `_is_memory_provider_dir()` 只认 `__init__.py` 里出现 `register_memory_provider` 或 `MemoryProvider` 字样。
   **不能**放 `plugins/custom-plugins/<name>/`——二级目录 discovery 走另一套，Provider 会静默找不到。这与"其他 custom-plugin"不同，必须写死。
2. **Provider 契约**（`agent/memory_provider.py::MemoryProvider`）必实现：
   `name` / `is_available()` / `initialize(session_id, **kwargs)` / `get_tool_schemas()`。
   可选覆盖：`prefetch()` / `queue_prefetch()` / `recall_status()` / `sync_turn()` / `handle_tool_call()` / `shutdown()` / `system_prompt_block()` / `on_session_end()` / `on_pre_compress()` / `on_memory_write()` / `backup_paths()`。
3. **多 profile 隔离**：一个 gateway 进程可服务多个 profile，每个 agent 有独立 provider 实例。存储路径必须从 `initialize(hermes_home=...)` 绑定到实例，禁止模块级共享可变状态。
4. **`prefetch()` 必须在关键路径上最快**：后台预取 + 缓存消费，不能同步等 embedding。
5. **后台线程带 contextvars**：用 `agent.memory_provider.spawn_context_thread()`，否则会落到默认 profile。
6. `memory.provider` 同时只能有一个外部 provider；内置 `MEMORY.md/USER.md` 不受影响。

### 3.5 Embedding 端点现状

```
base_url = https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
model    = qwen3.7-text-embedding-flash
dim      = 1024
key      = 从 .env 的 MNEMOSYNE_EMBEDDING_API_KEY / DASHSCOPE_API_KEY 复用
```

同模型同维度意味着：**Mnemosyne 里现成的 1645 条向量可以直接搬，不必重新调 API**。

### 3.6 插件依赖声明机制（PM，实测确认）

插件可以声明自己需要的 Python 包，PM 会自动装进 Hermes 的共享依赖环境。声明优先级：

1. 自带 `pyproject.toml` 的 `[project].dependencies`（有 pyproject 时它说了算）
2. `plugin.yaml` 的 `pip_dependencies:` / `python_dependencies:`（同义；PM 会为无 pyproject 的插件生成虚拟 member `plugin-deps/<key>/pyproject.toml`，`[tool.uv] package = false`）
3. 都没有且声明 `python_runtime: external` → PM 一个包都不装，插件自管

解析器实测（`pm.plugin_declarations.read_python_declaration`）：

```
pip_dependencies: [numpy>=2.0, fastembed>=0.4]  → install_requirements 同上, installs_packages=True   ← 解析器验证样例（fastembed 于 v0.3 弃用、v0.8 改为按需 side-env 可选后端，见 13.4，不再进 pip_dependencies）
python_runtime: external（Mnemosyne）           → install_requirements=(), installs_packages=False
```

`pip_dependencies` 在 `_KNOWN_MANIFEST_FIELDS` 里，是合法字段。

行为约束：
- `hermes plugins enable <name>` 直接触发 `sync_venv(explicit=True)`，uv 求解 → 装进新 generation venv，**不需要单独跑 pm 命令**
- 运行中的进程不会换环境（`activate_dependencies`：Already-running processes are never switched），装完必须重启
- 与核心/已启用插件版本冲突 → `DependencyConflict`，准入失败，config 与环境都不动（fail closed）
- 直接 URL 依赖（`pkg @ https://...`）PM 不装，只 WARN
- uv 对每个平台求解：onnxruntime / torch 这类在部分平台无 wheel 的包，会让插件在那些平台上被 evict

本机现状：`plugins/` 下 20 个插件**无一**声明 `pip_dependencies`，唯一相关的是 Mnemosyne 的 `python_runtime: external`——这条依赖路径在本机从未实跑过。当前 PM 锁在 `installs/<hash>/environments/<hash>/workspace/uv.lock`。

**对 minimem 的结论**：`plugin.yaml` 里声明 `pip_dependencies: ["numpy>=2.0"]`，交给 PM 管，不用 `python_runtime: external` 自己 pip。已实测本机可获取现成轮子（numpy 2.5.4 cp314-win_amd64，走清华源 4 秒下完 12.7 MB）。

### 3.7 sqlite-vec 实测否决（重要）

用户最初要求"只要 numpy 和 sqlite-vec"。两个包都能装、能跑（sqlite_vec 0.1.9，`vec_version()` 正常返回，KNN 结果与 numpy 暴力 top-8 完全一致），但性能实测否定了它：

| 指标（20,000 条，1024 维） | vec0 虚表（磁盘 WAL） | vec0 虚表（内存） | 普通表 BLOB + numpy |
|---|---|---|---|
| 写入 | **41,651 ms**（2.08 ms/条） | 435 ms | 960 ms（0.048 ms/条） |
| 建库总计 | 41.6 s | 0.4 s | 0.96 s |
| 单次 KNN | **292.9 ms** | 29.4 ms | **1.30 ms** |
| 文件 | 84.5 MB | — | 92.2 MB |

2000 条档同样成立：磁盘写 1.07 ms/条、KNN 15.8 ms；换 `journal_mode=DELETE` 更差（1.89 ms/条、28.3 ms）——所以瓶颈不是 fsync，是 vec0 虚表的固定写放大（内存 0.02 ms/条 vs 落盘 2.08 ms/条）。

结论：**不用 sqlite-vec**。向量存普通表的 `BLOB` 列，进程内用 numpy 矩阵检索，比 vec0 磁盘 KNN 快 225 倍、写快 43 倍。留一句备注：若将来条目数大到内存装不下（数十万级），再评估 vec0 或 ANN，现在不是。

补充实测（int8 存储的代价）：int8 vs float32 在随机向量下 top-8 平均重叠 7.19/8（90%）；真实 embedding 分布更集中，重叠率通常更高。体积 27.4 MB vs 92.2 MB，加载 86 ms vs 202 ms，查询 1.04 ms vs 1.23 ms——int8 全面占优。

---

## 4. 数据模型

### 4.1 表结构

```sql
CREATE TABLE mem(
  id        TEXT PRIMARY KEY,      -- mm_<时间戳>_<随机>，稳定唯一，不复用
  text      TEXT NOT NULL,         -- 唯一检索原文
  kind      TEXT NOT NULL,         -- preference|fact|insight|identity|task|relation
  imp       REAL NOT NULL DEFAULT 0.5,
  created   TEXT NOT NULL,         -- ISO8601 带时区
  updated   TEXT,
  source    TEXT,                  -- 小 JSON：{"origin":"user","session_id":"..."}
  vec       BLOB NOT NULL,         -- int8[dim] 原始字节
  vec_model TEXT NOT NULL,
  vec_dim   INTEGER NOT NULL
);

CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);   -- schema_version / embedding_model / dim / created
```

建库时开 `PRAGMA journal_mode=WAL`、`PRAGMA synchronous=NORMAL`、`PRAGMA busy_timeout=5000`。

**向量不进虚表**：`vec` 就是普通 `BLOB` 列（int8 原始字节，1024 字节/条）。不建 `vec0` 虚表——理由见 3.7 实测。检索靠进程内 numpy 矩阵（见 5.2）。

### 4.2 字段纪律

- `kind` 用枚举，不做自由文本。
- `vec_model` + `vec_dim` 随条目存。加载时与 `meta` 里的当前配置比对，不一致 → 该条向量标 stale，检索跳过并计入 `stale_count`，**禁止静默混用不同模型的向量**。
- `source` 只存小对象，不存整段对话。
- `forget` 是真 DELETE，不做软删除。

### 4.3 版本迁移

`schema_version` 存 `meta` 表。升级时读版本号决定迁移路径，不靠猜结构。不做自动迁移框架——版本不对直接报错并给出升级命令。

---

## 5. 检索设计

### 5.1 主路径（混合检索）

```
query
 ├─ 稠密路：embedding API → 与 int8 矩阵做矩阵乘 → cos 分数（~1.3 ms + API 延迟）
 ├─ 稀疏路：BM25（默认，中文 bigram + ASCII 整词）或模型稀疏 → 分数（~1.3 ms）
 └─ 融合：各自归一化后加权求和 → top-8 → 混合打分（cos + imp + 时近性）
```

实测（1647 条真实记忆，见 5.4 表）表明：**两路都不能省**——碎片 query 靠稀疏、改写 query 靠稠密，融合比任何单路都高 10–20 个点。

### 5.2 稠密：内存矩阵

`initialize()` 时一次性构建：

```
rows = SELECT id, vec FROM mem
mat  = np.frombuffer(b"".join(...), dtype=np.int8).reshape(N, 1024).astype(np.float32) / 127
ids  = np.array([...])        # 行号 -> 记忆 id
```

- 20,000 条：加载 86 ms，矩阵 82 MB（float32）。5 万条约 205 MB，32 GB 机器可承受。
- **写入增量维护**：预分配容量（翻倍增长），新条目写入尾部空槽，`count += 1`；容量满时 `np.resize` 到 2 倍（20,000 条约 80 ms，摊销可忽略）。禁止每次写入全量重建。
- **删除**：末行搬到被删行位置，`count -= 1`，保持紧凑；同时维护 `id -> 行号` 字典。
- **模型/维度变更**：矩阵整体重建（读全表），只在配置变更时发生。

### 5.3 稀疏：默认本地 BM25，模型稀疏为可选项

**两种稀疏的实测对比（1650 条真实记忆，同一批 20 条改写 query）**：

| 稀疏实现 | recall@1 | recall@8 | 成本 |
|---|---|---|---|
| 本地 BM25（中文 bigram） | 55% | 65% | 0 额外调用、0 额外 key |
| 模型稀疏（`text-embedding-v4` + 原生接口） | 50% | 65% | 每次多一个原生端点调用（延迟 +0，因为同一次 dense&sparse） |

结论：**打平**。模型稀疏没有带来质量优势，却要求另配原生 base_url 和一个通用 DashScope key。所以默认用 BM25，模型稀疏作为 `sparse_mode: model` 的可选项保留（将来换模型或调 `text_type` 后可能反超，留个开关比删掉便宜）。

**BM25 实现**（纯 Python，零依赖）：
- 中文：连续汉字串 → unigram + bigram（"用户偏好" → 用/户/偏/好/用户/户偏/偏好）
- ASCII：整词保留（含 `_ - . / +`），小写化
- 倒排结构 `term -> {doc_idx: tf}` + `idf` + `dl`
- 实测：1648 条建索引 156 ms，词表 62,005、postings 285,122 条，查询 1.34 ms
- **不持久化**：启动时从 `mem.text` 重建。存 FTS5 trigram 要多花 7.08 MB（1648 条），只把查询从 1.34 ms 降到 0.02 ms，不值
- **增量维护**：入库时 `add_document()`；删除标 tombstone，文档数增长超 30% 时重建一次
- **规模上限**：重建线性增长，2 万条约 1.9 s、5 万条约 4.7 s；超过 2 万条改为后台重建 + 先由稠密路服务

### 5.4 融合与自适应

**融合公式（实测选定）**：

```
score_i = α · norm(cos_i) + (1-α) · norm(sparse_i) + 0.1 · imp_i
```

`norm` 是当前查询内的 min-max 归一化。**不用 RRF**——实测 RRF 在碎片 query 下比纯稀疏还差 7–15 个点（它丢掉分数幅度信息）。

**α 自适应**（区间 [0.3, 0.6]，只做微调，不做开关）：

| query 特征 | α | 理由 |
|---|---|---|
| 长度 < 25 字 或 含标识符（大写缩写/日期/版本号/路径） | 0.35 | 碎片回忆，稀疏主导 |
| 长度 25–60 字 | 0.5 | 均衡 |
| 长度 > 60 字、自然语言描述 | 0.6 | 语义完整，稠密主导 |

**实测依据**（三档 query，recall@8，稠密统一用 `qwen3.7-text-embedding-flash`）：

| query 类型 | dense | sparse（BM25） | 融合 |
|---|---|---|---|
| 15 字碎片（记忆原文前缀） | 27% | 70% | **73%** |
| 80 字完整（记忆原文前缀） | 97% | 97% | **98%** |
| LLM 改写成「用户会怎么问」 | 80% | 65% | **90%** |

**模型选错会毁掉一切**：同一批改写 query 换成 `text-embedding-v4` 做稠密，recall@8 从 75% 掉到 45%，融合也跟着掉到 65%。所以：
- 默认稠密模型写死 `qwen3.7-text-embedding-flash`（兼容端点，1024 维）
- 面板允许改模型，但 `minimem_stats` 里要显示一条提示：换模型会触发全量重嵌入，且不同模型的召回质量差异可能远大于检索策略本身的差异

**阈值**：top-1 融合分 < 0.25 → 返回空，不注入垃圾上下文。

**样本量免责声明**：以上改写 query 实验的样本量是 20 条，recall 每 1 条 = 5 个百分点，噪声较大。三种策略"融合 ≥ 单路"的方向在两次独立实验中一致，但具体百分点只作量级参考；正式验收要求 ≥50 条样本（见 11 节）。

### 5.5 降级链

| 层级 | 条件 | 行为 |
|---|---|---|
| L1 | 百炼 API + numpy 都在 | 向量检索，完整功能 |
| L2 | embedding API 连续失败 3 次 | 纯关键词（`LIKE` 子串匹配）检索，WARN 一次，本会话不再试 API |
| L3 | numpy 导入失败 | 纯关键词检索 + 启动 WARN（不允许静默退回纯 Python 扫描，那会让每轮卡 200 ms 以上） |
| L4 | 全失败 | 返回空字符串，ERROR 记一次 |

任何一层都不能让对话回合失败。记忆是增强项，不是关键路径。

### 5.6 后台预取时序

- `queue_prefetch(query)` 只入队，立即返回
- 后台线程消费队列 → embedding → 检索 → 写 `self._prefetched[session_id]`
- 下一轮 `prefetch()` 直接读缓存，取不到就返回空（宁可少注入，不阻塞）
- `recall_status()` 返回上次实际注入条数与来源标签

---

## 6. 写入与并发

- `sync_turn()` **不落盘**，只入内存队列。
- 后台 writer 线程 debounce：距上次写 ≥5 秒且有新条目，或攒够 20 条，合并成一个事务提交。
- 事务失败（`database is locked`）→ 退避重试 3 次（50/200/800 ms），仍失败进内存队列延后，不丢数据、不抛到对话层。
- 每 100 次写跑一次 `PRAGMA wal_checkpoint(TRUNCATE)`，防止 WAL 无限增大（现有 Mnemosyne 的 WAL 已长到 4.2 MB）。

### 6.5 记忆抽取：接 Hermes 后台审查（用户的裁定）

**minimem 不自己调 LLM 抽记忆**。抽取由 Hermes 的 `background_review` 负责，minimem 只负责接收。

链路（读源码确认）：

1. 每回合结束后，`agent/background_review.py` fork 一个 agent，prompt 是"should any skill/memory be saved or updated?"
2. 该 agent 调**内置** `memory` 工具，写 `MEMORY.md`（环境事实）/ `USER.md`（用户画像）
3. 写入成功后，`MemoryManager.notify_memory_tool_write()` 把每条 add/replace/remove 连同 metadata（含 `write_origin`、`session_id`、`previous_content`）推给外部 provider 的 `on_memory_write()`
4. minimem 在这一步把内容收进自己的库

注意两个事实：
- `curator` **不是**记忆组件——它管技能（pin/archive/consolidate），fork 时带 `skip_memory=True`。别把两者混了。
- `memory` toolset 只含内置 `memory` 工具，**不自动包含** provider 注册的 `minimem_*` 工具。

#### minimem 的 `on_memory_write` 处理规则

```
on_memory_write(action, target, content, metadata)
```

- `action="add"`：新建条目。`kind` 由 `target` 映射（`user` → `preference`/`identity`，`memory` → `fact`/`insight`），`source.origin = metadata.write_origin or "memory_tool"`，`source.session_id` 原样保留。
- `action="replace"`：用 `metadata["previous_content"]` 精确匹配旧条目 → 更新 `text` 并**重算向量**；匹配不到则退化为 add。
- `action="remove"`：用 `metadata["previous_content"]` 匹配并删除；匹配不到则忽略（记 debug 日志，不报错）。
- 幂等：同一 `content` 的 add 在 30 秒窗口内重复到达只落一条（用 `sha1(content)` 做窗口去重）。

#### 归属划分（防止两套编辑互相打架）

| `source.origin` | 谁在管 | 规则 |
|---|---|---|
| `background_review` / `memory_tool` | 内置 MEMORY.md / USER.md | 跟随镜像的 add/replace/remove |
| `tool` | `minimem_remember` 直接调用 | minimem 自主管理，不受镜像影响 |

镜像条目只随镜像事件增删改，工具条目只随工具调用增删改——两套不交叉匹配。

#### 可选的增强：让审查直接写 minimem

配置 `auxiliary.background_review.extra_tools: [minimem_remember, minimem_recall, minimem_stats]` 后，审查 agent 可以绕过 MEMORY.md 的 2200 字符预算，直接写 minimem 大库。源码注释明确认可这个用法（"e.g. … a memory-provider write surface"）。

- 这需要改 `config.yaml`，由用户本人动手（我不代改配置）。
- 第一版**不依赖**它：只靠 `on_memory_write` 镜像就能工作，配置留空即可。
- 已知限制：镜像路径受内置库预算约束——MEMORY.md 写满导致写入失败时，provider 收不到通知（`notify_memory_tool_write` 只在 `success=true` 时转发）。这是选 `extra_tools` 的唯一理由。

---

## 7. 工具面（模型可见）

只注册 5 个：

| 工具 | 用途 |
|---|---|
| `minimem_remember(text, kind, tags, importance)` | 写一条 |
| `minimem_recall(query, limit)` | 显式检索，返回带 id 的列表 |
| `minimem_update(id, text)` | 改一条（改文本需重算向量） |
| `minimem_forget(id)` | 删一条（精确 id，不做模糊删除） |
| `minimem_stats()` | 条数、体积、后端、stale 向量数、最近写入时间 |

约束：
- 全部返回 JSON 字符串（`handle_tool_call` 契约）
- 工具描述写明"记忆条目会进入系统提示词，禁止写入指令性内容"，防注入

---

## 7.5 配置面（settings 位 = `plugins.entries.hermes-minimem.settings`）

用户指定：配置放在 Hermes 的插件 settings 位，可配 **端点地址 / 密钥环境变量 / 模型名 / 维度 / 稀疏方式**。

### 7.5.1 机制（读源码确认）

1. **声明**：在 `plugin.yaml` 写 `config_schema:`，Hermes 的 Settings → Plugins hub 自动渲染成表单。
   驱动链：`hermes_cli/plugins_settings.py::plugin_settings_fields()` → 前端字段 → 保存走 `save_plugin_setting()`。
2. **存储**：非密钥字段落在 `config.yaml` 的 `plugins.entries.hermes-minimem.settings.<key>`；`secret: true` 的字段只进 `.env`（变量名由 `env:` 指定），面板只回显 `has_value`，**永不回读明文**。
3. **读取**：插件侧 `ctx.get_config(key, default)`（`hermes_cli/plugins.py:266`），它读的正是 `plugins.entries.<id>.settings.<key>`，并兼容旧 `config` 子树。

**必须绕开的一个坑**：memory provider 拿到的 `ctx` 是 `plugins/memory/__init__.py` 的 `_ProviderCollector`，它的 `__getattr__` 只转发 `register_*` 开头的属性——`ctx.get_config` 会直接抛 `AttributeError`。

所以 minimem 用这个读取阶梯（照抄 smart-terminal-cwd 的 proven 做法）：

```
1) try: ctx.get_config(key)          # 未来框架若传真实 PluginContext，自动生效
2) except AttributeError / 返回 None:
   from hermes_cli.config import load_config_readonly
   entry = (cfg["plugins"]["entries"]).get("hermes-minimem") or 任何 endswith("/hermes-minimem") 的键
   value = entry["settings"][key]
3) 都拿不到 → 用 schema 里的 default
```

第 2 步的 `endswith("/hermes-minimem")` 是防嵌套目录坑：插件放在 `plugins/custom-plugins/<name>` 时，entries 键会带 `<subdir>/<name>` 前缀。minimem 部署在一级目录 `plugins/hermes-minimem/`，正常键就是 `hermes-minimem`，但回退逻辑照留。

### 7.5.2 `plugin.yaml` 的 config_schema

```yaml
config_schema:
  embedding_base_url:
    type: str
    label: Embedding 端点地址
    description: 只填到 /compatible-mode/v1，不要带 /embeddings 后缀
    default: https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    required: true
  embedding_model:
    type: str
    label: 模型名
    default: qwen3.7-text-embedding-flash
  embedding_dim:
    type: str
    label: 向量维度
    description: 需与该模型支持的档位一致；改维度会触发全量重嵌入
    choices: ["2560", "2048", "1536", "1024", "768", "512", "256", "128", "64"]
    default: "1024"
  sparse_mode:
    type: str
    label: 稀疏检索方式
    description: bm25=本地关键词（默认，零额外成本）；model=百炼原生接口模型稀疏（需另配原生端点与 DashScope Key，实测与 bm25 打平）；off=纯稠密
    choices: ["bm25", "model", "off"]
    default: "bm25"
  embedding_api_key:
    secret: true
    env: MINIMEM_EMBEDDING_API_KEY
    label: API Key
    required: true
```

字段规则（源码确认）：
- `type` 支持 `str/int/float/bool/list/dict`；带 `choices` 的 `str` 自动升级成 `enum`，保存时校验取值
- `secret: true` 时字段不进 config.yaml，值走 `.env`；`env:` 省略时自动生成 `MINIMEM_EMBEDDING_API_KEY` 这种 `<PLUGIN>_<KEY>` 名字
- 键名支持点分路径（`a.b`），minimem 用平铺键即可
- `embedding_dim` 的 `choices` 是**所有已知模型的并集**（因为模型可换）；保存只校验"是不是枚举值"，"当前模型支不支持这个档位"由加载时按 7.5.5 的档位表校验

### 7.5.3 百炼 embedding 端点事实（2026-11-05 实测，含纠错）

**两套接口，稀疏能力完全不同：**

| | OpenAI 兼容端点 | DashScope 原生接口 |
|---|---|---|
| URL | `POST {base_url}/embeddings` | `POST https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding` |
| 请求 | `{"model","input","dimensions","encoding_format"}` | `{"model","input":{"texts":[...]},"parameters":{"text_type","output_type"}}` |
| 默认返回 | 稠密 1024 维 float | 稠密 1024 维 float |
| `output_type=sparse` | **HTTP 200 但 `embedding` 是 `None`**（静默失败） | 返回 `sparse_embedding` 数组 |
| 二者同时要 | 不支持 | **`output_type: "dense&sparse"` 一次返回两个** |

实测数据（`text-embedding-v4`）：

```
兼容端点默认      → embedding: list, 1024 维, 非零 1024/1024 → 稠密
兼容端点 sparse   → embedding: None          ← 不报错，返回 200
原生接口 dense&sparse → dense 1024 维 + sparse 14 项（短文本）/ 24 项（400 字）
                        每项 {"index": 102543, "token": "用户", "value": 1.9727}
                        usage 只按输入 token 计一次（34 tokens / 2 条）——稀疏不额外计费
                        延迟 289–305 ms（与只取 dense 相当）
```

关键参数细节：
- 正确取值是 `"dense&sparse"`；写 `"both"` 会 400，错误信息会列出 `['dense','sparse','dense&sparse']`
- `parameters.text_type`：`document`（建库）/ `query`（检索），应分开传
- 批量上限 **10 条**（第 11 条直接 400）
- 稀疏向量的 `index` 是模型词表下标（实测最大到 118027），`token` 是原词，`value` 是权重——可解释、可调试

其他兼容端点事实：
- `base_url` 形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`；旧域名 `https://dashscope.aliyuncs.com/compatible-mode/v1` 仍可用
- `dimensions` 官方文档称"仅 `text-embedding-v3`/`v4` 支持"，但**实测 `flash` 也接受**（`dimensions=512` 返回 512 维）。实现上按"支持"处理：传 `dimensions`，若返回维度与请求不符则不报错、按实际维度入库并记一条 WARN。

| 模型 | 可选维度 | 单次最大行数（兼容） | 单次最大行数（原生） | 单行最大 token |
|---|---|---|---|---|
| `qwen3.7-text-embedding` | 2560 / 2048 / 1536 / 1024(默认) / 768 / 512 / 256 | 20 | — | 128,000 |
| `qwen3.7-text-embedding-flash` | 1024(默认) / 768 / 512 / 256 | 20 | — | 128,000 |
| `text-embedding-v4` | 2048 / 1536 / 1024(默认) / 768 / 512 / 256 / 128 / 64 | 10 | **10（实测）** | 8,192 |
| `text-embedding-v3` | 1024(默认) / 768 / 512 / 256 / 128 / 64 | — | — | — |

**稀疏只由 `text-embedding-v4`（及 v3）实用**。补充实测（2026-11-05）：

| 调用 | 结果 |
|---|---|
| 兼容端点 · `flash` 默认 | 1024 维稠密，非零 1023/1024 |
| 兼容端点 · `flash` + `output_type=sparse` | `embedding: None`（200，静默失败） |
| 兼容端点 · `flash` + `dimensions=512` | **512 维，参数生效**（官方文档称仅 v3/v4 支持，实测不符，文档过时） |
| 原生接口 · `flash` + `dense&sparse` | dense 1024 + **稀疏 2693 项**，token 是英文（`"functionality"`/`"scripts"`） |

`flash` 的稀疏**不可用于检索**：2693 项 ≈ 21 KB/条（比 dense 的 4 KB 还大）、点积慢 190 倍，等于把稠密换了个格式存。模型稀疏只在 `text-embedding-v4` 上有意义（14–24 项）。

### 7.5.4 「是否稀疏」的真实含义（含纠错）

用户最初的记忆是对的：**百炼确实支持稀疏向量**。上一版需求书写成"兼容接口不支持稀疏，所以稀疏只能是本地的"，只对了一半——兼容接口确实不支持，但原生接口支持，而且产出的是**模型学习的稀疏**（带词表索引与 token），可解释、可调试。

但实测它**并不比 BM25 更好**（见 5.3：两者 recall@8 都是 65%），所以只作为可选项保留。

因此稀疏方式的语义定为三选一（`sparse_mode`，取值依据见 5.3 的实测对决）：

- **`model`**：走原生接口 `dense&sparse`，一次调用拿两种向量。需要 DashScope 原生 base_url + 通用 API Key（与兼容端点的 key 可能不是同一个）。**实测与 bm25 打平**（recall@8 均 65%，recall@1 50% vs 55%），所以不是默认值。
- **`bm25`**（默认）：本地 BM25，零额外请求、零额外 key。
- **`off`**：纯稠密。

面板字段为三选一枚举 `sparse_mode`，`description` 里写清"model 需要另配原生端点与 key"。

### 7.5.5 校验与变更后果

**保存时校验**（拒绝并给可读原因，不静默吞掉）：
- `embedding_base_url` 以 `/embeddings` 结尾 → 提示"只填到 `/compatible-mode/v1`"，并在代码里自动剥离该后缀（宽容处理）
- `embedding_dim` 不符合当前模型的档位表 → **加载时**报错并在 `minimem_stats` 里标 `config_invalid`，不静默用错维度
- 模型名不在已知表里 → 允许保存（自定义端点可能有别的模型），维度改为自由填，跳过档位校验

**变更后果**：

| 变更 | 后果 |
|---|---|
| `embedding_model` 或 `embedding_dim` 改变 | 库内所有向量的 `vec_model`/`vec_dim` 与配置不符 → 全部标 stale，检索跳过并返回 `needs_reembed` 计数；需显式跑 `minimem reindex` |
| `embedding_base_url` 或 API Key 改变 | 向量不变，只影响新查询；不触发 reindex |
| `sparse_mode` 改变 | 立即生效（BM25 索引本就在内存，切到 off 只是不参与融合；切到 model 需要额外配原生端点与 key） |

`reindex` 必须在保存后**显式确认**才执行，绝不后台偷偷重嵌（会产生 API 费用）。

### 7.5.6 与 memory provider 专用 schema 的关系

Hermes 另有一套 memory provider 专用的声明式配置（`config_schema.py` + `flat_json`，落到 `$HERMES_HOME/<name>/config.json`，走 `GET/PUT /api/memory/providers/{name}/config`）。

**minimem 不用它**——用户指定 settings 落在 `plugins.entries`，两套并存会出现"同一个字段两个来源"的歧义。`get_config_schema()` 返回 `[]`，配置只有一个入口：Settings → Plugins。

---

## 8. 迁移（D4 的门禁）

### 8.1 步骤

1. 只读打开 `mnemosyne/data/mnemosyne.db`（`mode=ro`），导 `working_memory`（约 1650 条）+ `episodic_memory`（131 条）
2. 读 `memory_embeddings`，同模型同维度 → **直接复用向量转 int8**，不调 API；缺失的才补嵌
3. 写入 `minimem-import/minimem.db`（迁移产物单独目录，不碰正式库）
4. 生成 `migration-report.md`：导入条数、复用向量数、需重嵌入数、失败条目与原因

### 8.2 切换门禁（不达标不许切）

| 指标 | 门槛 |
|---|---|
| 检索质量 | 30 条真实 query（作者过去提过的问题），top-3 命中率 ≥ Mnemosyne 的 90% |
| 延迟 | 单轮 prefetch P95 ≤ 150 ms（命中缓存时） |
| 体积 | 全量迁移后 ≤ 20 MB |
| 稳定性 | 影子跑 3 天，零异常抛到对话层 |
| 写入完整性 | 并发测试 600 条零丢失 |

### 8.3 影子跑与回滚

- 新插件先不接 `memory.provider`，用独立 `/minimem` 命令做检索，与 Mnemosyne 并行，人工比对
- 达标后由**用户本人**改 `memory.provider`（我不代改配置）
- 回滚 = 把 `memory.provider` 改回 `mnemosyne`；新写入条目用 `minimem export` 导 JSONL，数据不丢

---

## 9. 决策状态

| 编号 | 问题 | 状态 |
|---|---|---|
| 9.1 | 向量精度 int8 还是 float32 | **已定 int8**：体积 27.4 vs 92.2 MB，加载 86 vs 202 ms，查询 1.04 vs 1.23 ms，top-8 重叠 90%（随机向量下） |
| 9.2 | 记忆抽取策略 | **已定：不自己抽，接 Hermes 后台审查**（`background_review`，默认开启）。minimem 实现 `on_memory_write` 被动接收；可选配置 `extra_tools` 让审查直接调 minimem 工具。详见 6.5 |
| 9.3 | numpy 怎么来 | **已定**：`pip_dependencies: ["numpy>=2.0"]` 交给 PM；本机已验证 cp314 win_amd64 轮子可直取 |
| 9.4 | 稀疏检索用什么实现 | ~~已定：默认内存 BM25~~ **v0.8 扩展**：BM25 仍是零成本默认；模型稀疏升级为按后端可选的一等公民（dashscope `output_type=sparse` / local `SparseTextEmbedding`），见 13.5。不用 FTS5 trigram——它多吃 7.08 MB（1648 条），只把查询从 1.34 ms 降到 0.02 ms，不值 |
| 9.5 | temporal triple | **不做**：`kind=relation` + 文本表达足够 |
| 9.6 | `on_pre_compress` 洞察抽取 | **不做**：避免与 Hermes 自身压缩逻辑抢活 |
| 9.7 | sqlite-vec | **已否决**（见 3.7）。若将来数十万条且内存装不下再重新评估 |

---

## 10. 非目标（明确不做）

- 不做向量索引（HNSW/FAISS/vec0 虚表），numpy 矩阵乘够用
- 不做 ORM、不做自动迁移框架、不做多表范式设计（就是 mem + meta 两张表）
- 不做云同步、不做加密
- 不做 Dashboard / 桌面 UI（第一版只做后端 + 工具 + CLI 命令）
- 不接管 `MEMORY.md / USER.md`
- 不修改任何 Hermes 核心文件

---

## 11. 验收标准

1. `hermes plugins doctor <name>` 通过，零 WARN
2. `memory.provider` 切过去后，新会话里 `minimem_*` 工具可见可调
3. `minimem_remember` → 重启进程 → `minimem_recall` 能取回（跨进程持久化）
4. **numpy 矩阵往返一致**：写入 → 重启 → 内存矩阵重建后，同一条 query 的 top-8 与重启前一致
5. **int8 往返无损**：`remember` 的文本经 embedding → int8 → 存库 → 加载还原，decode 后的向量与直接量化结果逐字节相同
6. 断网状态：走 L2 关键词降级，不抛异常，日志有 WARN
7. 写入 5000 条压测：单次事务 ≤ 20 ms，库文件 ≤ 12 MB（int8 向量 5 MB + 中文正文约 6 MB）
8. 并发写：两进程各写 50 条，总数正确 100，无 `database is locked` 失败
9. 检索延迟：20,000 条规模下混合检索（BM25 + 矩阵）P95 ≤ 10 ms（不含 embedding API）
10. **检索质量门禁**：用 ≥50 条 LLM 改写的"用户会怎么问"做评测集，混合检索 recall@8 ≥ 85%；且必须高于 dense-only 与 sparse-only 单路
11. **启动总耗时**：5000 条规模下（import numpy + BM25 重建 + 矩阵加载）≤ 500 ms
12. **settings 面板**：Settings → Plugins 显示 5 个字段（端点/模型/维度/稀疏方式 `bm25|model|off`/密钥），保存后 `config.yaml` 落到 `plugins.entries.hermes-minimem.settings.*`；密钥只进 `.env`，面板显示"已设置"
13. **配置变更行为**：改模型或维度后 `minimem_stats` 报 `needs_reembed > 0`，检索跳过旧向量且不报错
14. 迁移报告生成，复用向量数 = Mnemosyne 中同模型向量数
15. 影子跑 3 天零异常，门禁指标达标

---

## 12. 坑位记录

（实现阶段追加，格式：现象 / 根因 / 规避）

### 12.1 并发写会抛 `database is locked`

**现象**：两个连接同时追加（需求书 3.2 的验收场景），首版抛 `OperationalError: database is locked`。
**根因**：`sqlite3.connect(timeout=5.0)` 的 busy timeout 覆盖不到 `PRAGMA journal_mode=WAL`
切换与 `executescript` 建表这两种需要独占的时刻；两者同时开火必有一方失败。
**规避**：`busy_timeout=10000` + 写操作包 4 次指数退避重试（50/200/800 ms）；WAL 切换失败直接吞掉
（WAL 一旦设置就是持久的，失败只说明别人已经设过）。实测两线程各写 50 条 → 100/100 零丢失。

### 12.2 没有 numpy 时连写入都做不到

**现象**：Hermes 自己的 PM venv 里没有 numpy，`remember()` 直接 `ModuleNotFoundError`。
**根因**：`pack_vector` 用 `np.clip/np.rint` 做量化，文档向量归一化用 `np.linalg.norm`。
把"检索加速"的依赖泄漏到了"存储"路径。
**规避**：两处都改纯 Python（1024 维量化 <1 ms）。缺 numpy 只应降低检索质量，不应让插件变成只读。

### 12.3 BM25 行号与矩阵行号不能混用

**现象**：删过条目之后召回结果错位，返回的不是打分的那些记忆。
**根因**：矩阵只装 `vec_model`/`vec_dim` 与配置匹配的行（`all_vectors` 过滤），BM25 装全部行，
两套编号从第一条 stale 向量开始就分叉。首版把矩阵行号直接当 BM25 行号用。
**规避**：融合统一在 BM25 行号空间做，稠密命中经 `id -> bm25_row` 映射翻译；矩阵按 id 查表。

### 12.4 `provides_tools` 声明会让 doctor 报警

**现象**：`plugin.yaml` 声明 5 个工具后，`hermes plugins validate` 警告
"declares ... but register() did not register them"。
**根因**：memory provider 的工具经 `MemoryProvider.get_tool_schemas()` 每轮收集
（`agent/memory_manager.py`），不走 `ctx.register_tool`。声明与实际注册机制不符。
**规避**：删掉 `provides_tools`，在 yaml 里写清原因。

### 12.5 `hermes_cli.plugins_state._plugin_settings_entry` 是私有符号

**现象**：`validate` 报 catalog rule 9「private core imports」。
**根因**：该函数带下划线前缀，属于 core 内部，随版本可能移动。
**规避**：只依赖公开的 `load_config_readonly()`，`plugins.entries` 子树自己走一遍 dict；
再加一层纯标准库 `config.yaml` + `.env` 回退，连 `hermes_cli` 都导入不了时也能读配置。

### 12.6 BM25 在 20k 条时 68 ms，超阈值 7 倍

**现象**：20,000 条真实文本下稀疏路 p95 = 68 ms（阈值 10 ms）。
**根因**：查询里含高频词（"的记忆""配置"这类出现在几千条里的 bigram），它们的 idf 接近 0，
对排序没有任何贡献，却要遍历完整 postings。
**规避**：三层剪枝，实测 68 ms → 6.29 ms：
（a）跳过 `idf < 0.20` 的词（对排名的贡献可忽略）；
（b）只取 idf 最高的 8 个词参与打分（长 query 里低 idf 词改不了 top-k 顺序）；
（c）`doc_len` 从 dict 改成 list，内循环每次 posting 省一次 `dict.get`。
另：tokenize 从两趟正则改成一趟 `findall`、bigram 生成从 generator 换成推导式、
计数换成 C 层 `Counter`——这三项把 5,000 条重建从 472 ms 压到 367 ms。

### 12.7 启动 511 ms，差 11 ms 未达 §11 第 11 条

**现象**：5,000 条真实记忆冷启动 511 ms（阈值 500 ms）。
**根因**：186 万字 tokenize 出约 170 万 token，postings 插入 170 万次。拆开是 tokenize 108 ms、
postings 构建约 200 ms、idf 重算约 15 ms。纯 Python 在 170 万次哈希插入上的下限就在这里。
**现状**：**未达标，如实记录**。备选方案（改需求方定）：后台重建 BM25（启动降到约 145 ms）、
或砍掉中文 unigram（启动约 350 ms，代价是单字查询失效）。

### 12.8 迁移时向量可以直接复用（需求书 3.5 的假设成立）

**实测**：Mnemosyne `memory_embeddings` 表 1658 条，`model` 全部是 `qwen3.7-text-embedding-flash`，
维度全部 1024，且向量本身已归一化（norm 1.0002）。所以迁移不必调 API，直接量化入库。
**但**：`episodic_memory` 的 131 条在该表里没有对应向量（该表只服务 working_memory），
需要单独补嵌——补嵌 132 条（含 1 条 working_memory 也缺）花了 13 次批量调用。

### 12.9 实测修正需求书的两处数字

- §11 第 7 条原写"5000 条库文件 ≤ 2 MB"：int8 是 1 KB/条，光向量就 5 MB，加上中文正文
  （372 字/条 × 3 字节）实际 10.86 MB。已改为 ≤12 MB。
- §2.5 表的"数据库 ~2.5 MB / 18×"同源算错，按 1786 条实测 4.38 MB / 10× 修正。

### 12.10 需求书 §6.5 的前提错了：审查 fork 收不到 provider 通知

**现象**：`on_memory_write` 只在主 agent 前台调内置 `memory` 工具时触发；后台审查自动抽取的记忆
一条都进不来，库不会自动增长。
**根因**：`agent/background_review.py:949` 的 `_fork_init_kwargs` 显式 `"skip_memory": True`，
注释写明理由——"an external memory plugin scoped to the parent's session_id would leak the harness
prompt into the user's real memory namespace"。`agent_init.py:1256` 据此不给 fork 建
`memory_manager`，而 `inline_tool_executors.py:154` 是唯一的转发点，条件正是 `agent._memory_manager`。
**规避**：不再依赖通知，改为 `initialize` 之后在后台对账 `$HERMES_HOME/memories/MEMORY.md` 与
`USER.md`：按单独一行的 `§` 切条目、去掉 markdown 标题行、与库内精确文本比对、缺失的补嵌入库
（`origin=memory_tool`）。精确文本比对让重复执行不产生副本。
**已知限制**：只在启动时对账一次，长会话中新增的条目要等下次启动；要消除延迟需配
`auxiliary.background_review.extra_tools`（改 config，由用户决定）。

### 12.11 维度变化后矩阵对象不重建

**现象**：改 `embedding_dim` 后写入抛 `could not broadcast input array from shape (64,) into shape (1024,)`。
**根因**：`_rebuild_indices` 写的是 `if self._matrix is None: self._matrix = _VectorMatrix(dim)`，
矩阵已存在时复用旧对象；`_index_new` 又用配置维度而非矩阵实际宽度做判断，两者不一致时把错误维度的
向量塞进旧矩阵。
**规避**：维度变化时重建矩阵对象；`_index_new` 改比对 `self._matrix.dim`；`_VectorMatrix.append`
自己拒绝宽度不符的向量并 WARN（维度不符的行仍在 SQLite 与 BM25 里，只是暂时不能稠密打分）。

### 12.12 写入与索引不原子

**现象**：索引更新抛异常时，SQLite 里留下一条谁也搜不到的记录；镜像重试再插一条同文本。
**根因**：`remember` 先 `store.add` 再 `_index_new`，后者失败没有回滚。
**规避**：`_index_new` 包在 try 里，失败 `store.delete(mem_id)` 后重新抛出。

### 12.13 镜像队列的并发取件会重复插入

**现象**：一条镜像 add 在库里出现两行。
**根因**：重构 `_flush_pending` 时把"取 `_pending[0]`"和"pop(0)"拆成两个加锁块，
writer 线程与显式 force-flush 同时取到同一条，各 apply 一次。
**规避**：取件与出队合并为一次加锁（`item = self._pending.pop(0)`），失败时 `insert(0, item)`
放回并 `break`（同一轮内不重试同一条，否则死循环）。

### 12.14 CLI 命令名是 provider 名，不是简写

**现象**：README 里写 `hermes minimem stats`，实际命令是 `hermes hermes-minimem stats`，而且
**provider 不在位时这个命令根本不存在**。
**根因**：`hermes_cli/main.py::_attach_plugin_cli_command` 用 `subparsers.add_parser(cmd_info["name"])`，
而 `cmd_info["name"]` 来自 `discover_plugin_cli_commands()` 里的 `active_provider`——即
`memory.provider` 的字符串值。同时该函数按"当前激活的 provider"过滤，未激活时返回 []。
**规避**：文档按真实命令名写，并注明"只在 `memory.provider` 指向它时可用"。

### 12.15 测试里的 `hash()` 是不确定源

**现象**：审查子代理报告测试"第一次跑有 3 个失败，之后 13 次都复现不了"。
**根因**：`FakeEmbedder` 用 `hash(ch) % dim` 做字符分桶，而 Python 的 str hash 每进程加盐
（PYTHONHASHSEED），同一个文本在不同进程得到不同向量，排序类断言理论上会漂移。
（那次失败实测是并发改代码造成的，不是这个；但这个隐患是真的。）
**规避**：改成 `(ord(ch) * 2654435761) % dim` 的确定性映射；连续 10 次运行 + 3 个独立进程
比对向量已确认逐字节一致。

### 12.16 裸 URLError 逃逸"永不抛异常"契约（复核日抓到）

**现象**：真断网（DNS 失败/WiFi 掉线）时 recall 会把 `urllib.error.URLError` 抛出对话层，
违反 §5.5"记忆是增强项、不许把异常带给用户"。
**根因**：`_prepare_query_vector` 只 `except EmbeddingError`；而 `Embedder` 内部把 HTTP/解析
错误包装成 `EmbeddingError`，**传输层错误（URLError）却是 urlopen 直接抛裸的**，包装层拦不住。
测试里模拟断网用的正是 `EmbeddingError`，所以 43 项全绿仍漏网——降级路径测试必须用**真实
传输层的异常类型**探一次。
**规避**：查询向量与文档向量两处放宽 `except Exception`，保留 degraded 计数与 L2 熔断；
回归测试 `test_non_embedding_error_never_escapes_recall`（raise 裸 URLError 的假 embedder）。

### 12.17 贴线性能指标对机器负载敏感，单次采样不算数

**现象**：同一份代码，#9 检索 P95 空载 8.0–10.0 ms、带载（msedge 后台）10.2–13.2 ms；
#11 启动空载 525–537 ms、带载 646–864 ms。昨晚报 PASS 是最佳采样。
**根因**：纯 Python BM25 是 CPU 密集 + GIL 独占，任何后台进程都直接吃进尾延迟。
**规避**：贴线指标至少空载/带载各采一组，报告写区间不写单点；复核脚本已按此口径。

### 12.18 检索达标的真实含义

60 条改写 query：hybrid recall@8 **95%**、dense 93%、sparse 72%。
**hybrid 只赢 dense 1.7 个点**（1 条 query 的差异），统计上不显著。需求书 5.4 的样本量免责
声明在这里兑现了：方向可靠（三轮实验一致），幅度别当真。sparse 单独只有 72%，说明
"改写过的自然语言 query 靠关键词"是不成立的，混合检索的必要性由此成立。

---

## 13. v0.8 增量需求（2026-11-09 用户裁定）

> 本节是**新增量契约**，不推翻 v0.7 已验收的实现；它把 embedding 层从"单一百炼兼容端点"扩成三后端可切换，并把配置面纪律从 seed-once 升级为逐键补默认。与第 1/2/3/7.5 节冲突处以本节为准。

### 13.1 后端选择：`embedding_backend` 三选一

设置里新增 `embedding_backend`，取值 `openai | dashscope | local`，默认 `openai`（= v0.7 现行为，向后兼容，切换零迁移成本语义需明示，见 13.6）。

| 取值 | 走什么 | 依赖 | 典型用途 |
|---|---|---|---|
| `openai` | OpenAI 兼容 `POST {base_url}/embeddings`（百炼 compatible-mode 或任何兼容端点） | 标准库 urllib（已实现，`embed.py`） | 默认路，覆盖百炼与自建兼容网关 |
| `dashscope` | 官方 DashScope Python SDK 2.x `client.embeddings.create` | `dashscope>=2.0.1`（按需装，见 13.4） | 需要 `text_type` / `instruct` / `output_type=sparse` 的原生特性 |
| `local` | fastembed `TextEmbedding` / `SparseTextEmbedding`（ONNX，CPU） | `fastembed`（按需装，见 13.4） | 离线、零 API 费用、零 Key；首次使用需下载模型 |

面板上这是一个 `str` + `choices` 的 enum 字段。后端切换只影响新写入与实时查询；已入库向量带 `vec_model`/`vec_dim`，跨后端不一致自动标 stale（沿用 4.2 纪律），**禁止静默混用不同模型的向量空间**（沿用 5.4「模型选错会毁掉一切」教训）。

### 13.2 各后端在设置里暴露的字段

规则：每个后端只渲染自己用得到的字段（面板全量声明，读取/校验按后端裁剪）；固定维度的模型只填一个数、不给下拉，可选维度的模型下拉档位；`local` 没有 Key/base_url 概念，对应字段填了也不生效（stats 里说明被忽略）。

| 字段 | openai | dashscope | local |
|---|---|---|---|
| `embedding_model` | 文本，默认 `qwen3.7-text-embedding-flash` | 文本，默认 `qwen3.7-text-embedding` | 文本，默认 `BAAI/bge-small-en-v1.5` |
| `embedding_base_url` | 必填（到 `/v1` 或 `/compatible-mode/v1`，不带 `/embeddings`） | 可选（默认官方域名；WorkspaceId 专属域名可配） | 无（忽略） |
| `embedding_dim` | 可选档位下拉（见 13.5 表） | 同左 | 固定数：由 `list_supported_models()` 决定，用户只读展示 |
| `embedding_output_type` | 仅 `dense`（见 13.5） | `dense` / `sparse` / `dense&sparse` | `dense` / `sparse`（两套独立 fastembed 类，见 13.5） |
| `embedding_api_key` | 明文 str，默认空 | 明文 str，默认空 | 无（忽略） |
| `embedding_api_key_env` | 环境变量名（明文空时读这个） | 同左 | 无（忽略） |

Key 解析优先级（13.7 裁定）：`embedding_api_key`（明文）非空 → 用之；否则按 `embedding_api_key_env` 命名的环境变量；再否则按既有回退链 `MINIMEM_EMBEDDING_API_KEY` → `MNEMOSYNE_EMBEDDING_API_KEY` → `DASHSCOPE_API_KEY`。

### 13.3 DashScope SDK 官方文档事实（2026-11-09 实测 + 官方页面核对）

**设计 `dashscope` 后端务必以下列为准**，来源为百炼官方「向量化（Embedding）」「通用文本向量同步接口 API 详情」「OpenAI Embedding 接口兼容」「DashScope Python SDK 1.x→2.0 迁移参考」四页：

1. **新项目只用 SDK 2.0 写法**：`client = dashscope.Client(api_key=..., workspace=..., region=...)`，再 `client.embeddings.create(...)`。1.x 的 `TextEmbedding.call` / 模块级全局变量已进入冻结维护，仅作存量参考。实测本环境装到 `dashscope 2.0.4`，`dashscope.Client` 与 `client.embeddings.create` 签名核对通过。
2. **`client.embeddings.create` 的可用具名参数**（实测签名）：`model, input, text_type, dimension, output_type, instruct, parameters, timeout, api_key, workspace`。注意是 **`dimension`（单数）**——与 OpenAI 兼容端的 `dimensions`（复数）不同，这是最易踩的拼写差异；HTTP 原生调用则把 `dimension` / `output_type` / `text_type` 放进 `parameters` 对象。
3. **`Client` 构造支持 `base_url`**（实测签名含 `base_url`），可指向 WorkspaceId 专属域名 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1`；不填走官方默认。`workspace` + `region` 与 `base_url` 二者语义等价，选一种即可。
4. **`text_type`**：`document`（入库，默认）/ `query`（检索）。非对称检索任务必须区分。仅 DashScope SDK 与 API 支持，OpenAI 兼容端无此参数。
5. **`instruct`**：英文任务指令，官方称约 1%–5% 提升；**仅当 `text_type=query` 生效，且按条数计入 token 用量**。列为可选高级项，默认不填。
6. **`output_type`**：`dense | sparse | dense&sparse`，默认 `dense`；稀疏输出**只有 DashScope 接口支持，OpenAI 兼容端传 `output_type=sparse` 返回 200 但 embedding 为 `null`（静默失败）**（与 7.5.3 一致）。稀疏仅适用于 `qwen3.7-text-embedding`、`qwen3.7-text-embedding-flash`、`text-embedding-v3/v4`。
7. **稀疏向量形状**：数组，每项 `{index, value, token}`，index 为模型词表下标，可解释可调试；`dense&sparse` **一次调用同返两种，计费按输入 token 一次、延迟与纯 dense 相当**。
8. **维度档位 / 批量 / token 上限（官方表）**：

| 模型 | 可选维度 | 单批最大行数 | 单行最大 token |
|---|---|---|---|
| `qwen3.7-text-embedding` | 2560 / 2048 / 1536 / 1024(默认) / 768 / 512 / 256 | 20 | 128,000 |
| `qwen3.7-text-embedding-flash` | 1024(默认) / 768 / 512 / 256 | 20 | 128,000 |
| `text-embedding-v4` | 2048 / 1536 / 1024(默认) / 768 / 512 / 256 / 128 / 64 | 10 | 8,192 |
| `text-embedding-v3` | 1024(默认) / 768 / 512 / 256 / 128 / 64 | 10 | 8,192 |

9. **`raise_on_error`**：2.0 默认 True（失败抛 `DashScopeError`，含 `status_code`/`code`/`message`/`request_id`）。minimem 侧统一把 SDK 异常翻译进自己的降级链（13.6），不让 `DashScopeError` 逃逸到对话层（沿用 12.16 教训：真实传输异常类型也要探）。
10. **计费/合规**：`qwen3.7-text-embedding-flash` 返回 `prompt_tokens` 与 `total_tokens`，其余模型仅 `total_tokens`；单价见官方表，`instruct` 在 query 下按条计入。

> 结论：`dashscope` 后端比兼容端多拿到 `text_type`、`instruct`、真·模型稀疏三项官方独有能力；这是它相对 `openai` 后端存在的唯一理由，UI 描述要写清楚，别让用户以为换后端只是换个 URL。

### 13.4 依赖分发：Hermes PM 运行时按需安装（用户裁定「看看能不能拿 hermes pm 运行时按需安装」）

**已实测可行**：官方接缝 `pm.ensure_environment(name, requirements, root=<profile 自管目录>, explicit=True)`（`pm/client.py`，经 `pm` facade 导出）——任意 PEP 508 依赖、独立 generation、原子选择、旧代保留、返回该环境的 Python 解释器路径；插件用该解释器起子进程 helper，经 JSON/stdio 调用，**当前 gateway 进程无需重启，也不污染 Hermes 主 venv**。

- 实测记录（2026-11-09）：`pm.ensure_environment("dashscope-probe", ["dashscope>=2.0.1,<3"], ...)` 成功建 side env，装到 2.0.4，主运行时 `find_spec('dashscope')` 仍为 `None`；第二次相同 requirements 调用约 1 秒直接复用。
- **镜像坑**：默认 PyPI 源在本机解析超时，需显式给安装子进程 `UV_INDEX_URL=清华镜像`（env 只传给安装那次调用，不永久改进程环境）。
- 触发条件与授权：`local` / `dashscope` 是可选后端，**对应依赖默认不装**。用户选到未装的后端并保存 → 插件不得静默安装，须回报「需要安装 X，是否现在装」（本机软件管理红线）；确认后同步调用 `ensure_environment`，装好即用。`openai` 后端零依赖（标准库），保持 v0.7 轻量默认。
- 为什么不走「把 fastembed/dashscope 塞进共享 venv 的 `pip_dependencies`」：fastembed 会拖 onnxruntime（数十 MB + native wheel 平台门槛，与 D5「安装极轻」冲突），且 PM 对运行中进程不切代（`activate_dependencies` 契约）。「主 numpy 声明不变 + 重后端按需 side env」是当前唯一同时满足「轻」与「可切换」的路线。
- helper 进程边界：只读输入 JSON（texts / text_type / model / dim / output_type / key），只写 stdout（dense 数组 + 可选 sparse 项）；异常走退出码 + stderr；调用超时按 embedding 失败计入降级链（5.5）。

### 13.5 稀疏 / 稠密：用户可选，且稀疏真正参与检索

原 `sparse_mode` 扩展语义。稠密路与稀疏路的产物都进各自常驻结构（稠密=矩阵 5.2；稀疏=模型稀疏时复用 4.2 的融合，把 BM25 打分换成分数对齐的 dot）：

| 后端 | 支持 | 说明 |
|---|---|---|
| `openai` | 仅稠密 | 兼容端 `output_type=sparse` 静默返回 null（13.3.6）；面板对该后端隐藏稀疏选项 |
| `dashscope` | `dense` / `sparse` / `dense&sparse` | 仅 13.3.8 四款模型支持；`dense&sparse` 一次调用同返，计费不增 |
| `local` | 稠密（`TextEmbedding`）+ 稀疏（`SparseTextEmbedding`，SPLADE++/BM25 等官方支持表）| 两套独立模型；选 `sparse` 需指定稀疏模型名（官方支持表 `Qdrant/bm25`、`prithivida/Splade_PP_en_v1`）；`dense+sparse` = 各建一个模型实例，**双份下载、双份内存**，面板要写清 |

- 本地 BM25 独立存在：`bm25_sparse`（沿用 v0.7 默认，零模型）与「模型稀疏」并列可分别开关。融合规则不变（5.4）：`α·norm(dense) + (1−α)·norm(sparse) + 0.1·imp`，sparse 来源不管是 BM25 还是模型稀疏都走同一 min-max。
- `local` 固定维度处理：fastembed 模型的维度由 `TextEmbedding.list_supported_models()` 决定（如 `bge-small-en-v1.5`=384），面板展示只读，`embedding_dim` 对该后端忽略用户输入（沿用 13.2）。
- 「`embedding_output_type` 是否包含 sparse」只影响该后端是否额外产稀疏向量；用户另可完全不用模型稀疏、只用本地 BM25。

### 13.6 降级链扩展（覆盖 5.5，按后端分叉）

| 层 | 条件 | 行为 |
|---|---|---|
| L1 | 后端可用（openai=API 通；dashscope=helper 起得来；local=模型已载入）+ numpy | 完整混合检索 |
| L2 | 所选后端调用连续失败 3 次 | 纯 BM25/关键词，WARN 一次，本会话不再试该后端 |
| L3 | 后端依赖缺失（如 local 但 fastembed 未装且用户拒绝装 / dashscope helper 起不来 / numpy 缺失） | 纯关键词 + startup WARN，**不自动跨后端兜底**（静默换后端=用错 embedding 空间） |
| L4 | 全失败 | 空串 + ERROR 一次 |

后端故障绝不静默切换到另一个后端的模型（会产生跨空间混用，13.1 铁律）；降级只到关键词路，不降级到「另一套向量空间」。

### 13.7 配置面纪律变更：逐键默认补齐（覆盖 7.5 / 旧 seed-once）

用户 2026-11-09 裁定，并同步进插件作者规范笔记作为通用规范：

1. 初始化时遍历本插件 `config_schema` 的**每一个键**；判定「缺失」的唯一标准是 `key not in settings`。用户显式存的 `""` / `false` / `0` / `null` **都不是缺失，绝不覆盖**。
2. 缺失即调官方 writer 补默认。minimem 是 memory provider，`ctx` 无 `set_config` → 直接调 `hermes_cli.plugins_settings.save_plugin_settings(plugin_id, plugin_dir, {key: default})`，其底层与 Settings 面板同一 `save_plugin_setting`（schema 校验 + 跨进程锁 + 原子读改写）。**不得手改 config.yaml / 自拼 YAML**。
3. 无 `default` 的键不补；API Key 明文与 env 名只补**空字符串**，绝不生成或回显密钥值。
4. 单键写失败只 WARN + 本次内存用默认，下次启动再试，绝不抛进对话层。写前复读、进程内守卫防重入。
5. 与旧规范的行为差：用户删单键 → 下次启动**会被重新补回**（旧 seed-once 是永久缺失）。这是本次有意的改判。

### 13.8 验收增补（接 11 节编号）

- **A16** 三后端各跑通一次「写入→重启→召回」，向量 `vec_model` 记录后端+模型，跨后端旧向量自动 stale、不混用。
- **A17** 逐键补默认：新建 settings 全缺 → 遍历补齐；手动把某键改成 `""`/`false` 保存 → 重启不覆盖；删单键重启 → 补回。全程只经官方 writer，用文件 sha/mtime 佐证非手改。
- **A18** Key 优先级 13.2 全路径（明文 / env 名 / 三级回退）各测一次，日志与 stats 只报「来源 + 已设置」不回显值。
- **A19** 后端依赖缺失且用户未授权 → 插件不装、明确回报；授权后 `ensure_environment` 装好即在当前进程用（不重启 gateway），主 venv `find_spec` 保持缺失。
- **A20** 维度/批量按 13.3 表校验：`openai`+`qwen3.7-text-embedding-flash` 接受 512 等档位（官方文档标只 v3/v4，本端实测可用，按返回值入库，沿用 3.4/7.5.3）；超限批量 400 要翻进 L2。
- **A21** `dashscope` 后端 `dense&sparse` 一次调用同返，sparse 项 `{index,value,token}` 落融合路，验证 recall@8 与只用 BM25 的差异（≥50 条改写 query，沿用 5.4 n 免责）。
- **A22** `local` 后端离线可用：零网络、载入 `bge-small-en-v1.5` 完成一写一查。

### 13.9 未定 / 待办

- 模型稀疏（dashscope / local）当前**尚未进代码**（v0.7 面板 `sparse_mode=model` 会 WARN 落回 bm25）；本节把它升级为一等公民，实现工作量：helper 子进程协议 + sparse 打分对齐 min-max + local 双模型加载。
- `instruct` 高级项默认关闭，待用户确认是否要在面板暴露。
- 与 Mnemosyne 的同评测集 top-3 对比仍未跑（v0.7 遗留项）。

## 14. v0.8 实现状态（2026-11-10）

代码重构已完成，当前状态如下；本节只记录实现事实，不替代上面的需求契约。

| 项 | 状态 | 实现位置 |
|---|---|---|
| 三后端 enum 与按后端默认模型映射 | 已实现 | `plugin.yaml`、`config.py` |
| 逐键默认补齐（仅补缺失键，官方 writer） | 已实现 | `config.ensure_defaults()` |
| 明文 Key → env 名 → 三级回退，只报来源 | 已实现 | `config.resolve_api_key()` / stats |
| `openai` v0.7 兼容 | 已实现 | `embed.py` 仍为默认后端 |
| DashScope SDK 2.x sidecar、`dimension`/`text_type`/`output_type` | 已实现 | `sidecar.py` / `sidecar_worker.py` |
| fastembed 稠密与 learned-sparse sidecar | 已实现 | `sidecar.py` / `sidecar_worker.py` |
| PM side-env 按需安装与 `allow_pm_install=false` 权限门 | 已实现 | `sidecar.backend_python()` |
| SQLite v2 后端标签、稠密/稀疏持久化、v1 自动备份迁移 | 已实现 | `store.py` |
| 模型稀疏倒排与三通道融合 | 已实现 | `provider._ModelSparseIndex` / `_recall_locked` |
| 后端连续失败停路与不跨后端兜底 | 已实现 | provider L2/L3 路径 |
| CLI、README、迁移/实测脚本适配 | 已实现 | `cli.py`、`README.md`（迁移脚本 `export_mnemosyne.py` 未随本仓库发布，见文末脱敏记录） |
| 离线测试 | **81/81 通过**（44 v0.7 + 37 v0.8，含审查回归 5 例，见 14.2） | `pytest -q` |

### 14.1 验收进度（2026-11-10 实测更新）

| 项 | 状态 | 实测证据 |
|---|---|---|
| A16 三后端写入→重启→召回 | **通过** | openai=兼容端 1024d 往返正常；dashscope=PM side-env 256d `dense&sparse` 写入→重建→召回 top1 正确、`needs_reembed=0`；local=`BAAI/bge-small-en-v1.5`@384d 写入→重启→召回 top1 正确（`vec_backend=local`）。脚本：`cache/scratch/minimem_v08_dashscope_smoke.py`、`minimem_v08_local_smoke.py` |
| A17 逐键补默认 | 单测通过 | 官方 writer 路径、缺键补回、显式 `""/false/0` 不覆盖均覆盖；实际 `config.yaml` 启停对照留待插件启用时做 |
| A18 Key 优先级 | 单测 + 部分实测 | dashscope/local 烟测均显示 `api_key_source=env:MNEMOSYNE_EMBEDDING_API_KEY`，不回显值；明文与指定 env 名两路待面板启用后复验 |
| A19 依赖门与 side-env | **部分通过** | 权限门单测通过；dashscope 复用既有 `dashscope-probe` side env、主 venv 无该包；本轮未触发新安装（`allow_pm_install` 保持 false） |
| A20 维度/批量 | **通过（openai）** | 兼容端 `dimensions=512` 实返 512、20 行批量成功；dashscope `dimension=256` 单数参数实返 256 |
| A21 dense&sparse 融合差异 | 通路验证 | DashScope 一次调用同返（sparse 约 3218 项/条），模型稀疏索引 2 docs；与 BM25 的 60 条改写 query 对比集待插件在位时跑 `eval_retrieval.py`（已支持 `model_sparse` 模式） |
| A22 local 离线一写一查 | **通过** | fastembed 常驻 worker 完成一写一查与重启召回；模型缓存复用后加载约数秒。注意：模型**下载**发生在首跑（走 HF 网络），推理全程本地 ONNX |
| v1→v2 真实数据复核 | **通过** | 1786 行副本 schema 1→2 自动迁移 + 备份 `minimem.db.v1-backup-*`；openai 口径 stale=0、矩阵载入 1786；切 dashscope 口径 1786 全标 stale（跨空间不混用铁律生效）。原件未动 |

- 修复并回归：sidecar `-I` 忽略 `PYTHONUTF8` 导致中文 surrogate 崩溃 → worker 启动改 `-X utf8=1`；修复后全套离线测试 **76/76 通过**，DashScope 中文烟测通过。
- provider 仍保持 `mnemosyne`；本次只部署插件副本，不改 `memory.provider`、不改启用开关。

### 14.2 独立代码审查结论与修复（2026-11-10，子代理 review）

审查判定**无 blocker**（索引空间翻译、跨后端混用、迁移数据丢失三大风险面经核成立）。发现 3 major + 12 minor，处理如下：

**已修复并回归（新增 5 个 RED→GREEN 测试，全套 81/81 通过；DashScope/local 烟测复跑通过）：**

| 编号 | 缺陷 | 修复 |
|---|---|---|
| M1 | worker 内第三方库往 stdout 打印一行噪声即污染应答队列，下一个请求因 id 不符误杀常驻 worker（丢已载入 ONNX 模型 + 冤枉一次 L2 失败计数） | `_read_loop` 丢弃非 JSON 行；`request()` 改为按自己 request_id 排空等待，旧请求迟到的应答直接丢弃 |
| M2 | 明文 Key 在 settings 不在 os.environ，`_sanitise` 只认识 env 值 → worker 崩溃的 stderr 尾与工具异常回显可能带出密钥 | `SidecarProcess` 接收 `secrets`（含 settings 明文 key）统一脱敏；provider `_redact()` 兜住工具层异常文本；实测烟测输出无 key 值 |
| M3 | 两个进程并发首开 v1 库：后进者 ALTER 撞 duplicate column 直接炸 `initialize()` | `_migrate_v1` 捕获 duplicate-column 落 `_ensure_v2_columns` 幂等补列 |
| m5 | 后端已判死（L2）后每次写入仍重试完整退避链 | `_embed_document` 短路 `_backend_alive` |
| m6 | 无 key 配置下每次召回都烧 401+重试 | `_prepare_query_embedding` 检查 `is_configured()` 走关键词层 |
| m7 | worker 崩溃诊断恒为 `NoneType: None`（except 块外调 print_exc） | `traceback.print_exc()` 移进 handler；退出路径先等 stderr 排空再报尾 |
| m8 | v1 迁移备份不折 WAL，热库备份可能缺最新写入 | 备份前 best-effort `wal_checkpoint(TRUNCATE)` |

**留作已知限制（未修，启用前需用户拍板）：**

- m9：`sparse_mode=model` 时 BM25 与模型稀疏是二选一而非三通道并存（§13.5 双开要求未完全达成）；模型稀疏 min-max 只在 top-k 内归一，垫底名次被压 0。
- m10：`fusion_alpha` 读取了但没进 plugin.yaml/DEFAULTS → 面板不可达、逐键补齐覆盖不到它。
- m13：local 后端填了表外模型名+手配 dim 时稠密路静默全 stale。
- m15：`_temporary_index_url` 短暂改进程 `UV_INDEX_URL`（finally 还原；并发 PM 安装理论上互见）。
- m4/m11/m12/m14 属小毛刺，见审查全文（会话内委派审查记录，未随仓库发布）。

**部署事实**：插件目录整体放在 `$HERMES_HOME/plugins/hermes-minimem/`；启用与切换 `memory.provider` 由用户亲手操作并重启 gateway（插件不改宿主配置）。

## 15. v0.8.1 增量：workspace/region 路由 + reembed/导入（2026-11-10）

**DashScope 原生路由**。SDK 2.0.4 的 `Client(workspace=, region=)` 拼
`https://{ws}.{region}.maas.aliyuncs.com`；plugin.yaml/config.py 增 `dashscope_workspace`
（str）与 `dashscope_region`（六选一下拉，validate 校验），sidecar 的 worker_config 在有
workspace 时发 `workspace/region` 且 base_url 置 None（无 workspace 才回退 legacy/base_url），
worker `_ensure_dashscope` 按此组 kwargs。region 仅在 workspace 存在时下发——无 workspace
时默认 region 不该泄漏进 config（测试抓到）。用户曾想杜撰 `dashscope://region@ws` 伪 URL
塞 base_url，裁定不加：双入口优先级规则是隐性负担，字段名自解释、报错可定位，成本已付。

**`reembed` 子命令**（`hermes hermes-minimem reembed`）。store 抽出 `_stale_where` 供
count_stale/stale_ids 共用；provider.reembed_all(batch/dry_run/every/verbose) 分批嵌、逐行
update（L2 归一化同写路径），失败行保留旧向量继续跑，结束后 `_reindex_all()` 刷活索引。
`--every` 用于「旧向量名义上还匹配但想整体搬家」场景。

**导入模式**（`reembed <file.jsonl>`）。provider.import_documents：文本必需，kind 非法落
`--default-kind`，importance 夹 0..1；source 里记录自带 origin 保留（`setdefault`），另加
`imported_by` 戳——测试抓到第一版无脑覆盖 origin 的语义错误。去重回写后曾走 remember()
导致同批双嵌，改为复用批量 embed 结果 + `store.add`/`_index_new`（锁内成批写）。store.update
增加 vec 长度 vs vec_dim 不等即 raise 的护栏（防 caller bug 污染 int8 矩阵）。

**export_mnemosyne.py**（迁移辅助脚本，未随本仓库发布）。mnemosyne 库（working+episodic 两表）→ minimem JSONL，
实测导出 521 条（importance≥0.5 + 类型映射表过滤；unknown/context 默认全弃，`--include-types`
可点名放行落 fact）。映射：fact→fact、preference/instruction→preference、
error/decision/observation/learning/artifact→insight。坑：episodic 无 `pinned` 列；
`--include-types unknown` 第一版永远进不去（未映射类型先死在 membership 检查）→ 重写成
extra_types 白名单语义。sqlite URI 须 `file:///` 三斜杠，`file:D:/…` 被当 URI authority。
向量一律不搬（§no-mixing），导入即按当前后端嵌。

**验证**：pytest 90/90（新增 test_v08_bulk.py 8 例：stale 洗白+召回命中、dry-run 零接触、
--every、失败批不空白、未配置拒入、kind 映射+source 保 origin、去重幂等、后端挂 0 写入）；
实 CLI `reembed --dry-run` 接通（hermes 外壳首跑会先做依赖同步，可能吃掉数分钟）。
已完成部署与逐文件哈希对齐。

**待用户操作**：面板设 backend=dashscope + workspace/region + output_type=dense&sparse +
sparse_mode=model + allow_pm_install=true → 重启 gateway → `reembed --dry-run` 看 21 条
openai 旧行 → `reembed` 洗白 → `reembed $HERMES_HOME/hermes-minimem/mnemosyne-export.jsonl`
导入 521 条（可先 `--batch 20` 提速）。

**坑（§15 补，2026-11-10 作者实跑报阿里云 500）**：`qwen3.7-text-embedding-flash` 文档写
单请求 20 条，真实语料上 20 条整批**稳定触发服务端** `InternalServerError: Receive batching
backend response failed (code=InternalError, 500)`——同一批连试 3 次全挂，切 10 条/批全过；
521 条语料 batch=20 挂 7/27 批、batch=8/10 挂 0。错误与内容无关（逐条嵌 0 失败）、与大小
弱相关（长文本批更易中，但 2.4K 字符小批也挂），纯服务端 batching 层毛病。已把
DASHSCOPE_BATCH_CAPS 两个 qwen flash 系模型从 20 压到 10（带实测注释），README/CLI help
同步。作者那轮 500 发生在旧 cap 生效期间：120 条导入成功、7 批被 errors 计数吞掉——数据无损
（失败批不落半行），重跑 `reembed <file>` 补剩余（去重会跳过已导入的）。

---

## 16. 脱敏记录（公开版 v0.8.1，2026-11-10）

本版由私有母版脱敏生成，除下列各项外与母版同版本逐行一致：

- 称呼统一：「主人」→「用户/作者」（全部；母版为用户对助手的称呼，无技术含义）。
- 私有 WorkspaceId 域名（`llm-xxx.cn-beijing.maas.aliyuncs.com`，母版 3.5 节）→ `{WorkspaceId}` 占位符。
- 密钥复用来源一行的账号策略备注（母版 3.5 节）→ 仅保留环境变量名。
- 委派审查日志 id（母版 14 节遗留限制表）→ 「会话内委派审查记录，未随仓库发布」。
- 部署事实与待办两节中的本机绝对路径、内部备份目录名、开发工作区状态（母版 14/15 节）→ 收敛为 `$HERMES_HOME` 相对表述或删去；`scripts/deploy.py`、`scripts/export_mnemosyne.py` 为开发侧脚本，未随仓库发布。
- 作者技能库引用（「同步进 X 技能」，母版 13.6 节）→ 「插件作者规范笔记」。
- 记忆条数、导入语料规模等为本机库实测数字，保留（无识别性）。
