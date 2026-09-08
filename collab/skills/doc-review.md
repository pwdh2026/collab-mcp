---
{
  "name": "doc-review",
  "description": "文档复核：审查文档草稿的内容准确性、结构完整性与可读性（复核闸门）",
  "capability_tags": ["review:doc", "writing:review"],
  "entry": "template:doc-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要复核文档草稿时。
使用：create_task(template="doc-review", template_params='{"target": "<草稿/文档>"}')，输出复核意见，走复核闸门。
