---
{
  "name": "ai-teacher",
  "description": "AI 老师：纯只读通俗讲解 agent 工作成果（git 版本区间 / collab 任务 / 文件目录，如 WorkBuddy 日志），四段式输出：一句话总结 / 分块人话讲解 / 证据引用 / 检查清单+方向选项；防反噬：必引证据、讲完带作业、纯只读。",
  "capability_tags": [
    "teach:explain-work",
    "review:evidence",
    "read-only"
  ],
  "entry": "tools:explain_work",
  "scope": "task",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-08T14:21:25.431015+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

# AI 老师（explain_work）

> 用途：通俗讲解 agent 干完活的成果（版本区间 / collab 任务 / 文件或目录），
> 带证据引用与检查清单，防"看不懂→只能全权放权→思考力下降"的循环。
> 入口：MCP 工具 `explain_work` 或 CLI `python collab/scripts/ai_teacher.py`。

## 三个防反噬原则（实现内强制）
1. **必须引用证据**：每条讲解挂到具体文件/commit/测试/报告；证据里没有的写
   "材料未说明/这里没证据"，禁止编造原因。
2. **讲完带作业**：始终输出 3-5 个用户可亲自核实的检查点，判断权留给人。
3. **纯只读**：只收集产物→讲解；不执行、不修改被讲解对象；唯一写操作=
   把讲解存到 results/（UTF-8）。

## 触发场景
- 用户问"刚才这次工作干了什么 / 讲讲这次改动 / 这个文件（或目录）是怎么回事 /
  workbuddy 在后台做什么"，且对象是：git 版本区间、collab 任务、文件或目录。

## 使用方式
- `explain_work(milestone="v3.21.0")`：git 版本区间，自动收集 commit、变更统计、
  闸门报告（results/pc-c-*verify*.md）、release notes。
- `explain_work(task_id="0095655d3aea")`：collab 任务，读任务文件 + journal 时间线
  + evidence/result_path 预览。
- `explain_work(path="/path/to/some-agent/logs")`：
  任意文件/目录（某办公助手的日志、产物等），收集元数据 + 内容预览
  （自动脱敏 uuid/userId/deviceId，限长，日志取尾部）。
- LLM 底座：ollama qwen2.5:3b（环境变量 OLLAMA_BASE_URL/OLLAMA_MODEL 可覆盖）；
  不可用或 --no-llm 时自动降级提取式模板，四段结构不变。

## 边界与失败
- 纯只读：不执行、不修改、不产生外部副作用。
- LLM 生成的内容标记 method=llm，且证据区与检查清单永远由确定性逻辑生成，
  用户可逐条核对。
- 找不到 milestone/task/path 时明确报错；证据不足处明说，不编造。
