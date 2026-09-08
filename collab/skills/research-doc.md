---
{
  "name": "research-doc",
  "description": "调研要点：围绕主题收集事实/方案对比/来源清单，输出调研要点（供 pipeline-research 使用）",
  "capability_tags": ["research:collect"],
  "entry": "template:research-doc",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要围绕主题做事实收集与方案对比时。
使用：create_task(template="research-doc", template_params='{"topic": "<主题>"}')。
