---
{
  "name": "pipeline-code-review",
  "description": "代码审查流水线：lint（跑测试）→ review（代码审查，复核闸门）",
  "capability_tags": ["code-review:any", "pipeline:review"],
  "entry": "pipeline:pipeline-code-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要跑测试 + 代码审查一条龙时。
使用：create_task(template="pipeline-code-review",
template_params='{"target": "<目标>", "project_path": "<路径>"}')。
