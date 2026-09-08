---
{
  "name": "anydoc-convert",
  "description": "文档格式转换摄入：把 Office 全家桶（doc/ppt/xls/odt/ods/odp/csv 等）+ PDF/DOCX/EPUB/RTF/HTML 转成 Markdown 存入共享 documents/（add_document / list_documents）",
  "capability_tags": [
    "ingest:document",
    "files:convert",
    "format:office"
  ],
  "entry": "tools:add_document,list_documents",
  "scope": "tool",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-08T10:28:46.515118+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

# anydoc-convert（文档转换摄入）

> 一句话：add_document 本身就是转换器链（anydoc 优先 + 魔数嗅探 + 降级链），不是"只入库"接口——丢一个 .odt/.pptx/.xlsx 进去，它会先转 Markdown 再写入共享 documents/。

## 触发
- 需要把一份文档/附件转成可检索、可引用的 Markdown 时
- 来源是 Office 全家桶（doc/docm/ppt/pps/pot/pptx/pptm/ppsx/ppsm/xls/xlsx/xlsm/xlsb/odt/ods/odp/csv）或 PDF/DOCX/EPUB/RTF/HTML/MD/TXT/RST/ADOC

## 使用
- add_document(source_path=<执行端绝对路径或共享相对路径>, target_dir="documents", format_hint="")
- 格式自动检测：扩展名优先 → OLE/PK/PDF 魔数嗅探兜底；格式识别不了会明确报错
- list_documents() 浏览已摄入文档；摄入后可用 search_documents / semantic_search 检索
- 可选参数：skip_duplicates（同内容跳过）、dedup_key/dedup_url（去重指纹）、extra_meta（附加元数据）

## 边界与失败语义
- 转换在工具层完成，技能文件不需要承载任何转换逻辑
- anydoc（firecrawl-anydoc）已安装：全部 office 格式 + docx/pdf/epub/rtf 走 anydoc 高质量转换
- anydoc 未安装：office 独占格式（doc/ppt/xls/odt/ods/odp/csv 等）明确报错"需要安装 firecrawl-anydoc"；docx/pdf/epub/rtf 自动走降级链（pdftotext/python-docx/ebooklib/striprtf 等），零硬依赖
- anydoc 可用但提取到空文本 → 显式报错，不做二进制 passthrough（防乱码摄入）
- 输入上限 100MB / 输出 150 万字符；PDF 先过 %PDF- 魔数校验，伪装 .pdf 的文本文件拒绝摄入
- 扫描件/无文本层 → 明确报错"未能提取到文本"
