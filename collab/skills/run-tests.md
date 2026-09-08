---
{
  "name": "run-tests",
  "description": "运行项目测试并汇报：执行全量测试、输出结果摘要与失败定位",
  "capability_tags": ["testing:unit", "testing:run"],
  "entry": "template:run-tests",
  "scope": "task",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：需要跑测试并汇报结果时。
使用：create_task(template="run-tests", template_params='{"project_path": "<路径>"}')。
注意：hub VM 默认 python=python2，用 python3.11。
