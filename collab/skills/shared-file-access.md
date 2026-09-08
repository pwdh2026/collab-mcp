---
{
  "name": "shared-file-access",
  "description": "共享文件夹读写：list_shared_dir / read_shared_file（队友机经 MCP 访问共享目录）",
  "capability_tags": ["files:read", "files:list"],
  "entry": "tools:read_shared_file,list_shared_dir",
  "scope": "tool",
  "status": "ready",
  "created_by": "PC-A",
  "created_at": "2026-08-04T00:00:00+08:00"
}
---

触发：队友机需要读/列共享目录内容时。
使用：调用 read_shared_file / list_shared_dir（相对共享根路径）。
