---
{
  "name": "write-doc",
  "description": "撰写文档/草稿：按主题输出 Markdown 文档到指定路径",
  "capability_tags": ["writing:doc"],
  "entry": "template:write-doc",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要产出文档/草稿时。
使用：create_task(template="write-doc", template_params='{"topic": "<主题>", "file": "<输出路径>"}')。
