{
  "name": "web-search",
  "description": "外部互联网检索：web_search 查询互联网（Bing 免费默认 + DuckDuckGo/Firecrawl 自动降级），web_fetch 抓取网页转文本并默认入库 documents/web/（Firecrawl 优先、标准库兜底）",
  "capability_tags": ["web:search", "web:fetch", "search:external"],
  "entry": "tools:web_search,web_fetch",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-05T10:00:00+08:00"
}
---

触发：需要互联网上的新信息（内部语料 documents/ 里没有、或需要最新资讯）时。
使用：web_search(query="Python 异步 教程", max_results=10)；拿到候选后
web_fetch(url="https://...", save=true) 抓正文，默认入库 documents/web/，
之后可用 search_documents 在本地二次检索抓回来的内容。
注意：web_fetch 有 SSRF 防护，内网/本机地址会被拒绝；纯 JS 页面可能无正文。
