---
{
  "name": "project-lock",
  "description": "项目中央锁：acquire/release/list_project_lock（改共享代码前取锁，防 Git 冲突）",
  "capability_tags": ["collab:lock"],
  "entry": "tools:acquire_project_lock,release_project_lock,list_project_locks",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：修改 collab 共享代码前。
使用：先 acquire_project_lock，完成后 release_project_lock（2h TTL，过期可接管）。
