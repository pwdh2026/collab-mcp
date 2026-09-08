---
{
  "name": "evidence-backed-pr-review",
  "description": "PR 证据化审查：先核实目标可行性（避免重复劳动），再本地复现→跑测试→边界探测（含非法输入）→未打补丁基线对照→分级证据化提交 review。适合审查他人 PR、开源贡献与闸门复核。",
  "capability_tags": [
    "review:evidence",
    "pr:review",
    "verification:gate"
  ],
  "entry": "doc:results/python-sdk-pr2984-review-20260809.md",
  "scope": "task",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-09T06:36:14.337706+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

# PR 证据化审查（对抗性审查在开源 PR 上的落地）

> 一句话：先问「这个活还轮得到我吗」，再「用证据说话」：本地跑、边界打、基线比，最后把每一句结论都挂上证据。

## 1. 先核实目标可行性（开工前）
- issue 是否已有 PR？评论区维护者态度？（被关闭的 PR 也是信号：维护者可能在等官方方案）
- 检查同 issue 的多个 PR、assignee、milestone；避免重复劳动
  （实证：#226 三条 PR 被关/挂起、维护者等 pydantic 官方包 → 放弃实现）

## 2. 本地复现 + 测试
- 克隆仓库到隔离工作区；装最小依赖集直跑源码（不要默认全量环境，按需补齐）
- 跑 PR 新增测试 + 相关测试目录（记录 N passed / N skipped，注明环境）

## 3. 边界探测（对抗性审查核心）
- 向后兼容（旧路径行为是否变化）
- 非法输入（类型错误/越界/空值——很多崩溃藏在「用户传错」路径）
- 同步/异步两条路径；别名/强转行为；空容器
- 对每个异常记录完整异常链（ExceptionGroup → 根因 ValidationError）

## 4. 基线对照
- 在未打补丁的 base 上跑同一组探测，确认「崩溃/行为差异是 PR 引入还是既有」
- 结论可归因：PR 新增 API 的锐边 vs 存量问题

## 5. 提交审查
- 结论分级：功能正确性 / 回归 / 设计 / 类型 / 流程（rebase），每项附证据
- 无仓库写权限时用 COMMENT 形式提交；正文含测试统计、探测结果、对照表
- 落盘本地报告（results/）+ 可核实检查点

## 实测案例
- python-sdk PR #2984：PR 新增测试 4/4 过、mcpserver 目录 341 passed/1 skipped、
  6 探测场景、非法 content 项 → ExceptionGroup 崩溃（基线对照确认 PR 引入）、
  提交 COMMENT review（2026-08-09）。
