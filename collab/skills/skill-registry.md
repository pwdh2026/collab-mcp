---
{
  "name": "skill-registry",
  "description": "技能注册表自举：list_skills 浏览、find_skill 检索、register_skill 登记新技能（本技能说明如何用它）",
  "capability_tags": ["skill:registry"],
  "entry": "tools:list_skills,find_skill,register_skill",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：新队友/新会话想了解平台有什么可执行技能，或要登记新技能时。
使用：list_skills 浏览全部；find_skill(query/tags) 按需检索（tags 为精确主通道）；
register_skill 登记新技能（name 大小写不敏感统一小写；entry 用 template:/pipeline:/tools:/doc: 前缀）。
