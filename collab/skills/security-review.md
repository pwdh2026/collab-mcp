---
{
  "name": "security-review",
  "description": "安全审查：按注入/越权/路径穿越/密钥管理/供应链五类审查目标（复核闸门）",
  "capability_tags": ["security:review"],
  "entry": "template:security-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要对目标做安全专项审查时。
使用：create_task(template="security-review", template_params='{"target": "<目标>", "project_path": "<路径>"}')。
