# web-bingsearch v1.3.1 部署交接书

日期：2026-11-12
状态：代码完成、测试全过，**部署与 config 修改由主人执行**（D10 规矩）。

> v1.3.1 增量（质量实测驱动）：蜜罐回退默认改开；CJK 边界分词+品牌型号合并+纯中文前3字兜底；高置信度垃圾判定器；选词逆长度评分。8 查询出厂实测 48/68 相关、6/8 真结果（v1.3 默认仅 12/70）。e2e 回归（GIL 击杀/缓存/extract/进程内兜底）全过，官方 validate 通过。

## 交付物（本目录）

| 文件 | 说明 |
|---|---|
| `__init__.py` | v1.3 主代码（731→757 行）。v1.0 函数区原字节保留，provider 类最小接线，新函数块追加 |
| `_search_worker.py` | 子进程 worker（D9 隔离执行端，官方 ddgs 同款信封协议） |
| `pyproject.toml` | **PM 新格式依赖声明**：`[project].dependencies=["primp>=2.0.1"]`，uv workspace 成员 |
| `plugin.yaml` | v1.3.0 + config_schema（7 个开关，Settings→Plugins 面板） |
| `README.md` | v1.3 全貌 + 每选项改什么/实测依据/会引发什么 |
| `PROMPT.md` | 需求书 v0.3（三目标实测裁定 + 坑位记录） |
| `*.bak-v1.0` | v1.0 原件备份（__init__/plugin.yaml/README） |

## 已验证（真实工具输出）

- **离线**：假 ctx 接线、缺键补齐（显式 ""/false/0 不覆盖）、解析器跨 12 变体全过（桌面 10/移动 5，移动版真标题）、清洗/判读单测。
- **真网 e2e**：4 查询子进程搜索全 success（position 1 起、limit 生效）；extract 纯列表+单 URL 失败不整批炸；**GIL 钩子 4s 超时击杀子进程确认死亡**；缓存二次调用 0ms；进程内兜底可用。
- **蜜罐回退**：长查询 `how to fix windows 11 update error 0x800f0922` 关回退=fix 词典垃圾页，开回退=5/5 相关（微软官方+知乎）。
- **官方 validate**：`hermes plugins validate web-bingsearch` 全过，`python dependencies — 1 requirements from pyproject`。唯一警告：`tools.interrupt` private-import（官方 ddgs 同款，try 守卫已降级，可接受）。

## 部署步骤（主人执行）

1. **备份现役**（可选，现役=v1.0）：
   ```
   copy "D:\01-AI\Hermes-Agent\Data\plugins\custom-plugins\web-bingsearch" 到别处
   ```
2. **覆盖部署**：把本目录的 `__init__.py`、`_search_worker.py`、`pyproject.toml`、`plugin.yaml`、`README.md` 复制到
   `D:\01-AI\Hermes-Agent\Data\plugins\custom-plugins\web-bingsearch\`
   （`PROMPT.md`/`HANDOFF.md`/`.bak` 可不部署）。
3. **装依赖（PM 新格式）**：
   ```
   hermes pm install
   ```
   PM 从 config 发现已启用插件作为 workspace 成员，把 primp>=2.0.1 装进 Hermes 环境。
   - 本机 pypi 直连超时：先设 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`（或 pip.conf index-url，PM 自动桥接给 uv）。
   - 首次可能弹依赖同意询问，确认即可。
4. **切后端**（config.yaml，主人改）：
   ```yaml
   web:
     search_backend: bing-search
     extract_backend: bing-search   # 可选，extract 也走 bing
   ```
   （现役是 dashscope；bing 已在 plugins.enabled，无需再 enable。）
5. **重启 Hermes**，`hermes plugins doctor web-bing-search` 应通过。

## 回滚

把 `.bak-v1.0` 三个文件改回原名覆盖部署目录，config 切回 dashscope，重启。primp 依赖留着无害。

## 已知局限（诚实声明）

- 长查询蜜罐仍在：默认（回退关）可能拿泛泛结果；要稳就开 `honeypot_truncation_fallback`（代价：查询语义被削）。
- 钉死桌面指纹仍偶发移动页（5 条）：解析器已容错，结果数波动是变体差异。
- extract 是生肉 HTML，token 消耗大（正文解析未做，U2 候选）。
- 验证码/IP 级硬封禁不过：退避重试救不了，歇几分钟或换出口 IP。
