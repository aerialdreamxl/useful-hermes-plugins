# t2i-bailian — 阿里云百炼文生图后端

把百炼（Bailian/DashScope）的 `multimodal-generation` 文生图 API 接成 Hermes 原生 `image_generate` 工具的后端。作者亲测 **Token Plan 可用**。

注册为 image gen provider（`ctx.register_image_gen_provider`），provider id：**`t2i-bailian`**。

## 前置条件

百炼通用 API Key（`sk-` 开头）+ 对应端点 Base URL。注意用的是**独立变量名**，与 web 搜索插件的 `DASHSCOPE_API_KEY` 互不干扰（方便分开管额度/权限，值填一样的也行）：

```
# $HERMES_HOME/.env
DASHSCOPE_T2I_API_KEY=sk-xxx…xxxx
DASHSCOPE_T2I_BASE_URL=https://dashscope.aliyuncs.com
```

## 安装与启用

1. 目录放入 `$HERMES_HOME/plugins/t2i-bailian/`（可嵌一级分类目录）。
2. `config.yaml` → `plugins.enabled` 加 `t2i-bailian`。
3. `config.yaml` → 选择后端：
   ```yaml
   image_gen:
     provider: t2i-bailian
   ```
4. 重启 Hermes。之后 `hermes tools` 里配置文生图时会看到 "Aliyun Bailian"（badge: pay as you go），或在会话里直接让 agent 调 `image_generate`。

## 模型清单

| 模型 id | 速度 | 定位 |
|---|---|---|
| `wan2.7-image` | ~5s | 默认；快速出图迭代 |
| `wan2.7-image-pro` | ~30s | 最高保真 |
| `qwen-image-3.0-pro` | ~30s | 最高保真 |

## 画幅

按 Hermes 标准 aspect_ratio 映射到固定尺寸：`landscape → 1696*960`、`portrait → 960*1696`、其余 → `1280*1280`。

## 能力边界（诚实清单）

- **纯文生图**：`capabilities()` 声明 text-only，传参考图/图生图参数会被明确拒绝（`Reference image input is NOT ready for use`），不会静默忽略。
- 返回的是百炼侧的图片 URL（有效期内），由 Hermes 工具层负责落地。
- 实现方式比较粗犷：拼一条 `curl` 命令走 shell 执行（**这是刻意的最小依赖路线**，代价是运行瞬间 API key 会出现在本机进程命令行里，多用户机器上请自行斟酌）。超时 120s。
- 计费跟着百炼账号按量走。
