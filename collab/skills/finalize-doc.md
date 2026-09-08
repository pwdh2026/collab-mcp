---
{
  "name": "finalize-doc",
  "description": "文档定稿：按复核意见修订并产出最终版本",
  "capability_tags": ["writing:finalize"],
  "entry": "template:finalize-doc",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：研究/文档流水线收尾定稿时。
使用：create_task(template="finalize-doc", template_params='{"topic": "<主题>"}')。
