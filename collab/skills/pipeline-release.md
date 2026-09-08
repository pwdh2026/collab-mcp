---
{
  "name": "pipeline-release",
  "description": "发布流水线：质量检查 → 发布审批 → 部署确认（复核闸门）",
  "capability_tags": ["release:pipeline", "quality:check"],
  "entry": "pipeline:pipeline-release",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要走发布质量门禁时。
使用：create_task(template="pipeline-release", template_params='{"version": "<版本号>"}')。
