---
{
  "name": "research-pipeline",
  "description": "研究流水线：research → draft → review（复核闸门）→ finalize，产出结构化调研报告",
  "capability_tags": ["research:doc", "pipeline:research"],
  "entry": "pipeline:pipeline-research",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要围绕主题做调研并产出报告时。
使用：create_task(template="pipeline-research", template_params='{"topic": "<主题>"}')。
