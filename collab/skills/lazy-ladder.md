---
{
  "name": "lazy-ladder",
  "description": "写码前先走七级决策阶梯停在第一成立台阶（YAGNI→复用→stdlib→平台原生→已装依赖→一行→最小实现），配套过度工程审查删除清单。源自 DietrichGebert/ponytail（MIT，2026-08-07 调研）。",
  "capability_tags": [
    "review:overengineering",
    "build:minimal"
  ],
  "entry": "doc:skills-vendor/ponytail/lazy-ladder/SKILL.md",
  "scope": "task",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-07T03:39:59.952990+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

完整技能内容：doc:skills-vendor/ponytail/lazy-ladder/SKILL.md（源：DietrichGebert/ponytail，MIT，2026-08-07 调研裁剪）。

触发：编码/重构/修 bug/选依赖时想压过度工程，或审查时想加「删除清单」维度。lazy-ladder 只管建什么不管怎么说；ponytail-review 的删除清单只猎复杂度，正确性/安全留给正常审查。
