from __future__ import annotations

import os
import threading
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

# ============================================================
# v1.3 升级新增（2026-11-12）——只加函数，v1.0 原函数全部保留不接线
# 三目标实测裁定（PROMPT.md v0.2）：
#   目标1 primp 主通道，钉死桌面版本指纹（2.0.1 裸别名 chrome 随机掉 iPhone 移动版）
#   目标2 伪装头防御性保留；首页预热默认关（触发富变体，h2 锚点全灭+体积3-4倍）
#   目标3 多级容错解析器（b_algo 切块 + h2>a / a.tilk 多级锚点，跨变体稳）
#   D9 子进程隔离根治 primp GIL 冻结（官方 ddgs 同款模板）
# ============================================================

# 桌面版本指纹池（2026-11-12 实测：chrome_152/chrome_146/safari 均桌面 10 条；
# 裸别名 chrome/edge 在 primp 2.0.1 会随机解析到 iPhone CriOS → 移动页 5 条无 h2，禁用）
_desktopFingerprintPool=["chrome_152", "chrome_146", "safari"]
_desktopFingerprintIdx=0

# 子进程硬超时参数（官方 ddgs 模板同款）
_searchTimeoutSecs=45
_pollIntervalSecs=0.1
_terminateGraceSecs=1.0
_lastWorkerProc=None
_testHook=None  # 仅测试用：sleep/gil/empty，需 HERMES_BING_ALLOW_TEST_HOOKS=1 才生效

# register 时捕获的 ctx（settings 读取用）；provider 方法拿不到 ctx，只能模块级存
_pluginCtx=None

# 设置默认值（与 plugin.yaml config_schema 一致）：
# 行为保持类默认 true（v1.3 行为即默认），新行为/修改类默认 false（主人 11-09 DIY 规范）
_settingsDefaults={
    "stealth_headers": True,    # 伪装头（MUID/Accept-Language/Sec-Fetch），防御性，不提质量
    "warm_homepage": False,     # 首页预热——实测有害（富变体），默认关
    "subprocess_isolation": True,  # 子进程隔离（D9），关掉=退回 v1.0 式进程内直跑（有 GIL 冻结风险）
    "result_cache": False,      # 短 TTL 同查询缓存（U7 新行为，默认关）
    "cache_ttl_secs": 300,
    "search_timeout_secs": 45,
    "honeypot_truncation_fallback": True,  # 蜜罐截词回退（v1.3.1 默认开）：判垃圾降档截3词→单词→中文前3字。
    # 三轮实测：关=12/70 相关，开=53/78；代价仅垃圾查询多几秒。显式存 false 可关（不截词原教旨）。
}

# v1.4 隐式设置（故意不进 _settingsDefaults：不进面板、不由 seedMissingSettings 写进主人 config，
# 手写 config.yaml 的插件 settings 里加同名键即生效；缺键时读侧走各自默认）：
#   extract_timeout_secs = 30   extract 每 URL 子进程墙钟上限（N2）
#   allow_empty          = false 零命中是否允许静默返回空表（F17；默认关=报错）

def getSetting(key: str):
    """读 plugins.entries.<id>.settings.<key>；读不到键按默认值（主人 11-09 规范）。
    显式存的 ""/false/0 是有效值，不会被默认覆盖（get_config 命中即返回）。"""
    default=_settingsDefaults.get(key)
    if(_pluginCtx is None):
        return(default)
    try:
        val=_pluginCtx.get_config(key, default)
    except Exception:
        return(default)
    if(val is None):
        return(default)
    return(val)

def seedMissingSettings(ctx):
    """逐键自动补齐：只有 key 不在 settings 里才写默认值（覆盖旧 seed-once）。
    用户显式存的 ""/false/0 不覆盖；写入走官方 writer ctx.set_config。"""
    for key in _settingsDefaults:
        try:
            existing=ctx.get_config(key, None)
        except Exception:
            existing=None
        if(existing is None):
            try:
                ctx.set_config(key, _settingsDefaults[key])
            except Exception:
                pass  # 写不进不拦加载，读侧有默认兜底

def readIntSetting(key: str, default: int, minimum: int = 1):
    """显式整数配置读取（N3 修复）：值缺失(None)才算没配，显式 0 是有效值。
    原先 `int(getSetting(k) or default)` 会把显式配置的 0 吞成默认——0 秒超时 = 死循环，故钳到 minimum。
    非数字垃圾值退回 default（面板手改 settings 可能塞进空串）。"""
    raw=getSetting(key)
    if(raw is None):
        return(default)
    try:
        val=int(raw)
    except (TypeError, ValueError):
        return(default)
    if(val < minimum):
        return(minimum)
    return(val)

def pickNextDesktopFingerprint():
    """轮流取下一个桌面版本 TLS 指纹（模块级计数器轮转，同 v1.0 怪癖）"""
    global _desktopFingerprintIdx
    imp=_desktopFingerprintPool[_desktopFingerprintIdx % len(_desktopFingerprintPool)]
    _desktopFingerprintIdx=_desktopFingerprintIdx + 1
    return(imp)

def makeFakeMUID():
    """伪造一个 MUID cookie（32 位大写 hex，EnhancedBing 同款手法）"""
    import secrets
    return(secrets.token_hex(16).upper())

def makeStealthHeaders():
    """伪装头（目标2 B 臂）：随机 Accept-Language 三变体 + Sec-Fetch 全套 + DNT。
    故意不设 UA/Sec-Ch-Ua——避免与 impersonate 的 TLS 指纹自相矛盾（v1.1 教训）。
    实测：无质量增益，价值是降封禁/限速概率，防御性保留。"""
    import random
    langs=["zh-CN,zh;q=0.9,en;q=0.8", "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "zh-CN,zh;q=0.9"]
    headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(langs),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    return(headers)

def crawlBing2RawHtmlSearch(queue: str, fingerprint: str = None, stealth: bool = True, warm: bool = False):
    """
    纯 HTML 通道爬 Bing（v1.2 架构还原 + v1.3 修正）。
    不带 format=rss——RSS 蜜罐静默降级已弃（v1.2 裁定）。
    锁中文 setlang=zh-CN&ensearch=0；不截词，原句完整直发。
    warm 默认 False：首页预热实测触发富变体（h2 全灭+体积3-4倍），仅硬封禁换画像时才开。
    """
    import primp
    from urllib.parse import quote
    if(fingerprint is None):
        fingerprint=pickNextDesktopFingerprint()
    client=primp.Client(impersonate=fingerprint, timeout=25)
    headers={}
    if(stealth):
        headers=makeStealthHeaders()
        headers["Cookie"]="MUID="+makeFakeMUID()+"; SRCHHPGUSR=SRCHLANG=zh-Hans&BRW=NOTP&BRH=M&CW=800&CH=600&SCW=800&SCH=600&DPR=1.0&UTC=480&DM=0"
    if(warm):
        # 预热拿会话 cookie（实测收 8 个）——代价是富变体，默认不走
        try:
            client.get("https://cn.bing.com/", headers=headers)
            headers["Referer"]="https://cn.bing.com/"
        except Exception:
            pass
    resp=client.get("https://cn.bing.com/search?q="+quote(queue)+"&setlang=zh-CN&ensearch=0", headers=headers)
    rawHtml=resp.text
    return(rawHtml)

def crawlUrlRawHtmlStealth(url: str):
    """伪装版抓任意 URL（extract 用）：桌面指纹 + 伪装头 + Bing Referer。
    F18：primp 默认会自己跟随重定向——实测确认"检查最终 URL"发生在请求已经
    打到重定向目标之后，等于先访问再拒。改成手动逐跳：每跳先过闸门再发下一跳。"""
    import primp
    from urllib.parse import quote, urljoin
    client=primp.Client(impersonate=pickNextDesktopFingerprint(), timeout=30, follow_redirects=False)
    headers=makeStealthHeaders()
    headers["Referer"]="https://cn.bing.com/"
    headers["Cookie"]="MUID="+makeFakeMUID()
    current=url
    for _hop in range(6):
        reason=checkExtractUrlAllowed(current)
        if(reason is not None):
            raise RuntimeError("blocked url: "+reason)
        resp=client.get(quote(current, safe=":/?#&=@%+~!$'()*,"), headers=headers)
        if resp.status_code in (301, 302, 303, 307, 308):
            location=resp.headers.get("location") or ""
            if(not location):
                raise RuntimeError("redirect without a Location header")
            current=urljoin(current, location)
            continue
        return(resp.text)
    raise RuntimeError("too many redirects (possible redirect loop)")

def isPrivateFetchHost(host: str):
    """内网/环回/保留段判定（F18 SSRF 探测面）。
    数字 host 必须是标准、完整 IP；无法由 ipaddress 解析的数字形态一律拒绝。
    域名会检查 getaddrinfo 的所有结果；解析失败不等于私网，避免误杀所有公网域名。
    返回 True=不许抓。"""
    import ipaddress
    import re
    import socket
    h=(host or "").strip().lower().rstrip(".")
    if(h.startswith("[") and h.endswith("]")):
        h=h[1:-1]
    h=h.split("%")[0]
    if(not h):
        return(True)
    if(h == "localhost" or h.endswith(".localhost")):
        return(True)
    is_numeric=bool(re.fullmatch(r"[0-9.]+", h)) or bool(re.fullmatch(r"0[xX][0-9a-fA-F]+", h))
    if(is_numeric):
        try:
            ip=ipaddress.ip_address(h)
        except ValueError:
            return(True)
    else:
        try:
            ip=ipaddress.ip_address(h)
        except ValueError:
            try:
                infos=socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
            except socket.gaierror:
                return(False)
            resolved=set()
            for item in infos:
                addr=(item[4][0] if item and len(item) > 4 and item[4] else "")
                if(addr):
                    resolved.add(str(addr).split("%", 1)[0])
            if(not resolved):
                return(False)
            return(any(isPrivateFetchHost(str(addr)) for addr in resolved))
    return(
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )

def checkExtractUrlAllowed(url: str):
    """extract 入口 URL 校验（F18）：scheme 先行，只放行 http/https。
    BING_ALLOW_PRIVATE_FETCH=1 只豁免私网/解析检查，绝不豁免 scheme 或 host 语法。"""
    from urllib.parse import urlsplit
    raw=str(url or "").strip()
    if(not raw):
        return("empty url")
    try:
        parts=urlsplit(raw)
    except Exception as e:
        return("invalid url: %s" % e)
    scheme=(parts.scheme or "").lower()
    if(scheme not in ("http", "https")):
        return("blocked scheme %r — extract only allows http/https" % (parts.scheme or ""))
    host=parts.hostname or ""
    if(not host):
        return("blocked url: no host in %r" % raw)
    if(os.environ.get("BING_ALLOW_PRIVATE_FETCH") != "1" and isPrivateFetchHost(host)):
        return("blocked private/loopback host %r (SSRF guard; set BING_ALLOW_PRIVATE_FETCH=1 to allow)" % host)
    return(None)

def rssLooksBlocked(rawHtml: str):
    """硬封禁判读（沿用 v1.2 命名）：captcha/verify 同现才判验证码页。
    challenge 字样会出现在 JS 资源名里（PoWChallengeSolver.br.js），单独不算数。"""
    low=rawHtml.lower()
    if("captcha" in low and "verify" in low):
        return(True)
    if("b_bingcaptcha" in low or "captcha-container" in low):
        return(True)
    return(False)

def htmlLooksDegraded(rawHtml: str):
    """频率墙软降级结构探测：HTTP 200 + b_results 容器在 + b_algo≈0 = 阉割页。
    封禁关键词表抓不到这种，必须看结构。b_no 是「真没结果」页，不算降级。"""
    if('class="b_algo"' in rawHtml):
        return(False)
    if("b_results" in rawHtml and 'class="b_no"' not in rawHtml):
        return(True)
    return(False)

def stripTags(s: str):
    """剥 HTML 标签 + unescape + 压缩空白"""
    import re
    from html import unescape
    return(re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", s))).strip())

def cleanBingBreadcrumb(title: str):
    """洗移动版标题混入的面包屑前缀：microsoft.comhttps://www... › path 这种。
    桌面标题不受影响（不含 ://）。"""
    import re
    t=title
    # 域名+URL›路径 前缀整体剥掉（贪婪吃到最后一个 › 或 URL 结束）
    m=re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}https?://\S*?(?:\s*›\s*[^›]*)?\s*(.*)$", t)
    if(m and m.group(1)):
        t=m.group(1)
    else:
        # 兜底：标题里嵌着 http 的，从 http 前最近的空格切；
        # 保护：http 开头且前缀≤4字符的可能是合法标题（如 "https:// 是什么"），不切
        idx=t.find("http")
        if(idx > 4):
            t=t[:idx].strip()
    return(t.strip())

def cleanTitleSiteSuffix(title: str):
    """标题站名后缀清洗（继承 v1.2 正则）：分隔符命中且后缀 ≤16 字符才砍，
    真标题的冒号/破折号原样保留。"""
    import re
    m=re.match(r"^(.*?)\s*(?:_|—|–|·|\||-)\s*([^_|—–·\-]{1,16})$", title)
    if(m and m.group(1)):
        return(m.group(1).strip())
    return(title)

def splitAlgoBlocks(rawHtml: str):
    """按 <li class="b_algo" 切块（别按 <li> 切——块内嵌套 li+大量 link CSS，一切就碎）。
    每块截到下一个 <li class= 或 </ol>。"""
    import re
    parts=re.split(r'<li class="b_algo"', rawHtml)
    blocks=[]
    for p in parts[1:]:
        m=re.search(r'<li class="|</ol>', p)
        blocks.append(p[:m.start()] if m else p)
    return(blocks)

def extractTitleUrlFromBlock(block: str):
    """块内多级标题锚点（目标3 容错链）：
      桌面 <h2><a href> → 移动 <a href><h2>（b_algoheader，方向相反）→ a.tilk → 首个外链兜底。
    全页 h2 不可靠（混相关搜索/图片导航，计数>结果数）。
    注意：移动版 a.tilk 是面包屑域名锚点（aria-label=域名，文本=域名›路径），不是标题，
    所以移动方向 a>h2 必须排在 tilk 前面；tilk 只作桌面变体的兜底。
    滤 bing.com/translator/go.microsoft 站内链。返回 (title, url) 或 (None, None)。"""
    import re
    pats=[
        r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        r'<a[^>]*href="([^"]+)"[^>]*>\s*<h2[^>]*>(.*?)</h2>',
        r'<a[^>]*class="[^"]*tilk[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*tilk[^"]*"[^>]*>(.*?)</a>',
        r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    ]
    for pat in pats:
        m=re.search(pat, block, re.S)
        if(m):
            url=m.group(1)
            title=stripTags(m.group(2))
            if(url.startswith("http") and "bing.com" not in url and "go.microsoft" not in url and title):
                title=cleanBingBreadcrumb(title)
                title=cleanTitleSiteSuffix(title)
                return(title, url)
    m=re.search(r'<a[^>]*href="(http[^"]+)"', block)
    if(m and "bing.com" not in m.group(1) and "go.microsoft" not in m.group(1)):
        return("(no-title)", m.group(1))
    return(None, None)

def extractSnippetFromBlock(block: str):
    """块内摘要多级兜底：b_lineclamp → b_caption>p → 首个 <p>"""
    import re
    pats=[
        r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
        r'<div[^>]*class="[^"]*b_caption[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>',
        r'<p[^>]*>(.*?)</p>',
    ]
    for pat in pats:
        m=re.search(pat, block, re.S)
        if(m):
            s=stripTags(m.group(1))
            if(s):
                return(s)
    return("")

def parseBingHtml(rawHtml: str):
    """
    多级容错解析器（目标3，跨经典/富/移动变体验证通过）：
    b_algo 切块 → 块内多级锚点 → 摘要兜底 → 洗面包屑/站名后缀。
    返回 list[dict] {"title","url","description"}，与 parseBingRSS 同形状。
    软降级阉割页（b_algo≈0）自然返回空表=显式失败不掺垃圾。
    """
    results=[]
    for block in splitAlgoBlocks(rawHtml):
        title, url=extractTitleUrlFromBlock(block)
        if(url):
            results.append({
                "title": title,
                "url": url,
                "description": extractSnippetFromBlock(block),
            })
    return(results)

def backoffDelay(attempt: int):
    """失败退避（继承 v1.2 实测参数）：第1次 2-5s，第2次 4-8s"""
    import random
    import time
    if(attempt <= 1):
        time.sleep(random.uniform(2, 5))
    else:
        time.sleep(random.uniform(4, 8))

def splitMixedScriptTokens(tokens):
    """v1.3.1：中文没有空格分词，「英伟达RTX5090显卡价格」整串会被当一个 token，
    截词/挑词全失效（probe12 实锤：5090/酸面团 单发 10/10 相关，但整串查询救不回来）。
    把 CJK↔拉丁/数字 边界切开；纯中文串保持整块（无词典不瞎切，整词直发实测有效）。
    引号短语整体保护，但粘在引号尾部的中文照样切。全角标点（，。：等）当分隔符剥掉。
    最后把「品牌+型号」（RTX + 5090）合并成 RTX5090——纯数字单独打分太低会被泛词碾压。"""
    import re
    cjk=r'㐀-䶿一-鿿'
    def _pieces(s):
        got=[]
        for p in re.findall(r'['+cjk+r']+|[^'+cjk+r']+', s):
            for pc in re.split(r'[　-〿＀-￯]+', p):
                pc=pc.strip()
                if(pc):
                    got.append(pc)
        return(got)
    out=[]
    for tok in tokens:
        m=re.match(r'^("[^"]+")(.*)$', tok)
        if(m):
            out.append(m.group(1))
            if(m.group(2)):
                out.extend(_pieces(m.group(2)))
            continue
        out.extend(_pieces(tok))
    # 品牌+型号合并：2-6 字母 + 3-5 数字 → RTX5090 / GTX1080 这种唯一标识符
    merged=[]
    i=0
    while(i < len(out)):
        t=out[i]
        if(i+1 < len(out) and re.fullmatch(r'[A-Za-z]{2,6}', t) and re.fullmatch(r'\d{3,5}', out[i+1])):
            merged.append(t + out[i+1])
            i=i + 2
        else:
            merged.append(t)
            i=i + 1
    return(merged)

def htmlTokenScore(tok: str):
    """v1.3.1 HTML 通道选词分（不改 v1.0 scoreTokenInfo，RSS 老通道原字节保留）。
    标识符（字母+数字，如 KB5054156/0x800f0922/RTX5090）最值钱；纯数字低分；
    普通实义词用逆长度——越短越可能是稀有主题词（primp>python，probe12 实锤）。
    已知局限：同长词（dataview/obsidian）离线分不出，交阶梯多发候选让 Bing 选。"""
    t=tok.strip('"')
    hasAlpha=any(c.isalpha() for c in t)
    hasDigit=any(c.isdigit() for c in t)
    if(t.lower() in {"for","of","the","a","an","and","or","to","in","on","at","after","with","is","are","how","why","what","about","site","from","by","into","not"}):
        return(-100)
    if(hasAlpha and hasDigit):
        return(120 + min(len(t), 20))
    if(hasDigit and not hasAlpha):
        return(min(len(t), 20))
    return(100 - max(0, len(t) - 3) * 6)

def ladderCandidates(queue: str):
    """v1.3.1：HTML 通道回退阶梯候选（原句→截3词→最值钱单词），CJK 边界分词。
    不改 v1.0 的 shortenQueue/rarestSingleToken（RSS 老通道原字节保留）。"""
    import re
    rawTokens=re.findall(r'"[^"]+"|\S+', queue)
    tokens=splitMixedScriptTokens(rawTokens)
    cands=[queue]
    if(len(tokens) > 3):
        scored=sorted(enumerate(tokens), key=lambda p: -htmlTokenScore(p[1]))
        picked=sorted(scored[:3], key=lambda p: p[0])
        short=" ".join(t for _, t in picked)
        if(short not in cands):
            cands.append(short)
    if(tokens):
        rare=max(tokens, key=htmlTokenScore)
        if(rare not in cands):
            cands.append(rare)
    # v1.3.1：纯中文长句无空格/拉丁边界可拆（酸面团酵头怎么培养 整块一个 token），
    # 取前3字作兜底候选——probe12 实锤「酸面团」单发 10/10 相关。候选总数封顶4控制延迟。
    import re as _re
    for t in tokens:
        if(len(t) >= 5 and _re.fullmatch(r'[㐀-䶿一-鿿]+', t)):
            head=t[:3]
            if(head not in cands):
                cands.append(head)
    return(cands[:4])

def htmlLooksLikeGarbage(parsed, queue: str):
    """v1.3.1 HTML 通道蜜罐判定——只做高置信度判断，不堆规则过拟合（12 样本实测裁定）：
      ① 查询含字母数字标识符（0x800f0922/KB5054156/RTX5090）但结果里全灭 → 垃圾；
      ② 覆盖率 < 1/3（几乎没有任何实义词命中）→ 全 homepage 垃圾。
    已知盲区（诚实声明）：无标识符查询里「泛词命中、核心词全灭」的垃圾（python.org 带 python
    但 primp 全灭）抓不到——因为「泛但真」的教程合集（obsidian 教程）长得一样，硬杀会误杀真结果。
    这种精度问题交截词阶梯多发候选词解决，不在判定器里硬判。
    无词可查（纯停用词）宁可放行。返回 True=疑似垃圾。"""
    import re
    if(not parsed):
        return(True)
    stopwords={"for","of","the","a","an","and","or","to","in","on","at","after","with","is","are","how","why","what","about","site","from","by","into","not"}
    rawTokens=re.findall(r'"[^"]+"|\S+', queue)
    tokens=splitMixedScriptTokens(rawTokens)
    salient=[t.strip('"').lower() for t in tokens if len(t.strip('"')) >= 2 and not t.strip('"').isdigit() and t.strip('"').lower() not in stopwords]
    if(not salient):
        return(False)
    identifiers=[t for t in salient if any(c.isalpha() for c in t) and any(c.isdigit() for c in t)]
    blobs=[(res.get("title","") + " " + res.get("description","")).lower() for res in parsed]
    # 标识符匹配压掉空格：品牌合并造出 RTX5090，但真结果标题是 "RTX 5090"（带空格）
    compactBlobs=[re.sub(r'\s+', '', b) for b in blobs]
    if(identifiers and not any(any(w in cb for cb in compactBlobs) for w in identifiers)):
        return(True)
    coverage=sum(1 for cb in compactBlobs if any(w in cb for w in salient))
    if(coverage * 3 < len(parsed)):
        return(True)
    return(False)

# 最近一次搜索的诊断（F17）：零命中时随 error 一起回给调用方，别让"网上没有"变成静默结论
_lastSearchDiag={}

def _recordSearchDiag(tiers: int, query: str, htmlLen: int = 0):
    """记一次搜索的尝试档数/最终查询/最终 URL/最终页面体积（模块级，非线程局部；
    并发搜索只会互相覆盖诊断字段，不影响结果本身）。"""
    global _lastSearchDiag
    from urllib.parse import quote
    _lastSearchDiag={
        "tiers_tried": tiers,
        "final_query": query or "",
        "final_url": "https://cn.bing.com/search?q="+quote(query or "")+"&setlang=zh-CN&ensearch=0",
        "final_html_bytes": htmlLen,
    }

def ladderSearchHtml(queue: str):
    """
    不截词阶梯（v1.2 架构 + v1.3 修正）：原句完整直发，失败退避重试换桌面指纹。
    硬封禁（验证码）直接抛错——IP 级，重试救不了；
    软降级（频率墙）退避 2-5s/4-8s + 换指纹 + 新 MUID，3 败返空表=显式失败。
    v1.3 新增：蜜罐第二副面孔（含查询词但全是泛泛热门页）用 rssLooksLikeGarbage 验货；
    honeypot_truncation_fallback 开启时判垃圾降档截3词→单词（复用 v1.0 截词函数）。
    """
    stealth=bool(getSetting("stealth_headers"))
    warm=bool(getSetting("warm_homepage"))
    truncFallback=bool(getSetting("honeypot_truncation_fallback"))
    # 候选查询：默认只原句；开回退则追加截3词、单词两档（v1.3.1：CJK 边界分词，去重保序）
    candidates=[queue]
    if(truncFallback):
        for cand in ladderCandidates(queue):
            if(cand not in candidates):
                candidates.append(cand)
    lastErr=None
    lastParsed=[]
    tiersTried=0
    lastHtmlLen=0
    for ci in range(len(candidates)):
        use=candidates[ci]
        tiersTried=ci + 1
        for attempt in range(3):
            fp=pickNextDesktopFingerprint()
            try:
                rawHtml=crawlBing2RawHtmlSearch(use, fingerprint=fp, stealth=stealth, warm=warm)
            except Exception as e:
                lastErr=e
                if(attempt < 2):
                    backoffDelay(attempt + 1)
                continue
            lastHtmlLen=len(rawHtml or "")
            if(rssLooksBlocked(rawHtml)):
                raise RuntimeError("Bing hard block (captcha) — IP-level, retry cannot fix; rest a few minutes or change exit IP")
            if(htmlLooksDegraded(rawHtml)):
                # 频率墙软降级：退避+换指纹+新MUID（MUID 每枪新建 client 时自动换）
                if(attempt < 2):
                    backoffDelay(attempt + 1)
                continue
            parsed=parseBingHtml(rawHtml)
            if(parsed):
                # 蜜罐验货（v1.3.1 新判定器：锚词+覆盖率双闸）：判垃圾且还有下一档候选 → 降档；否则收下
                if(truncFallback and htmlLooksLikeGarbage(parsed, use) and ci < len(candidates) - 1):
                    lastParsed=parsed
                    break
                return(parsed)
            # 解析空但结构正常：可能是真没结果（b_no），也可能是新变体——再试一档
            if(attempt < 2):
                backoffDelay(attempt + 1)
    # F17：零命中/只剩渣时把诊断留给调用方（父进程读的是子进程信封里的 debug 字段）
    _recordSearchDiag(tiers=tiersTried, query=candidates[-1], htmlLen=lastHtmlLen)
    if(lastParsed):
        return(lastParsed)
    if(lastErr is not None):
        raise lastErr
    return([])

# ---- 结果缓存（U7，默认关）----
_resultCache={}
# N4：缓存是跨线程共享（search 可能被并发调用），get/put/清最旧必须同锁，
# 否则 min() 迭代 dict 时另一线程写入会 RuntimeError: dictionary changed size during iteration
_resultCacheLock=threading.Lock()

def cacheGet(queue: str):
    import time
    ttl=readIntSetting("cache_ttl_secs", 300)
    with _resultCacheLock:
        hit=_resultCache.get(queue)
    if(hit and (time.time() - hit[0]) < ttl):
        return(hit[1])
    return(None)

def cachePut(queue: str, parsed):
    import time
    with _resultCacheLock:
        _resultCache[queue]=(time.time(), parsed)
        # 容量闸：超限就反复清最旧直到 <=128（不是只清一条——并发批量写会一次冲高）
        while(len(_resultCache) > 128):
            oldest=min(_resultCache, key=lambda k: _resultCache[k][0])
            del _resultCache[oldest]

# ---- 子进程隔离（D9，官方 ddgs 模板）----

def _searchInterrupted():
    """tools.interrupt 不在插件公开白名单（private-import 警告项），try 守卫降级"""
    try:
        from tools.interrupt import is_interrupted
        return(is_interrupted())
    except Exception:
        return(False)

def _terminateAndReap(proc):
    """terminate → kill → wait，不留孤儿（官方 ddgs 同款）"""
    import time
    if(proc is None):
        return
    def _waitUntilDead():
        deadline=time.monotonic() + _terminateGraceSecs
        while(proc.poll() is None and time.monotonic() < deadline):
            time.sleep(0.05)
        return(proc.poll() is not None)
    try:
        for escalate in (proc.terminate, proc.kill):
            if(proc.poll() is None):
                escalate()
                _waitUntilDead()
    except Exception:
        pass

def _workerEnvironment():
    """构造最小 worker 环境：只传运行所需的路径、代理、编码与临时目录。"""
    import os
    keep_exact=(
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
        "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "PYTHONIOENCODING",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMP", "TEMP", "TMPDIR",
        # v1.4 验收闸旗标（非密钥）：子进程 main() 与父进程要按同一开关判 test_hook
        "HERMES_BING_ALLOW_TEST_HOOKS",
        # SSRF 闸的纯开关（非密钥）：子进程复查最终 URL 时要与父进程同一豁免口径
        "BING_ALLOW_PRIVATE_FETCH",
    )
    env={key: value for key, value in os.environ.items() if key in keep_exact}
    return(env)

def _spawnSearchWorker(env: dict):
    """起 _search_worker.py 子进程。Windows CREATE_NEW_PROCESS_GROUP / Unix start_new_session。
    stderr=DEVNULL：话痨子进程会把只读 stdout 的 drain 堵死（官方注释）。"""
    import os
    import subprocess
    import sys
    # pm store PATH：worker 跑在 STORE python 下，第三方 import 靠 launcher 组合的 PYTHONPATH；
    # 把环境 PYTHONPATH 合并回来（官方 ddgs 同款处理）
    ambientPP=os.environ.get("PYTHONPATH")
    if(ambientPP):
        current=env.get("PYTHONPATH", "")
        env["PYTHONPATH"]=ambientPP + os.pathsep + current if current else ambientPP
    workerPath=os.path.join(os.path.dirname(os.path.abspath(__file__)), "_search_worker.py")
    extraKwargs={"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    return(subprocess.Popen(
        [sys.executable, workerPath], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True, encoding="utf-8", errors="replace", **extraKwargs,
    ))

def _snapshotSettings():
    """把当前开关快照出来（子进程没有活 ctx，靠 _SettingsShim 读）"""
    snapshot={}
    for key in _settingsDefaults:
        snapshot[key]=getSetting(key)
    return(snapshot)

def _workerEnvelopeDict(raw: str, proc, label: str = "search"):
    """把 worker stdout 解成信封 dict，形状不对一律 RuntimeError（search/extract 共用）"""
    import json
    raw=(raw or "").strip()
    if(not raw):
        raise RuntimeError("Bing %s worker exited without a result (code=%s)" % (label, proc.poll()))
    try:
        envelope=json.loads(raw)
    except Exception as exc:
        raise RuntimeError("Bing %s worker returned invalid JSON: %r" % (label, raw[:200])) from exc
    if(not isinstance(envelope, dict)):
        raise RuntimeError("Bing %s worker returned an invalid envelope" % label)
    if(not envelope.get("ok")):
        raise RuntimeError(str(envelope.get("error") or "Bing %s worker failed" % label))
    return(envelope)

def _parseWorkerEnvelope(raw: str, proc):
    """解 worker stdout 信封，形状不对一律 RuntimeError"""
    envelope=_workerEnvelopeDict(raw, proc, "search")
    results=envelope.get("results") or []
    if(not isinstance(results, list)):
        raise RuntimeError("Bing worker returned non-list results")
    return(results)

def _parseExtractEnvelope(raw: str, proc):
    """解 extract worker 信封，取 raw_content 字符串"""
    envelope=_workerEnvelopeDict(raw, proc, "extract")
    content=envelope.get("raw_content")
    if(not isinstance(content, str)):
        raise RuntimeError("Bing extract worker returned no raw_content")
    return(content)

def _runWorkerBounded(request: dict, timeoutSecs: int, label: str = "search"):
    """一次性子进程跑一个请求，硬墙钟超时 terminate→kill，返回 (stdout, proc)。
    父进程绝不 join 可能持 GIL 卡在 native 的子进程——轮询 communicate 线程+截止+中断旗标。
    抛 TimeoutError / InterruptedError；stdout 合法性交给调用方解析。"""
    import concurrent.futures as cf
    import json
    import os
    import time
    global _lastWorkerProc
    secs=max(1, int(timeoutSecs))  # 0 秒 = 每次立刻超时，钳到 1
    env=_workerEnvironment()
    proc=_spawnSearchWorker(env)
    _lastWorkerProc=proc
    pool=cf.ThreadPoolExecutor(max_workers=1)
    fut=pool.submit(proc.communicate, json.dumps(request))
    interrupted=False
    done=False
    raw=""
    try:
        deadline=time.monotonic() + secs
        while(not done and not (interrupted := _searchInterrupted()) and (remaining := deadline - time.monotonic()) > 0):
            try:
                raw=fut.result(timeout=min(_pollIntervalSecs, remaining))[0] or ""
                done=True
            except cf.TimeoutError:
                pass
    finally:
        _terminateAndReap(proc)
        if(not fut.done()):
            try:
                raw=raw or fut.result(timeout=_terminateGraceSecs)[0] or ""
            except Exception:
                pass
        pool.shutdown(wait=False, cancel_futures=True)
    if(interrupted):
        raise InterruptedError("Bing %s interrupted" % label)
    if(not done):
        raise TimeoutError("Bing %s timed out after %ds" % (label, secs))
    return(raw, proc)

def runHtmlSearchBounded(queue: str):
    """
    一次性子进程跑 ladderSearchHtml，硬墙钟超时 terminate→kill（D9 根治 GIL 冻结）。
    v1.4：轮询/杀进程逻辑搬到 _runWorkerBounded，和 extract 通道共用。
    抛 TimeoutError / RuntimeError / InterruptedError。
    """
    import json
    import os
    global _lastSearchDiag
    # 把当前开关快照转发给子进程（子进程没有活 ctx，靠 _SettingsShim 读）
    request={"op": "search", "queue": queue, "settings": _snapshotSettings()}
    if(os.environ.get("HERMES_BING_ALLOW_TEST_HOOKS") == "1" and _testHook):
        request["test_hook"]=_testHook
    timeoutSecs=readIntSetting("search_timeout_secs", 45)
    raw, proc=_runWorkerBounded(request, timeoutSecs, "search")
    results=_parseWorkerEnvelope(raw, proc)
    # F17：子进程里 ladderSearchHtml 记的诊断随信封回来（零命中时要给调用方看尝试档数/最终URL）
    try:
        debug=json.loads((raw or "").strip()).get("debug")
    except Exception:
        debug=None
    if(isinstance(debug, dict) and debug):
        _lastSearchDiag=debug
    return(results)

def _extractDatStub(url):
    """extract 单项返回骨架（键结构与 v1.3 完全一致，只有 raw_content 由子进程填）"""
    return({"url": url, "title": "Too lazy to parse out, see below.", "content": "Too lazy to parse out, see below.", "raw_content": ""}) # "我懒得解析出来了，直接看下面吧"

def _runOneExtractBounded(url, settingsSnapshot, timeoutSecs):
    """单个 URL：起子进程抓，硬墙钟超时 terminate→kill"""
    import os
    request={"op": "extract", "url": url, "settings": settingsSnapshot}
    if(os.environ.get("HERMES_BING_ALLOW_TEST_HOOKS") == "1" and _testHook):
        request["test_hook"]=_testHook
    raw, proc=_runWorkerBounded(request, timeoutSecs, "extract")
    return(_parseExtractEnvelope(raw, proc))

def runExtractBounded(urls):
    """extract 通道子进程隔离（N2）：每 URL 一个一次性子进程 + 硬墙钟 terminate→kill
    （默认 30s，settings.extract_timeout_secs 可覆盖）。超时/崩溃只标记该 URL 的 error，
    其余 URL 继续跑——primp 的 GIL 冻结再也冻不到网关进程，父进程只读 stdout 的 JSON。
    F18：父进程入口先校验 scheme/内网（子进程不再判，免得绕路）。
    返回 extract() 契约同款 list[dict]。"""
    timeoutSecs=readIntSetting("extract_timeout_secs", 30)
    settingsSnapshot=_snapshotSettings()
    respDat=[]
    for url in (urls or []):
        dat=_extractDatStub(url)
        reason=checkExtractUrlAllowed(url)
        if(reason is not None):
            dat["error"]=reason
        else:
            try:
                dat["raw_content"]=_runOneExtractBounded(url, settingsSnapshot, timeoutSecs)
            except Exception as e:
                # 单个 URL 超时/崩溃只标记该项 error，其他 URL 照常返回（不整批失败）
                dat["error"]=str(e)
        respDat.append(dat)
    return(respDat)

def searchHtmlEntry(queue: str):
    """search 统一入口：缓存（可选）→ 子进程隔离（可选）→ 进程内直跑（兜底）。
    subprocess_isolation 关掉=退回 v1.0 式进程内直跑，有 GIL 冻结风险，README 写明。"""
    if(bool(getSetting("result_cache"))):
        hit=cacheGet(queue)
        if(hit is not None):
            return(hit)
    if(bool(getSetting("subprocess_isolation"))):
        parsed=runHtmlSearchBounded(queue)
    else:
        parsed=ladderSearchHtml(queue)
    if(bool(getSetting("result_cache")) and parsed):
        cachePut(queue, parsed)
    return(parsed)

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
        return "Bing Search (Direct Crawl HTML, v1.3)"

    def is_available(self) -> bool:
        # Cheap check — env var present, optional dep importable, etc.
        # MUST NOT make network calls (runs on every `hermes tools` paint).
        # v1.4：改 find_spec 探 primp 是否可导入——父进程自己不再 import 它（N2：primp 只活在子进程里）
        try:
            import importlib.util
            return(importlib.util.find_spec("primp") is not None)
        except Exception:
            return(False)

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        response=[]
        idx=1
        
        try:
            # v1.3 接线：纯 HTML 通道（不截词+多级容错解析+子进程隔离），
            # 替代 v1.0 的 RSS 三档阶梯（RSS 蜜罐静默降级已弃，见 PROMPT.md）。
            # v1.0 原接线保留在注释：parsedRss=ladderSearch(queue=query)
            parsedRss=searchHtmlEntry(queue=query)
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

        # F17：零命中不再静默 success=True + 空表（最坏形态是让模型断言"网上没有"）。
        # 只有显式打开 settings.allow_empty 才放行空表；debug 带尝试档数/最终 URL 便于归因。
        if(not response):
            if(bool(getSetting("allow_empty"))):
                return {
                    "success": True,
                    "data": {
                        "web": []
                    }
                }
            return {
                "success": False,
                "error": "no results parsed (possible block/markup drift)",
                "debug": dict(_lastSearchDiag or {}),
            }

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
        urls=list(urls or [])
        if(bool(getSetting("subprocess_isolation"))):
            # N2：默认每 URL 一个一次性子进程 + 硬墙钟（父进程不再 import primp，GIL 冻结止步于子进程）
            return(runExtractBounded(urls))

        # subprocess_isolation=false 时退回进程内直抓（v1.3 原行为，primp 持 GIL 有冻结风险）
        respDat=[]

        for url in urls:
            dat=_extractDatStub(url)
            reason=checkExtractUrlAllowed(url)  # F18：只放行 http/https，IP 字面量不得落内网/保留段
            if(reason is not None):
                dat["error"]=reason
                respDat.append(dat)
                continue
            try:
                dat["raw_content"]=crawlUrlRawHtmlStealth(url)  # v1.3 伪装版（桌面指纹+伪装头+Bing Referer）
            except Exception as e:
                # 单个 URL 失败只标记该项 error，其他 URL 照常返回（不整批失败）
                dat["error"]=str(e)
            respDat.append(dat)

        return respDat
        


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    global _pluginCtx
    _pluginCtx=ctx
    # 逐键自动补齐缺失 settings（主人 11-09 DIY 规范：只补缺键，显式值不覆盖，走官方 writer）
    seedMissingSettings(ctx)
    ctx.register_web_search_provider(BingCrawlerWebSearchProvider())