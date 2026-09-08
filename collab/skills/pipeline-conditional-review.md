---
{
  "name": "pipeline-conditional-review",
  "description": "条件分支审查流水线：按条件跳过/执行步骤（条件分支能力示例）",
  "capability_tags": ["pipeline:conditional", "code-review:any"],
  "entry": "pipeline:pipeline-conditional-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要条件化执行审查步骤时。
使用：create_task(template="pipeline-conditional-review", template_params='{"target": "<目标>", "project_path": "<路径>"}')。
