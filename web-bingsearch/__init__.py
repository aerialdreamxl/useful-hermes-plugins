from __future__ import annotations

import os
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

def crawlBing2RawRSS(queue: str):
    """
    以某种聪明的方式爬Bing的RSS Feed不被抓
    queue: 字符串，你要搜索的东西。
    """
    import primp
    from urllib.parse import quote
    # timeout: 防止 Bing 卡住时把整个 Hermes 进程一起拖死（primp 原生代码持有 GIL）
    # quote: 把 query 做 URL 编码——中文没事，但 C++ 的 + 会被当空格、R&B 的 & 会被当参数分隔符，必须编码
    client = primp.Client(impersonate="chrome", timeout=20)
    resp = client.get("https://cn.bing.com/search?q="+quote(queue)+"&format=rss")
    rawRSS=resp.text
    return(rawRSS)

def crawlUrlRawHtml(url: str):
    """
    以某种聪明的方式爬任何URL
    url: 你的心所向往的地方，人话说就是网址。
    """
    import primp
    from urllib.parse import quote
    # 只编码 URL 里的非安全字符（中文路径等），保留 :/?#&= 等结构字符，不能整体 quote
    client = primp.Client(impersonate="chrome", timeout=30)
    resp = client.get(quote(url, safe=":/?#&=@%+~!$'()*,"))
    rawHtml=resp.text
    return(rawHtml)

def parseBingRSS(rawRSS: str):
    """
    用 Python 标准库 xml.etree.ElementTree 把 Bing RSS 解析成结构化结果列表
    rawRSS: crawlBing2RawRSS 返回的 RSS 文本字符串。
    返回: list[dict]，每项 {"title": 标题, "url": 链接, "description": 摘要}
    """
    import xml.etree.ElementTree as ET
    from html import unescape
    root = ET.fromstring(rawRSS)
    results = []
    for item in root.iter():
        if item.tag.split("}")[-1] != "item":
            continue
        d = {}
        for ch in item:
            d[ch.tag.split("}")[-1]] = (ch.text or "").strip()
        if not d.get("title") or not d.get("link"):
            continue
        results.append({
            "title": unescape(d["title"]),
            "url": d["link"],
            "description": unescape(d.get("description", "")),
        })
    return(results)

# ============================================================
# 反蜜罐三档阶梯（2026-08-31 新增）
# 实测确诊：cn.bing RSS 对 ≥4 词的查询确定性返回反爬垃圾
# （<channel> 回显查询但 <item> 是无关填充内容），≤3 词稳定。
# 垃圾还有第二副面孔：含查询词的全是泛泛官方热门页，检测器抓不到，
# 所以主防线是截断，检测器只做辅助闸门。
# ============================================================

# 轮换指纹池：三个真浏览器指纹均实测连通（2026-08-31），
# 轮换着发可以少被单一 TLS 指纹盯上限速
_fingerprintPool=["chrome", "edge", "safari"]
_fingerprintIdx=0

def pickNextFingerprint():
    """轮流取下一个 TLS 指纹（模块级计数器轮转）"""
    global _fingerprintIdx
    imp=_fingerprintPool[_fingerprintIdx % len(_fingerprintPool)]
    _fingerprintIdx=_fingerprintIdx + 1
    return(imp)

def crawlBing2RawRSSWithFingerprint(queue: str):
    """
    和 crawlBing2RawRSS 一样爬 Bing RSS，但 TLS 指纹走轮换池。
    原函数保留不动，这个是反爬增强版。
    """
    import primp
    from urllib.parse import quote
    client = primp.Client(impersonate=pickNextFingerprint(), timeout=20)
    resp = client.get("https://cn.bing.com/search?q="+quote(queue)+"&format=rss")
    rawRSS=resp.text
    return(rawRSS)

def scoreTokenInfo(tok: str):
    """
    给一个词的信息量打分：
      字母数字混合词（KB5120998 这种唯一标识符）最值钱；
      实义词保底分；纯数字只是修饰给低分；停用词负分滚粗。
    """
    t=tok.strip('"').lower()
    stopwords={"for","of","the","a","an","and","or","to","in","on","at","after","with","is","are","how","why","what","about","site","from","by","into","not"}
    if(t in stopwords):
        return(-100)
    hasAlpha=any(c.isalpha() for c in t)
    hasDigit=any(c.isdigit() for c in t)
    if(hasAlpha and hasDigit):
        return(120 + min(len(t), 20))
    if(hasDigit and not hasAlpha):
        return(min(len(t), 20))
    return(30 + min(len(t), 20))

def shortenQueue(queue: str, maxTokens: int = 3):
    """
    查询词超过 3 个时，按信息量分数挑最值钱的 maxTokens 个词，
    再按原来的先后顺序拼回去（Bing RSS 的降级线就在 3→4 词之间）。
    带引号的短语整体当一个词保护。
    """
    import re
    rawTokens=re.findall(r'"[^"]+"|\S+', queue)
    if(len(rawTokens) <= maxTokens):
        return(queue)
    scored=sorted(enumerate(rawTokens), key=lambda p: -scoreTokenInfo(p[1]))
    picked=sorted(scored[:maxTokens], key=lambda p: p[0])
    return(" ".join(t for _, t in picked))

def rarestSingleToken(queue: str):
    """从查询里挑信息量最高的那一个词（阶梯的最后一档兜底）"""
    import re
    rawTokens=re.findall(r'"[^"]+"|\S+', queue)
    if(not rawTokens):
        return(queue)
    return(max(rawTokens, key=lambda t: scoreTokenInfo(t)))

def rssLooksLikeGarbage(parsedRss, queue: str):
    """
    分级蜜罐检测器（辅助闸门，不是主防线）：
      查询里有字母数字混合标识符 → 至少一条结果必须沾到其中一个；
      没有标识符 → 至少一条结果沾到任意实义词。
      都不满足 → 判定为反爬降级垃圾。
    已知盲区：Bing 有时返回"含查询词但全是泛泛热门页"的垃圾，
    这种检测器抓不到——所以长查询照样要先走截断。
    返回 True=疑似垃圾，False=看起来是真结果。
    """
    import re
    if(not parsedRss):
        return(True)
    stopwords={"for","of","the","a","an","and","or","to","in","on","at","after","with","is","are","how","why","what","about","site","from","by","into","not"}
    tokens=[t.strip('"') for t in re.findall(r'"[^"]+"|\S+', queue)]
    salient=[t.lower() for t in tokens if len(t) >= 2 and not t.isdigit() and t.lower() not in stopwords]
    if(not salient):
        return(False)  # 没词可查时宁可放行，不瞎判
    identifiers=[t for t in salient if any(c.isalpha() for c in t) and any(c.isdigit() for c in t)]
    watchlist=identifiers if identifiers else salient
    for res in parsedRss:
        blob=(res.get("title","") + " " + res.get("description","")).lower()
        if(any(w in blob for w in watchlist)):
            return(False)
    return(True)

def ladderSearch(queue: str):
    """
    反蜜罐三档阶梯：原句发 → 截到3词发 → 只发最值钱的一个词。
    每一档都用 rssLooksLikeGarbage 验货，判垃圾就降一档；
    全部判垃圾时返回最后一档的结果（聊胜于无，跟原行为一致）。
    返回 parseBingRSS 同款 list[dict]。
    """
    ladder=[queue]
    short=shortenQueue(queue)
    if(short != queue):
        ladder.append(short)
    rare=rarestSingleToken(queue)
    if(rare not in ladder):
        ladder.append(rare)
    lastParsed=[]
    for i in range(len(ladder)):
        use=ladder[i]
        try:
            rawRSS=crawlBing2RawRSSWithFingerprint(queue=use)
            parsed=parseBingRSS(rawRSS=rawRSS)
        except Exception:
            if(i == len(ladder) - 1):
                raise  # 最后一档还炸：抛出去让 search() 按原逻辑报错
            continue
        lastParsed=parsed
        if(not rssLooksLikeGarbage(parsedRss=parsed, queue=use)):
            return(parsed)
    return(lastParsed)

class BingCrawlerWebSearchProvider(WebSearchProvider):
    """Simple search provider against the Bing RSS feed."""

    @property
    def name(self) -> str:
        # Stable id used in web.search_backend / web.extract_backend / web.backend
        # config keys. Lowercase, no spaces; hyphens permitted.
        return "bing-search"

    @property
    def display_name(self) -> str:
        # Human label shown in `hermes tools`. Defaults to `name`.
        return "Bing Search (Direct Crawl RSS feed)"

    def is_available(self) -> bool:
        # Cheap check — env var present, optional dep importable, etc.
        # MUST NOT make network calls (runs on every `hermes tools` paint).
        try:
            import primp
            import xml.etree.ElementTree as ET
            from html import unescape
            return True
        except:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        response=[]
        idx=1
        
        try:
            # 反蜜罐升级：走三档阶梯（原句→3词→单词），替代原来的直发直解
            # （原代码: rss = crawlBing2RawRSS(queue=query); parsedRss=parseBingRSS(rawRSS=rss)）
            parsedRss=ladderSearch(queue=query)
            for res in parsedRss[:max(1, int(limit))]:
                response.append({
                    "title": res.get("title","No Title Provided"), 
                    "url": res.get("url", ""),
                    "description": res.get("description", "No Description Provided"),
                    "position": idx
                    })
                idx = idx + 1
        except Exception as e:
            return {"success": False, "error": str(e)}

        # Response shape is fixed — see "Response shape" below.
        # return {
        #     "success": True,
        #     "data": {
        #         "web": [
        #             {
        #                 "title": item.get("title", ""),
        #                 "url": item.get("url", ""),
        #                 "description": item.get("snippet", ""),
        #                 "position": idx + 1,
        #             }
        #             for idx, item in enumerate(data.get("results", []))
        #         ],
        #     },
        # }
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
            dat={"url": url, "title": "Too lazy to parse out, see below.", "content": "Too lazy to parse out, see below.", "raw_content": ""} # "我懒得解析出来了，直接看下面吧"
            try:
                dat["raw_content"]=crawlUrlRawHtml(url)
            except Exception as e:
                # 单个 URL 失败只标记该项 error，其他 URL 照常返回（不整批失败）
                dat["error"]=str(e)
            respDat.append(dat)

        return respDat
        


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    ctx.register_web_search_provider(BingCrawlerWebSearchProvider())