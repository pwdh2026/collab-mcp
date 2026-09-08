---
{
  "name": "document-intake",
  "description": "文档摄入：把 PDF/DOCX/HTML/EPUB/RTF/MD 等附件转成 Markdown 存入共享 documents/ 目录（add_document / list_documents）",
  "capability_tags": ["ingest:document", "files:convert"],
  "entry": "tools:add_document,list_documents",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-05T00:00:00+08:00"
}
---

触发：需要把一份文档/附件转成可检索、可引用的 Markdown 时。
使用：add_document(source_path=<执行端绝对路径或共享相对路径>, target_dir="documents")；
list_documents() 浏览已摄入文档。转换器自动降级（pdftotext→markitdown→PyPDF2→pdfminer 等），零硬依赖。
