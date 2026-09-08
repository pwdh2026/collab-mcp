---
{
  "name": "code-review",
  "description": "按五轴（功能/安全/并发/错误处理/可读性）对指定目标做代码审查并输出分级问题清单",
  "capability_tags": ["code-review:any", "review:code"],
  "entry": "template:code-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要审查某段代码/PR/提交时。
使用：create_task(template="code-review", template_params='{"target": "..."}')，输出分级问题清单，走复核闸门。
