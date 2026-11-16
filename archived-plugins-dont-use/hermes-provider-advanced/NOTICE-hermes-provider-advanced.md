NOTICE — hermes-provider-advanced 移植来源声明
================================================================

本插件部分模块移植自以下上游开源项目，标注见各文件头部 docstring
（路径均相对 custom-plugins/hermes-provider-advanced/）：

1. CLIProxyAPI (CPA) — https://github.com/router-for-me/CLIProxyAPI
   许可证：MIT（全文见同目录 LICENCE.cpa）
   移植模块：
   - advlib/antigravity_infer.py / antigravity_resp.py / antigravity_client.py
     （translators 移植）
   - advlib/gemini_schema_clean.py（JSON-schema 清洗引擎的忠实移植）
   - advlib/fingerprints/cpa.py（imitation 值取自 CPA 各平台执行器源码，
     标注精确到文件:行号）
   - OAuth 登录流（adapters/ 交互式登录部分，参照 CPA Go 源码移植）
   - OAuth 令牌刷新报文（adapters/claude.py refresh 的端点/JSON 体/axios 头，
     参照 internal/auth/claude/anthropic_auth.go）
   - 并发刷新合并（advlib/oauth_gate.py single_flight，语义参照其
     errgroup singleflight 用法）

2. Sub2API — https://github.com/Wei-Shaw/sub2api
   许可证：LGPL-3.0（代码本体未链接，仅按常量出处引用）
   引用模块：
   - advlib/fingerprints/sub2api.py（header 常量取自其
     openai_codex_identity.go / internal/pkg/claude/constants.go /
     internal/pkg/antigravity/oauth.go，标注精确到行号）

除上述模块外的全部代码为本项目原创（Hermes 插件接缝、账号存储、
proxy 路由、transport 补丁等），以 GPL-3.0 发布，与 MIT/LGPL-3.0 兼容。

使用提示：OAuth 订阅通道与客户端指纹功能可能受各 AI 提供商服务条款
限制，使用者需自行评估并承担相应合规责任。
