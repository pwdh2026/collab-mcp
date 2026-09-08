---
{
  "name": "web-research-agent",
  "description": "网络调研：web_research 单问题多源研究简报（默认首选），web_agent 任务型自主数据收集（可带种子 URL/多页深挖），配合 web_search/web_fetch 抓取；SSRF 防护与 citation 净化内置",
  "capability_tags": [
    "web:research",
    "search:external",
    "agent:research"
  ],
  "entry": "tools:web_research,web_agent",
  "scope": "task",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-08T10:28:46.520152+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

# web-research-agent（网络调研）

> 一句话：需要"最新/外部信息、多源对比"时走这里。两个工具的分工和路由规则必须照下面执行，不要混用。

## 硬路由（先读这个）
- 默认 web_research：用户给"一个主题/问题"，要结构化多源简报 → web_research(question=..., depth="standard")
  - 例："查一下 AI 新闻""调研 X 的现状""Y 和 Z 有什么区别"
  - 关键词：查/调研/现状/最新/对比/介绍一下
- web_agent：用户给"任务指令"、要按给定 URL 深挖、或需要多页自主收集 → web_agent(prompt=..., urls=..., max_pages=..., max_time_ms=...)
  - 例："去这些站点收集 X 的定价""按步骤调研 X 并整理报告"
  - 关键词：去/收集/深挖/展开/按这个清单/任务
- 串行组合：先 web_research 拿简报，需深挖其中某个来源 → web_fetch(url=...) 抓正文；需在给定 URL 上做自主多页采集 → web_agent(urls=...)

## 使用
- web_research(question, depth="standard|quick|comprehensive", max_sources, include_domains, exclude_domains, timeout)
- web_agent(prompt, urls="逗号分隔", max_pages=1..100, max_time_ms=5000..120000, timeout)
- 两者返回统一结构：report / citations / sources_count / heuristic / ssrf_removed

## 边界与失败语义
- 两者都依赖 wigolo serve（WIGOLO_REST_URL 或默认 127.0.0.1:3333）；不可用时明确报错
- 无 LLM 配置 → heuristic 兜底简报（heuristic=true，零 API 成本，质量较低）；配置 LLM 后为综合简报（消耗 token）
- web_agent 可配 WEB_AGENT_PROVIDER=firecrawl（需 FIRECRAWL_API_KEY），未配置或未装时自动走 wigolo
- 慢源可能超时（timeout 参数，默认 90s，web_agent 预算 ≥60s 且只增不减）；超时/空结果 → 明确失败，不静默空成功
- SSRF：种子 URL 与 citation 都过 _check_url 净化，内网/本机地址被拒；被剔除的会标记 ssrf_removed
- 报告截断到预览上限，truncated=true 时完整输出需再调用或走文件
