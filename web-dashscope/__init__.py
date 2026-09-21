from __future__ import annotations

import os
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

# ============================================================
# 阿里云百炼 MCP 直连层（纯标准库，不装 mcp sdk 也能跑）
# 实测确认（2026-09-15，probe_mcp.py）：
#   EnhancedSearch/mcp → 工具 search_pro，入参 {query}，
#     返回 content[0].text = JSON 字符串 {pages:[{title,url,snippet,hostname,...}],...}
#   WebFetch/mcp        → 工具 web_fetch，入参 {url}，
#     返回 content[0].text = 页面 Markdown 正文；失败时 isError=true + 错误 JSON
#   协议：Streamable HTTP，initialize → notifications/initialized → tools/call，
#   会话靠响应头 mcp-session-id 维持，响应体可能是 SSE(data: 行) 或裸 JSON。
# ============================================================

_mcpServers={
    # 服务名 → 端点（和 install-cmd.txt 里 Claude Code 用的那两个一模一样）
    "EnhancedSearch": "https://dashscope.aliyuncs.com/api/v1/mcps/EnhancedSearch/mcp",
    "WebFetch": "https://dashscope.aliyuncs.com/api/v1/mcps/WebFetch/mcp",
}

_mcpSessionCache={}  # 服务名 → mcp-session-id（进程级复用，省掉每次搜索都握手一遍）

def getApiKey():
    """从环境里拿百炼通用 Key（Hermes 启动时会加载 Data/.env）。"""
    return(os.environ.get("DASHSCOPE_API_KEY", "").strip())

def mcpPost(url: str, body: dict, sessionId: str, timeout: int):
    """
    往 MCP 端点 POST 一个 JSON-RPC 包，返回 (会话id, 解析后的响应 dict 或 None)。
    响应可能是 SSE(text/event-stream，结果藏在 data: 行) 或裸 JSON，两种都吃。
    """
    import json
    import urllib.request
    headers={
        "Content-Type": "application/json",
        # 必须同时声明能吃两种格式，只发一种服务端可能按另一种回，解出来全是乱码
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer "+getApiKey(),
    }
    if(sessionId):
        headers["Mcp-Session-Id"]=sessionId
    req=urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    resp=urllib.request.urlopen(req, timeout=timeout)
    sid=resp.headers.get("mcp-session-id") or sessionId
    raw=resp.read().decode("utf-8", "replace")
    result=None
    for line in raw.splitlines():
        if(not line.startswith("data:")):
            continue
        try:
            msg=json.loads(line[5:].strip())
        except Exception:
            continue
        # SSE 流里可能有 progress/log 之类的杂包，只认真正带 result/error 的那条
        if("result" in msg or "error" in msg):
            result=msg
    if(result is None and raw.strip()):
        try:
            result=json.loads(raw)
        except Exception:
            pass
    return(sid, result)

def ensureMcpSession(server: str):
    """握手建立（或复用）某个 MCP 服务的会话，返回 session id。"""
    if(server in _mcpSessionCache):
        return(_mcpSessionCache[server])
    url=_mcpServers[server]
    sid, init=mcpPost(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "hermes-web-dashscope", "version": "1.0.0"},
        },
    }, None, 20)
    if(init is None or "result" not in init):
        raise RuntimeError("MCP initialize 失败: "+server)
    # 协议规定 initialize 之后必须补一发 initialized 通知，不然 tools/call 会被晾着
    mcpPost(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, 20)
    _mcpSessionCache[server]=sid
    return(sid)

def callMcpTool(server: str, tool: str, args: dict, timeout: int = 60):
    """
    调一次 MCP 工具，返回 content[0].text 的原始文本。
    会话失效（服务端隔一段时间会扔旧 session）时自动重握手重试一次；
    isError=true 时抛 RuntimeError，让上层按原样记 error。
    """
    import json
    body={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": tool, "arguments": args}}
    sid=ensureMcpSession(server)
    try:
        _, resp=mcpPost(_mcpServers[server], body, sid, timeout)
    except Exception:
        raise
    if(resp is None):
        raise RuntimeError("MCP 无响应: "+server+"/"+tool)
    if("error" in resp):
        # 会话被判死刑（常见 -32001/404 语义）→ 清缓存重握手，只重试一次
        _mcpSessionCache.pop(server, None)
        sid=ensureMcpSession(server)
        _, resp=mcpPost(_mcpServers[server], body, sid, timeout)
        if(resp is None or "error" in resp):
            raise RuntimeError("MCP 调用失败: "+str((resp or {}).get("error", "no response")))
    result=resp.get("result", {})
    content=result.get("content") or []
    text="".join(c.get("text", "") for c in content if c.get("type") == "text")
    if(result.get("isError")):
        raise RuntimeError("MCP 工具报错: "+text[:300])
    return(text)

def stripHighlightTags(s: str):
    """search_pro 的 snippet 里带 <em> 高亮标签，Hermes 消费端不需要，剥掉。"""
    import re
    return(re.sub(r"</?em>", "", s or ""))

def firstMarkdownHeading(md: str):
    """从 web_fetch 返回的 Markdown 里抠第一个 # 标题当 title，没有就用域名凑。"""
    import re
    m=re.search(r"^#{1,3}\s+(.+)$", md or "", re.MULTILINE)
    return(m.group(1).strip() if m else "")

class DashScopeWebSearchProvider(WebSearchProvider):
    """Routes web_search/web_extract to Aliyun Bailian EnhancedSearch/WebFetch MCP."""

    @property
    def name(self) -> str:
        # Stable id used in web.search_backend / web.extract_backend / web.backend
        # config keys. Lowercase, no spaces; hyphens permitted.
        return "dashscope"

    @property
    def display_name(self) -> str:
        # Human label shown in `hermes tools`. Defaults to `name`.
        return "DashScope MCP (EnhancedSearch + WebFetch)"

    def is_available(self) -> bool:
        # Cheap check — MUST NOT make network calls (runs on every `hermes tools` paint).
        return(bool(getApiKey()))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        import json
        response=[]
        idx=1
        try:
            text=callMcpTool("EnhancedSearch", "search_pro", {"query": query[:500]}, timeout=30)
            payload=json.loads(text)
            pages=payload.get("pages") or []
            for res in pages[:max(1, int(limit))]:
                response.append({
                    "title": res.get("title") or "No Title Provided",
                    "url": res.get("url", ""),
                    "description": stripHighlightTags(res.get("snippet")) or "No Description Provided",
                    "position": idx
                    })
                idx = idx + 1
        except Exception as e:
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "data": {
                "web": response
            }
        }

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        # 契约：必须返回纯列表（web_tools.py 消费端直接 enumerate + .get("raw_content")，
        # 包 success/data 外壳会让 "success".get("error") 直接 AttributeError 炸掉 web_extract）
        respDat=[]

        for url in urls:
            dat={"url": url, "title": "", "content": "", "raw_content": ""}
            try:
                md=callMcpTool("WebFetch", "web_fetch", {"url": url[:2000]}, timeout=60)
                dat["raw_content"]=md
                dat["content"]=md
                # MCP 只给正文 Markdown，标题能从首个 # 标题抠就抠，抠不到用域名顶着
                host=url.split("/")[2] if len(url.split("/")) > 2 else url
                dat["title"]=firstMarkdownHeading(md) or host
            except Exception as e:
                # 单个 URL 失败只标记该项 error，其他 URL 照常返回（不整批失败）
                dat["error"]=str(e)
            respDat.append(dat)

        return respDat


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    ctx.register_web_search_provider(DashScopeWebSearchProvider())
