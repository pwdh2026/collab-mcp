---
{
  "name": "pipeline-parallel-review",
  "description": "并行审查流水线：lint → [unit, integration] 并行 → review 汇聚",
  "capability_tags": ["pipeline:parallel", "code-review:any"],
  "entry": "pipeline:pipeline-parallel-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要 lint + 并行单测/集成 + 汇聚审查时。
使用：create_task(template="pipeline-parallel-review", template_params='{"target": "<目标>", "project_path": "<路径>"}')。
