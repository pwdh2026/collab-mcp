---
{
  "name": "ocr-delegate-review",
  "description": "open-code-review 代码审查委托：中枢生成 delegate 规格，host agent 按规则做行级代码审查（无需 API key）",
  "capability_tags": ["code-review:any", "review:delegate", "ocr:delegate"],
  "entry": "pipeline:pipeline-ocr-delegate-review",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：对某提交做 ocr 行级审查（适用无 git 队友机，方案 B）。
使用：create_task(template="pipeline-ocr-delegate-review",
template_params='{"commit": "<提交号>", "repo": "C:\\myshare", "spec_assignee": "PC-A"}')。
详见 progress/ocr-usage.md。
