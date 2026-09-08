---
{
  "name": "karpathy-discipline",
  "description": "写码前的四条 Karpathy 编码纪律（先思考/最小实现/外科手术式修改/目标驱动执行），防 LLM 常见编码通病；与 lazy-ladder 决策阶梯互补。源自 multica-ai/andrej-karpathy-skills（MIT，2026-08-08 调研裁剪）。",
  "capability_tags": [
    "build:minimal",
    "review:overengineering",
    "coding:discipline"
  ],
  "entry": "",
  "scope": "task",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-08T07:18:30.768025+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

# Karpathy 编码纪律（源自 andrej-karpathy-skills）

> 源：https://github.com/multica-ai/andrej-karpathy-skills（MIT）｜本平台 2026-08-08 调研裁剪。
> 一句话：先想清楚再写码；能少写就少写；只改该改的；用成功标准驱动执行。
> 与 lazy-ladder（决策阶梯）互补：lazy-ladder 决定"做不做/怎么做最省"，本纪律约束"怎么写码/改码"。

## 1. Think Before Coding（先思考）
- 显式陈述假设；不确定就问，不猜。
- 有多种解读就摆出来，别默默选一个。
- 有更简单方案就说出来，该反驳就反驳。
- 感到困惑就停下，点名不清楚之处并提问。

## 2. Simplicity First（最小实现）
- 不加没被要求的特性；不做单点抽象；不造没要求的"灵活性"。
- 不处理不可能发生的错误；200 行能 50 行就重写。
- 自问：资深工程师会说这过度复杂吗？会 → 简化。

## 3. Surgical Changes（外科手术式修改）
- 只改必须改的；不顺手"改进"相邻代码/注释/格式；不重构没坏的东西。
- 匹配现有风格；发现无关死代码 → 提一句，别删。
- 自己改动造成的孤儿（未用 import/变量/函数）→ 清掉；存量死代码不动。

## 4. Goal-Driven Execution（目标驱动）
- 把命令式任务转成可验证目标：
  - "加校验" → "先写无效输入测试再让它过"
  - "修 bug" → "先写复现测试再让它过"
  - "重构 X" → "前后测试都绿"
- 多步任务给计划：1. [Step] → verify: [check] ...（对应 collab 任务的 evidence 协议）
- 强验收标准让模型独立闭环；弱标准（"弄好它"）只会来回问。

## 启用强度
- trivial（改 typo/一行）→ 不用全套。
- 正常任务 → 默认全过一遍（对照自查）。
- 复杂/高风险 → 在答复里显式列出 4 条如何满足（防过度设计 + 外科手术）。
