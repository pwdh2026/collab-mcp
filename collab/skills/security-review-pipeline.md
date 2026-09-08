---
{
  "name": "security-review-pipeline",
  "description": "安全审查流水线：lint（跑测试）→ security-review（五类安全审查，复核闸门）",
  "capability_tags": ["security:review", "pipeline:security"],
  "entry": "pipeline:pipeline-security-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要对目标做安全扫描/审查时。
使用：create_task(template="pipeline-security-review",
template_params='{"target": "<目标>", "project_path": "<路径>"}')。
