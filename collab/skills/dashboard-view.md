---
{
  "name": "dashboard-view",
  "description": "协作看板：生成静态只读看板（状态统计 + 流水线 DAG 节点图）",
  "capability_tags": ["observability:dashboard"],
  "entry": "tools:generate_dashboard",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要总览平台任务/流水线状态时。
使用：调用 generate_dashboard 工具。
