---
{
  "name": "document-search",
  "description": "文档全文检索：SQLite FTS5 关键词检索已摄入文档（documents/），返回相关度与命中摘要（search_documents）",
  "capability_tags": ["document:search", "search:fulltext"],
  "entry": "tools:search_documents",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-05T08:00:00+08:00"
}
---

触发：需要在已摄入的 documents/ 里查关键词、定位相关内容时。
使用：search_documents(query="量子 纠错", target_dir="documents", limit=20)；
query 空白分隔多词、全部命中；命中结果带 bm25 相关度与上下文摘要。
摄入 → 检索闭环：add_document 摄入后可直接 search_documents 命中。
