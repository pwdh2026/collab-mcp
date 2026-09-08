---
{
  "name": "collab-messaging",
  "description": "协作消息：send_message / get_chat_history（任务线程沟通，以 chat 为准）",
  "capability_tags": ["collab:message"],
  "entry": "tools:send_message,get_chat_history",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：与队友沟通、汇报任务进展时。
使用：调用 send_message / get_chat_history（可带 task_id 关联任务）。
