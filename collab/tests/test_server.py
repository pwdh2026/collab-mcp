"""collab_mcp 回归测试（unittest 编写，pytest 亦兼容）。

运行方式：
    cd C:\\myshare\\collab
    python -m unittest discover -s tests -v
或（安装 pytest 后）：
    python -m pytest tests -v
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

# 让本文件可独立运行：把仓库 collab/ 加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在导入 collab_mcp 之前设置独立的测试目录，避免污染真实协作数据
_TEST_DIR = Path(tempfile.mkdtemp(prefix="collab_test_"))
os.environ["COLLAB_DIR"] = str(_TEST_DIR)
# M2（PC-B 验证发现）：测试套件必须 hermetic——VM authorized_keys 会向 SSH
# 会话注入 REQUIRE_IDENTITY=1 + COLLAB_IDENTITY=<队友名>，若不隔离，
# 经 SSH 按文档命令跑测试会出现 7 个身份用例失败（非功能回归）。
for _identity_env in (
    "COLLAB_IDENTITY",
    "REQUIRE_IDENTITY",
    "COLLAB_ROLE",
    "COLLAB_HTTP",
    "COLLAB_HTTP_BEARER_TOKEN",
    "COLLAB_HTTP_IDENTITY",
    "COLLAB_HTTP_ALLOWED_IDENTITIES",
):
    os.environ.pop(_identity_env, None)

from collab_mcp import __version__  # noqa: E402
from collab_mcp import (  # noqa: E402
    ai_teacher,
    artifacts,
    body,
    chat,
    codegraph,
    dashboard,
    delegation,
    documents,
    files,
    fixit,
    health,
    journal,
    locks,
    media,
    memory,
    rag,
    schedules,
    search,
    semantic,
    skills,
    summarize,
    status,
    tasks,
    teammates,
    vision,
    websearch,
)
import notify_daemon  # noqa: E402
from collab_mcp import notifications  # noqa: E402
from collab_mcp.app import server  # noqa: E402
from collab_mcp.config import (  # noqa: E402
    CHAT_DIR,
    COLLAB_DIR,
    DONE_DIR,
    INBOX_DIR,
    MEMORY_DIR,
    SCHEDULES_DIR,
    SKILLS_DIR,
    DOCS_DIR,
    ensure_directories,
)
from collab_mcp.utils import safe_read_json, safe_write_json  # noqa: E402

# 仓库 collab/ 目录（含 .codegraph 索引，用于 CodeGraph 相关测试）
REPO_DIR = Path(__file__).resolve().parent.parent

# v1.8.6：索引按约定不入库（.codegraph/.gitignore），干净 CI 环境没有索引时
# 跳过依赖索引的用例，保证云端 108 测试全绿；本地/VM 有索引则照常执行。
HAS_CODEGRAPH_INDEX = (REPO_DIR / ".codegraph" / "codegraph.db").exists()

TOOL_NAMES = [
    "bootstrap",
    "heartbeat",
    "mark_research_done",
    "dashboard_data",
    "create_task",
    "register_artifact",
    "verify_artifact",
    "get_pending_tasks",
    "complete_task",
    "claim_task",
    "claim_next_task",
    "get_task_context",
    "force_assign",
    "approve_task",
    "request_changes",
    "fail_task",
    "list_templates",
    "should_delegate",
    "acquire_project_lock",
    "release_project_lock",
    "list_project_locks",
    "send_message",
    "get_chat_history",
    "get_collab_status",
    "get_task_metrics",
    "generate_dashboard",
    "health_check",
    "register_teammate",
    "list_teammates",
    "query_codegraph",
    "list_shared_dir",
    "read_shared_file",
    "get_notifications",
    "remember_fact",
    "search_memory",
    "list_skills",
    "find_skill",
    "register_skill",
    "add_document",
    "list_documents",
    "search_documents",
    "search_troubleshooting",
    "semantic_search",
    "rag_query",
    "web_search",
    "web_fetch",
    "web_research",
    "web_agent",
    "transcribe_media",
    "summarize_text",
    "explain_work",
    "analyze_observation",
    "execute_action",
    "schedule_recurring_task",
    "list_schedules",
    "set_schedule_enabled",
]


def _parse(result: str) -> dict:
    return json.loads(result)


def _reset_collab_dir() -> None:
    """清空测试协作目录（含注册表），保证每个测试用例独立。"""
    ensure_directories()
    for d in (INBOX_DIR, DONE_DIR, CHAT_DIR, COLLAB_DIR / "locks", SCHEDULES_DIR):
        for f in d.glob("*.json"):
            f.unlink()
    if MEMORY_DIR.is_dir():
        for f in MEMORY_DIR.glob("*.md"):
            f.unlink()
    journal_dir = COLLAB_DIR / "journal"
    if journal_dir.is_dir():
        for f in list(journal_dir.glob("*.jsonl")) + list(journal_dir.glob(".*.lock")):
            f.unlink()
    registry = COLLAB_DIR / "teammates.json"
    if registry.exists():
        registry.unlink()


class ArtifactTest(unittest.IsolatedAsyncioTestCase):
    """v3.33.0：大文件交付登记 register_artifact / verify_artifact。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        shutil.rmtree(artifacts.ARTIFACTS_DIR, ignore_errors=True)
        self._saved_identity = {
            k: os.environ.get(k) for k in ("COLLAB_IDENTITY", "REQUIRE_IDENTITY", "COLLAB_ROLE")
        }
        for k in self._saved_identity:
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        shutil.rmtree(artifacts.ARTIFACTS_DIR, ignore_errors=True)
        for k, v in self._saved_identity.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def _new_task(self) -> str:
        r = _parse(await tasks.create_task("带附件任务", "登记大文件"))
        self.assertTrue(r["success"])
        return r["task_id"]

    def _write_artifact(self, task_id: str, rel_path: str, content: bytes) -> Path:
        target = artifacts.ARTIFACTS_DIR / task_id / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    async def test_register_artifact_writes_manifest(self):
        task_id = await self._new_task()
        content = b"hello artifact v3.33"
        self._write_artifact(task_id, "data.bin", content)

        r = _parse(await artifacts.register_artifact(
            task_id,
            "data.bin",
            hashlib.sha256(content).hexdigest(),
            len(content),
        ))
        self.assertTrue(r["success"], r)
        manifest = safe_read_json(
            artifacts.ARTIFACTS_DIR / task_id / "manifest.json"
        )
        self.assertEqual(manifest["task_id"], task_id)
        self.assertEqual(manifest["artifacts"][0]["rel_path"], "data.bin")
        self.assertEqual(manifest["artifacts"][0]["size"], len(content))

    async def test_register_rejects_size_mismatch(self):
        task_id = await self._new_task()
        content = b"123456"
        self._write_artifact(task_id, "data.bin", content)

        r = _parse(await artifacts.register_artifact(
            task_id,
            "data.bin",
            hashlib.sha256(content).hexdigest(),
            len(content) + 1,
        ))
        self.assertFalse(r["success"])
        self.assertIn("size", r["error"])

    async def test_register_rejects_sha_mismatch(self):
        task_id = await self._new_task()
        self._write_artifact(task_id, "data.bin", b"abc")

        r = _parse(await artifacts.register_artifact(
            task_id,
            "data.bin",
            "0" * 64,
            len(b"abc"),
        ))
        self.assertFalse(r["success"])
        self.assertIn("sha256", r["error"])

    async def test_register_rejects_traversal_rel_path(self):
        task_id = await self._new_task()
        r = _parse(await artifacts.register_artifact(
            task_id,
            "../escape.bin",
            "0" * 64,
            0,
        ))
        self.assertFalse(r["success"])
        self.assertIn("相对路径", r["error"])

    async def test_register_rejects_unknown_task(self):
        r = _parse(await artifacts.register_artifact(
            "deadbeef0000",
            "data.bin",
            "0" * 64,
            0,
        ))
        self.assertFalse(r["success"])
        self.assertIn("不存在", r["error"])

    async def test_verify_passes_after_register(self):
        task_id = await self._new_task()
        content = b"verify me once"
        self._write_artifact(task_id, "data.bin", content)
        reg = _parse(await artifacts.register_artifact(
            task_id,
            "data.bin",
            hashlib.sha256(content).hexdigest(),
            len(content),
        ))
        self.assertTrue(reg["success"], reg)

        manifest = artifacts.ARTIFACTS_DIR / task_id / "manifest.json"
        r = _parse(await artifacts.verify_artifact(str(manifest), "data.bin"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(r["verified"])

    async def test_verify_fails_after_tamper(self):
        task_id = await self._new_task()
        self._write_artifact(task_id, "data.bin", b"original")
        reg = _parse(await artifacts.register_artifact(
            task_id,
            "data.bin",
            hashlib.sha256(b"original").hexdigest(),
            len(b"original"),
        ))
        self.assertTrue(reg["success"], reg)

        self._write_artifact(task_id, "data.bin", b"tampered")
        manifest = artifacts.ARTIFACTS_DIR / task_id / "manifest.json"
        r = _parse(await artifacts.verify_artifact(str(manifest), "data.bin"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["status"], "FAIL")
        self.assertFalse(r["verified"])

    async def test_verify_reports_missing_file_as_fail(self):
        task_id = await self._new_task()
        content = b"gone soon"
        self._write_artifact(task_id, "data.bin", content)
        reg = _parse(await artifacts.register_artifact(
            task_id,
            "data.bin",
            hashlib.sha256(content).hexdigest(),
            len(content),
        ))
        self.assertTrue(reg["success"], reg)
        (artifacts.ARTIFACTS_DIR / task_id / "data.bin").unlink()

        manifest = artifacts.ARTIFACTS_DIR / task_id / "manifest.json"
        r = _parse(await artifacts.verify_artifact(str(manifest), "data.bin"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("缺失", r["message"])

    async def test_identity_gate_under_require_identity(self):
        os.environ["REQUIRE_IDENTITY"] = "1"
        r = _parse(await artifacts.register_artifact(
            "deadbeef0000",
            "data.bin",
            "0" * 64,
            0,
        ))
        self.assertFalse(r["success"])
        self.assertIn("需要调用者身份", r["error"])


class TaskLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_create_and_list_pending(self):
        r = _parse(await tasks.create_task("测试任务", "任务内容", assignee="PC-B"))
        self.assertTrue(r["success"])
        task_id = r["task_id"]
        self.assertEqual(len(task_id), 12)  # v1.3.2 起 ID 加长到 12 位，降低碰撞
        self.assertTrue((INBOX_DIR / f"{task_id}.json").exists())

        pending = _parse(await tasks.get_pending_tasks())
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["tasks"][0]["title"], "测试任务")
        self.assertEqual(pending["tasks"][0]["status"], "pending")

    async def test_create_task_with_execution_env(self):
        r = _parse(await tasks.create_task(
            "带环境任务", "x", assignee="PC-B", execution_env="hub VM"))
        self.assertTrue(r["success"])
        task_id = r["task_id"]
        saved = json.loads((INBOX_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["execution_env"], "hub VM")

        # 未指定时不落该字段，保持向后兼容
        r2 = _parse(await tasks.create_task("普通任务", "x"))
        saved2 = json.loads((INBOX_DIR / f"{r2['task_id']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("execution_env", saved2)

    async def test_pending_filter_by_assignee(self):
        await tasks.create_task("任务A", "x", assignee="PC-B")
        await tasks.create_task("任务B", "y", assignee="PC-C")

        only_b = _parse(await tasks.get_pending_tasks(assignee="PC-B"))
        self.assertEqual(only_b["count"], 1)
        self.assertEqual(only_b["tasks"][0]["title"], "任务A")

        # assignee=any 的任务对所有实例可见
        await tasks.create_task("任务C", "z", assignee="any")
        for_pc_b = _parse(await tasks.get_pending_tasks(assignee="PC-B"))
        self.assertEqual(for_pc_b["count"], 2)

        all_tasks = _parse(await tasks.get_pending_tasks())
        self.assertEqual(all_tasks["count"], 3)

    async def test_complete_task_moves_to_done(self):
        r = _parse(await tasks.create_task("完成任务", "done"))
        task_id = r["task_id"]

        done = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertTrue(done["success"])
        self.assertFalse((INBOX_DIR / f"{task_id}.json").exists())
        self.assertTrue((DONE_DIR / f"{task_id}.json").exists())

        saved = json.loads((DONE_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "done")
        self.assertIsNotNone(saved["completed_at"])

    async def test_complete_missing_task_fails(self):
        r = _parse(await tasks.complete_task("deadbeef", evidence="test"))
        self.assertFalse(r["success"])
        self.assertIn("未找到", r["error"])

    async def test_complete_task_idempotent(self):
        r = _parse(await tasks.create_task("幂等任务", "x"))
        task_id = r["task_id"]
        first = _parse(await tasks.complete_task(task_id, evidence="test"))
        second = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])  # 重复完成不报错
        self.assertEqual(second["task_title"], "幂等任务")

    async def test_complete_task_stores_token_usage(self):
        r = _parse(await tasks.create_task("带用量任务", "x"))
        task_id = r["task_id"]
        done = _parse(await tasks.complete_task(
            task_id,
            evidence="test",
            input_tokens=1234,
            output_tokens=987,
            cost=0.12,
            model="qwen2.5:3b",
        ))
        self.assertTrue(done["success"])
        saved = json.loads((DONE_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
        usage = saved["token_usage"]
        self.assertEqual(usage["input_tokens"], 1234)
        self.assertEqual(usage["output_tokens"], 987)
        self.assertEqual(usage["total_tokens"], 2221)
        self.assertEqual(usage["cost"], 0.12)
        self.assertEqual(usage["model"], "qwen2.5:3b")

    async def test_complete_task_omits_empty_token_usage(self):
        r = _parse(await tasks.create_task("无用量任务", "x"))
        task_id = r["task_id"]
        done = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertTrue(done["success"])
        saved = json.loads((DONE_DIR / f"{task_id}.json").read_text(encoding="utf-8"))
        self.assertNotIn("token_usage", saved)

    async def test_pending_skips_done_leftover(self):
        # 模拟旧版本非原子移动的残留：inbox 和 done 同时存在同 ID 任务
        r = _parse(await tasks.create_task("残留任务", "x"))
        task_id = r["task_id"]
        source = INBOX_DIR / f"{task_id}.json"
        task = json.loads(source.read_text(encoding="utf-8"))
        task["status"] = "done"
        safe_write_json(DONE_DIR / f"{task_id}.json", task)

        pending = _parse(await tasks.get_pending_tasks())
        self.assertEqual(pending["count"], 0)  # done 中已存在 → 不再返回
        self.assertFalse(source.exists())  # 残留文件被清理

    async def test_task_context_without_code(self):
        r = _parse(await tasks.create_task("无代码任务", "x"))
        ctx = _parse(await tasks.get_task_context(r["task_id"]))
        self.assertTrue(ctx["success"])
        self.assertFalse(ctx["has_code_context"])
        self.assertEqual(ctx["task"]["id"], r["task_id"])

    @unittest.skipUnless(HAS_CODEGRAPH_INDEX, "CodeGraph 索引未入库（CI 干净环境跳过）")
    async def test_task_context_with_codegraph(self):
        r = _parse(await tasks.create_task(
            "带代码任务", "x",
            project_path=str(REPO_DIR),
            related_symbols="create_task",
        ))
        ctx = _parse(await tasks.get_task_context(r["task_id"]))
        self.assertTrue(ctx["success"])
        self.assertTrue(ctx["has_code_context"])
        first = ctx["code_context"][0]
        self.assertEqual(first["status"], "found")
        self.assertIn("create_task", first["insight"])


class ClaimTest(unittest.IsolatedAsyncioTestCase):
    """v1.7.1：任务显式认领 + 隐式 self-claim（状态机 checkpointing）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")
        os.environ.pop("COLLAB_IDENTITY", None)

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity

    async def test_claim_sets_in_progress_and_owner(self):
        r = _parse(await tasks.create_task("认领任务", "x", assignee="any"))
        task_id = r["task_id"]
        c = _parse(await tasks.claim_task(task_id))
        self.assertTrue(c["success"])
        self.assertEqual(c["claimed_by"], "local")
        self.assertFalse(c["already_claimed"])
        saved = safe_read_json(INBOX_DIR / f"{task_id}.json")
        self.assertEqual(saved["status"], "in_progress")
        self.assertEqual(saved["claimed_by"], "local")
        self.assertIn("claimed_at", saved)

    async def test_claim_idempotent_for_same_owner(self):
        r = _parse(await tasks.create_task("幂等认领", "x", assignee="any"))
        task_id = r["task_id"]
        c1 = _parse(await tasks.claim_task(task_id))
        c2 = _parse(await tasks.claim_task(task_id))
        self.assertTrue(c1["success"])
        self.assertTrue(c2["success"])
        self.assertTrue(c2["already_claimed"])

    async def test_claim_conflict_denied(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r = _parse(await tasks.create_task("冲突任务", "x", assignee="any"))
        task_id = r["task_id"]
        first = _parse(await tasks.claim_task(task_id))
        self.assertTrue(first["success"])

        os.environ["COLLAB_IDENTITY"] = "PC-C"
        second = _parse(await tasks.claim_task(task_id))
        self.assertFalse(second["success"])
        self.assertIn("已被 PC-B 认领", second["error"])

    async def test_claim_denied_for_others_assignee(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r = _parse(await tasks.create_task("指派给C", "x", assignee="PC-C"))
        c = _parse(await tasks.claim_task(r["task_id"]))
        self.assertFalse(c["success"])
        self.assertIn("无权认领", c["error"])

    async def test_complete_requires_claimer(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r = _parse(await tasks.create_task("认领后完成", "x", assignee="any"))
        task_id = r["task_id"]
        await tasks.claim_task(task_id)

        os.environ["COLLAB_IDENTITY"] = "PC-C"
        c = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertFalse(c["success"])
        self.assertIn("认领", c["error"])
        self.assertTrue((INBOX_DIR / f"{task_id}.json").exists())

    async def test_complete_after_claim_by_owner(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r = _parse(await tasks.create_task("认领后完成", "x", assignee="any"))
        task_id = r["task_id"]
        await tasks.claim_task(task_id)
        c = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertTrue(c["success"])
        self.assertTrue((DONE_DIR / f"{task_id}.json").exists())
        done = safe_read_json(DONE_DIR / f"{task_id}.json")
        self.assertEqual(done["completed_by"], "PC-B")
        self.assertEqual(done["claimed_by"], "PC-B")

    async def test_complete_implicit_self_claim(self):
        # 旧版 Agent 不调用 claim_task：complete 隐式认领后完成，不报错
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r = _parse(await tasks.create_task("隐式认领", "x", assignee="PC-B"))
        task_id = r["task_id"]
        c = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertTrue(c["success"])
        done = safe_read_json(DONE_DIR / f"{task_id}.json")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["claimed_by"], "PC-B")

    async def test_pending_hides_task_claimed_by_other(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r1 = _parse(await tasks.create_task("B认领的", "x", assignee="any"))
        await tasks.claim_task(r1["task_id"])
        await tasks.create_task("没人认领的", "y", assignee="any")

        mine = _parse(await tasks.get_pending_tasks())
        self.assertEqual(mine["count"], 2)  # 自己的认领 + 未认领

        os.environ["COLLAB_IDENTITY"] = "PC-C"
        other = _parse(await tasks.get_pending_tasks())
        self.assertEqual(other["count"], 1)
        self.assertEqual(other["tasks"][0]["title"], "没人认领的")

    async def test_claim_fails_on_done_task(self):
        r = _parse(await tasks.create_task("已完成", "x"))
        task_id = r["task_id"]
        await tasks.complete_task(task_id, evidence="test")
        c = _parse(await tasks.claim_task(task_id))
        self.assertFalse(c["success"])
        self.assertIn("已完成", c["error"])

    async def test_force_assign_resets_claim(self):
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        try:
            r = _parse(await tasks.create_task("转移释放认领", "x", assignee="PC-B"))
            task_id = r["task_id"]
            await tasks.claim_task(task_id)

            os.environ["COLLAB_IDENTITY"] = "PC-A"
            f = _parse(await tasks.force_assign(task_id, "PC-C", reason="PC-B 离线"))
            self.assertTrue(f["success"])
            saved_task = safe_read_json(INBOX_DIR / f"{task_id}.json")
            self.assertEqual(saved_task["status"], "pending")
            self.assertNotIn("claimed_by", saved_task)

            os.environ["COLLAB_IDENTITY"] = "PC-C"
            c = _parse(await tasks.claim_task(task_id))
            self.assertTrue(c["success"])
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved


class ClaimNextTaskTest(unittest.IsolatedAsyncioTestCase):
    """MVP-3：自动取下一个任务（优先级/截止时间/并发上限/队列锁）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")
        os.environ.pop("COLLAB_IDENTITY", None)

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity

    async def test_create_task_normalizes_priority(self):
        r = _parse(await tasks.create_task("明确优先级", "x", priority="CRITICAL"))
        saved = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(saved["priority"], "critical")

        r2 = _parse(await tasks.create_task("非法优先级", "x", priority="urgent"))
        saved2 = safe_read_json(INBOX_DIR / f"{r2['task_id']}.json")
        self.assertEqual(saved2["priority"], "medium")

        r3 = _parse(await tasks.create_task("默认优先级", "x"))
        saved3 = safe_read_json(INBOX_DIR / f"{r3['task_id']}.json")
        self.assertEqual(saved3["priority"], "medium")

    async def test_claim_next_picks_highest_priority(self):
        low = _parse(await tasks.create_task("低", "x", priority="low"))
        critical = _parse(await tasks.create_task("关键", "x", priority="critical"))
        high = _parse(await tasks.create_task("高", "x", priority="high"))
        claimed = _parse(await tasks.claim_next_task())

        self.assertTrue(claimed["success"])
        self.assertEqual(claimed["task_id"], critical["task_id"])
        self.assertEqual(claimed["priority"], "critical")
        self.assertNotEqual(claimed["task_id"], low["task_id"])
        self.assertNotEqual(claimed["task_id"], high["task_id"])

    async def test_claim_next_uses_deadline_after_priority(self):
        late = _parse(await tasks.create_task(
            "晚", "x", priority="high", deadline="2030-01-01T00:00:00+00:00"))
        early = _parse(await tasks.create_task(
            "早", "x", priority="high", deadline="2029-01-01T00:00:00+00:00"))
        claimed = _parse(await tasks.claim_next_task())

        self.assertEqual(claimed["task_id"], early["task_id"])
        self.assertEqual(claimed["task_title"], "早")
        self.assertNotEqual(claimed["task_id"], late["task_id"])

    async def test_claim_next_no_duplicate(self):
        low = _parse(await tasks.create_task("低", "x", priority="low"))
        high = _parse(await tasks.create_task("高", "x", priority="high"))
        first = _parse(await tasks.claim_next_task())
        second = _parse(await tasks.claim_next_task())
        idle = _parse(await tasks.claim_next_task())

        self.assertEqual(first["task_id"], high["task_id"])
        self.assertEqual(second["task_id"], low["task_id"])
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertTrue(idle["success"])
        self.assertIsNone(idle["task_id"])
        self.assertEqual(idle["message"], "no_tasks_available")

    async def test_claim_next_no_tasks_and_lock_cleanup(self):
        result = _parse(await tasks.claim_next_task())
        self.assertTrue(result["success"])
        self.assertIsNone(result["task_id"])
        self.assertEqual(result["message"], "no_tasks_available")
        self.assertFalse((COLLAB_DIR / "locks" / "claim_next.json").exists())
        self.assertFalse((COLLAB_DIR / "locks" / ".claim_next.lock").exists())

    async def test_claim_next_lock_cleaned_after_claim(self):
        r = _parse(await tasks.create_task("锁清理", "x", priority="high"))
        claimed = _parse(await tasks.claim_next_task())
        self.assertEqual(claimed["task_id"], r["task_id"])
        self.assertFalse((COLLAB_DIR / "locks" / "claim_next.json").exists())
        self.assertFalse((COLLAB_DIR / "locks" / ".claim_next.lock").exists())
        events = journal.read_task_journal(r["task_id"])
        self.assertEqual(events[-1]["type"], "claimed")
        self.assertEqual(events[-1]["detail"], "claim_next:local")

    async def test_claim_next_respects_max_concurrency(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        await teammates.register_teammate("PC-B", "Python", max_concurrency=1)
        task = _parse(await tasks.create_task("并发一", "x", priority="high"))
        first = _parse(await tasks.claim_next_task())
        second = _parse(await tasks.claim_next_task())

        self.assertEqual(first["task_id"], task["task_id"])
        self.assertTrue(second["success"])
        self.assertIsNone(second["task_id"])
        self.assertTrue(second["at_capacity"])
        self.assertEqual(second["max_concurrency"], 1)

    async def test_claim_next_skips_research_gate(self):
        r = _parse(await tasks.create_task(
            "需调研", "x", priority="high", research_required=True))
        before = _parse(await tasks.claim_next_task())
        self.assertEqual(before["message"], "no_tasks_available")

        await tasks.mark_research_done(r["task_id"], findings="方案A/B/C")
        after = _parse(await tasks.claim_next_task())
        self.assertEqual(after["task_id"], r["task_id"])


class JournalTest(unittest.IsolatedAsyncioTestCase):
    """v1.7.2：任务级事件日志（事件溯源 / 时间旅行）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_task_transitions_write_journal(self):
        r = _parse(await tasks.create_task("日志任务", "x"))
        task_id = r["task_id"]
        await tasks.claim_task(task_id)
        await tasks.complete_task(task_id, evidence="test")

        events = journal.read_task_journal(task_id)
        self.assertEqual(
            [e["type"] for e in events],
            ["created", "claimed", "completed"],
        )
        self.assertTrue(all(e["task_id"] == task_id for e in events))
        self.assertTrue(all("ts" in e and "identity" in e for e in events))

    async def test_journal_history_in_task_context(self):
        r = _parse(await tasks.create_task("回放任务", "x"))
        ctx = _parse(await tasks.get_task_context(r["task_id"]))
        self.assertTrue(ctx["success"])
        self.assertEqual(len(ctx["history"]), 1)
        self.assertEqual(ctx["history"][0]["type"], "created")

    async def test_force_assign_writes_transferred(self):
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            r = _parse(await tasks.create_task("转移日志", "x", assignee="PC-B"))
            await tasks.force_assign(r["task_id"], "PC-C", reason="离线")
            events = journal.read_task_journal(r["task_id"])
            self.assertEqual(events[-1]["type"], "transferred")
            self.assertIn("PC-B → PC-C", events[-1]["detail"])
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    def test_concurrent_appends_no_interleaving(self):
        from concurrent.futures import ThreadPoolExecutor

        task_id = "conc00000001"
        workers, per_worker = 8, 30

        def worker(i):
            for n in range(per_worker):
                if not journal.append_journal_event(
                    task_id, "claimed", detail=f"w{i}-{n}"
                ):
                    raise RuntimeError(f"append failed w{i}-{n}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, range(workers)))

        events = journal.read_task_journal(task_id, limit=100000)
        self.assertEqual(len(events), workers * per_worker)
        details = [e["detail"] for e in events]
        self.assertEqual(len(set(details)), workers * per_worker)
        # v2.7.3：原断言 len(set(ts))==240 在写入快于时钟分辨率的并发环境
        # （如 uv Python 3.11 Windows 构建）会必现失败——并发事件合法地可共享
        # 同一时间戳，时间戳唯一性不是并发正确性契约。真正要验证的是：
        # 无丢失（行数=240）且无交错（detail 全部唯一，锁互斥生效）。

    async def test_corrupt_line_skipped(self):
        r = _parse(await tasks.create_task("损坏行", "x"))
        task_id = r["task_id"]
        jf = COLLAB_DIR / "journal" / f"{task_id}.jsonl"
        jf.write_text(jf.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")

        events = journal.read_task_journal(task_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "created")


class ChatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_chat_roundtrip(self):
        r = _parse(await chat.send_message("PC-A", "大家好，我是 PC-A"))
        self.assertTrue(r["success"])

        hist = _parse(await chat.get_chat_history())
        self.assertEqual(hist["count"], 1)
        self.assertEqual(hist["messages"][0]["sender"], "PC-A")
        self.assertIn("大家好", hist["messages"][0]["content"])
        # v1.3.2 起 timestamp 为本地时间带时区偏移，与文件名一致
        self.assertIn("+", hist["messages"][0]["timestamp"])

    async def test_send_message_with_task_id_and_filter(self):
        # v1.8.2：消息关联任务，聊天可变成任务线程
        r1 = _parse(await chat.send_message("PC-B", "开始做任务", task_id="abc123"))
        r2 = _parse(await chat.send_message("PC-B", "无关联消息"))
        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])

        hist = _parse(await chat.get_chat_history(filter_task_id="abc123"))
        self.assertEqual(hist["count"], 1)
        self.assertEqual(hist["messages"][0]["content"], "开始做任务")
        self.assertEqual(hist["messages"][0]["task_id"], "abc123")

        all_hist = _parse(await chat.get_chat_history())
        self.assertEqual(all_hist["count"], 2)
        no_task = [m for m in all_hist["messages"] if "task_id" not in m]
        self.assertEqual(len(no_task), 1)

        empty = _parse(await chat.get_chat_history(filter_task_id="nope"))
        self.assertEqual(empty["count"], 0)


class TeammateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_register_creates_welcome_task(self):
        r = _parse(await teammates.register_teammate("PC-B", "Docker,前端"))
        self.assertTrue(r["success"])
        self.assertTrue(r["is_new"])
        self.assertIsNotNone(r["welcome_task_id"])
        self.assertTrue((INBOX_DIR / f"{r['welcome_task_id']}.json").exists())

    async def test_reregister_is_not_new(self):
        await teammates.register_teammate("PC-B", "Docker")
        r2 = _parse(await teammates.register_teammate("PC-B", "Docker,GPU"))
        self.assertFalse(r2["is_new"])
        self.assertIsNone(r2["welcome_task_id"])
        self.assertEqual(r2["total_teammates"], 1)

    async def test_list_teammates(self):
        await teammates.register_teammate("PC-B", "Docker")
        lst = _parse(await teammates.list_teammates())
        self.assertEqual(lst["total"], 1)
        self.assertEqual(lst["teammates"][0]["name"], "PC-B")
        self.assertEqual(lst["teammates"][0]["capabilities"], "Docker")

    async def test_register_with_structured_skills(self):
        r = _parse(await teammates.register_teammate(
            "PC-B",
            capabilities="Python 后端与代码审查",
            skills="code-review:python,testing:unit",
            max_concurrency=3,
        ))
        self.assertTrue(r["success"])
        lst = _parse(await teammates.list_teammates())
        entry = lst["teammates"][0]
        self.assertEqual(entry["skills"], ["code-review:python", "testing:unit"])
        self.assertEqual(entry["max_concurrency"], 3)
        # capabilities 描述保留作为语义兜底
        self.assertEqual(entry["capabilities"], "Python 后端与代码审查")

    async def test_register_without_skills_keeps_backward_compat(self):
        r = _parse(await teammates.register_teammate("PC-B", "Docker"))
        self.assertTrue(r["success"])
        lst = _parse(await teammates.list_teammates())
        entry = lst["teammates"][0]
        self.assertNotIn("skills", entry)
        self.assertNotIn("max_concurrency", entry)
        self.assertEqual(entry["capabilities"], "Docker")

    async def test_list_empty(self):
        lst = _parse(await teammates.list_teammates())
        self.assertEqual(lst["teammates"], [])
        self.assertIn("暂无注册队友", lst["message"])


class StatusAndHealthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_status_counts(self):
        await tasks.create_task("t", "x")
        await chat.send_message("PC-A", "hi")
        s = _parse(await status.get_collab_status())
        self.assertTrue(s["success"])
        self.assertEqual(s["status"]["pending_tasks"], 1)
        self.assertEqual(s["status"]["completed_tasks"], 0)
        self.assertEqual(s["status"]["chat_messages"], 1)

    async def test_task_metrics(self):
        # v1.8.3：生命周期指标（纯读）
        r1 = _parse(await tasks.create_task("任务A", "x"))
        await tasks.claim_task(r1["task_id"])
        await tasks.complete_task(r1["task_id"], evidence="ok")
        r2 = _parse(await tasks.create_task("任务B", "y"))
        await tasks.complete_task(r2["task_id"], evidence="test")

        m = _parse(await status.get_task_metrics())
        self.assertTrue(m["success"])
        self.assertEqual(m["total_completed"], 2)
        self.assertIsNotNone(m["avg_cycle_minutes"])
        self.assertIsNotNone(m["avg_active_minutes"])  # 任务A 有 claimed_at
        self.assertTrue(m["by_teammate"])
        names = {t["name"] for t in m["by_teammate"]}
        self.assertIn("unknown", names)  # 未设身份时 completed_by=unknown
        self.assertLessEqual(len(m["recent"]), 2)
        with_cycle = [r for r in m["recent"] if "cycle_minutes" in r]
        self.assertEqual(len(with_cycle), 2)

    async def test_task_metrics_empty(self):
        m = _parse(await status.get_task_metrics())
        self.assertTrue(m["success"])
        self.assertEqual(m["total_completed"], 0)
        self.assertIsNone(m["avg_cycle_minutes"])
        self.assertEqual(m["by_teammate"], [])
        self.assertEqual(m["by_model"], [])

    async def test_task_metrics_token_aggregation(self):
        r1 = _parse(await tasks.create_task("任务A", "x"))
        await tasks.complete_task(r1["task_id"], evidence="ok", total_tokens=100, cost=0.5)
        r2 = _parse(await tasks.create_task("任务B", "y"))
        await tasks.complete_task(r2["task_id"], evidence="test")

        m = _parse(await status.get_task_metrics())
        self.assertTrue(m["success"])
        self.assertEqual(m["total_tokens"], 100)
        self.assertEqual(m["total_cost"], 0.5)
        self.assertEqual(m["requests_with_usage"], 1)

        by_unknown = next(t for t in m["by_teammate"] if t["name"] == "unknown")
        self.assertEqual(by_unknown["total_tokens"], 100)
        self.assertEqual(by_unknown["total_cost"], 0.5)
        self.assertEqual(by_unknown["requests_with_usage"], 1)
        self.assertEqual(by_unknown["avg_tokens_per_request"], 100.0)
        self.assertEqual(by_unknown["avg_cost_per_request"], 0.5)

    async def test_task_metrics_by_model_aggregation(self):
        r1 = _parse(await tasks.create_task("任务A", "x"))
        await tasks.complete_task(
            r1["task_id"],
            evidence="ok",
            total_tokens=100,
            cost=0.5,
            model="qwen2.5:3b",
        )
        r2 = _parse(await tasks.create_task("任务B", "y"))
        await tasks.complete_task(
            r2["task_id"],
            evidence="ok",
            total_tokens=300,
            cost=1.2,
            model="gpt-4o-mini",
        )
        r3 = _parse(await tasks.create_task("任务C", "z"))
        await tasks.complete_task(r3["task_id"], evidence="ok", total_tokens=50)

        m = _parse(await status.get_task_metrics())
        self.assertTrue(m["success"])
        by_model = {item["model"]: item for item in m["by_model"]}
        self.assertEqual(set(by_model), {"qwen2.5:3b", "gpt-4o-mini"})
        self.assertEqual(by_model["qwen2.5:3b"]["total_tokens"], 100)
        self.assertEqual(by_model["qwen2.5:3b"]["total_cost"], 0.5)
        self.assertEqual(by_model["qwen2.5:3b"]["requests_with_usage"], 1)
        self.assertEqual(by_model["qwen2.5:3b"]["avg_tokens_per_request"], 100.0)
        self.assertEqual(by_model["gpt-4o-mini"]["total_tokens"], 300)
        self.assertEqual(by_model["gpt-4o-mini"]["total_cost"], 1.2)
        self.assertEqual(m["total_tokens"], 450)

    async def test_health_ok(self):
        h = _parse(await health.health_check())
        self.assertTrue(h["success"])
        self.assertTrue(h["healthy"])
        self.assertEqual(h["checks"]["directories"]["inbox"], "✅ 可读写")


class CodeGraphTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset_collab_dir()

    @unittest.skipUnless(HAS_CODEGRAPH_INDEX, "CodeGraph 索引未入库（CI 干净环境跳过）")
    async def test_query_codegraph(self):
        r = _parse(await codegraph.query_codegraph(
            "create_task", project_path=str(REPO_DIR)
        ))
        self.assertTrue(r["success"])
        self.assertGreater(r["symbol_count"], 0)
        self.assertIn("create_task", r["result"])

    async def test_query_codegraph_missing_index(self):
        # 测试目录没有 .codegraph → 应返回明确错误
        r = _parse(await codegraph.query_codegraph("create_task"))
        self.assertFalse(r["success"])
        self.assertIn("CodeGraph", r["error"])

    @unittest.skipUnless(HAS_CODEGRAPH_INDEX, "CodeGraph 索引未入库（CI 干净环境跳过）")
    async def test_query_codegraph_twice(self):
        # 第二次查询应命中缓存副本，同样成功（v1.5.0 每进程独立缓存）
        r1 = _parse(await codegraph.query_codegraph(
            "create_task", project_path=str(REPO_DIR)))
        self.assertTrue(r1["success"])
        r2 = _parse(await codegraph.query_codegraph(
            "create_task", project_path=str(REPO_DIR)))
        self.assertTrue(r2["success"])
        self.assertEqual(r2["symbol_count"], r1["symbol_count"])

    @unittest.skipUnless(HAS_CODEGRAPH_INDEX, "CodeGraph 索引未入库（CI 干净环境跳过）")
    async def test_codegraph_cache_rebuild_on_corruption(self):
        # 往本进程缓存副本写入垃圾 → 触发 "not a database" 重建恢复路径
        cache_dir = Path(tempfile.gettempdir()) / "codegraph_cache"
        project_hash = hashlib.sha1(
            str(Path(REPO_DIR).resolve()).encode("utf-8")
        ).hexdigest()[:12]
        cache = cache_dir / f"cg_{project_hash}_{os.getpid()}.db"
        cache.write_bytes(b"garbage-not-a-sqlite-db")

        r = _parse(await codegraph.query_codegraph(
            "create_task", project_path=str(REPO_DIR)))
        self.assertTrue(r["success"])
        self.assertGreater(r["symbol_count"], 0)


class _IdentityMixin:
    """身份隔离测试的公共 setUp/tearDown（保存并恢复环境变量）。"""

    _IDENTITY = ""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")
        self._saved_role = os.environ.get("COLLAB_ROLE")
        os.environ["COLLAB_IDENTITY"] = self._IDENTITY
        os.environ.pop("COLLAB_ROLE", None)

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity
        if self._saved_role is None:
            os.environ.pop("COLLAB_ROLE", None)
        else:
            os.environ["COLLAB_ROLE"] = self._saved_role


class IdentityIsolationTest(_IdentityMixin, unittest.IsolatedAsyncioTestCase):
    """第 7 项：基于 SSH 身份的 assignee 权限隔离（v1.5.0）。"""

    _IDENTITY = "PC-B"

    async def test_teammate_only_sees_own_and_any(self):
        await tasks.create_task("给B", "x", assignee="PC-B")
        await tasks.create_task("给C", "y", assignee="PC-C")
        await tasks.create_task("公开", "z", assignee="any")

        r = _parse(await tasks.get_pending_tasks())
        self.assertEqual(r["count"], 2)
        self.assertEqual({t["title"] for t in r["tasks"]}, {"给B", "公开"})
        self.assertEqual(r["identity"], "PC-B")
        self.assertEqual(r["role"], "teammate")

    async def test_teammate_cannot_query_others_assignee(self):
        await tasks.create_task("给C", "y", assignee="PC-C")
        r = _parse(await tasks.get_pending_tasks(assignee="PC-C"))
        self.assertFalse(r["success"])
        self.assertIn("无权限", r["error"])

    async def test_teammate_cannot_complete_others_task(self):
        r = _parse(await tasks.create_task("给C", "y", assignee="PC-C"))
        task_id = r["task_id"]
        r2 = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertFalse(r2["success"])
        self.assertTrue((INBOX_DIR / f"{task_id}.json").exists())

    async def test_teammate_cannot_read_others_context(self):
        r = _parse(await tasks.create_task("给C", "y", assignee="PC-C"))
        r2 = _parse(await tasks.get_task_context(r["task_id"]))
        self.assertFalse(r2["success"])

    async def test_teammate_can_complete_own_task(self):
        r = _parse(await tasks.create_task("给我", "x", assignee="PC-B"))
        task_id = r["task_id"]
        r2 = _parse(await tasks.complete_task(task_id, evidence="test"))
        self.assertTrue(r2["success"])
        self.assertEqual(r2["completed_by"], "PC-B")
        self.assertTrue((DONE_DIR / f"{task_id}.json").exists())

    async def test_register_name_must_match_identity(self):
        r = _parse(await teammates.register_teammate("PC-B", "PowerShell"))
        self.assertTrue(r["success"])
        r2 = _parse(await teammates.register_teammate("PC-C", "x"))
        self.assertFalse(r2["success"])
        self.assertIn("身份不匹配", r2["error"])


class HubIdentityTest(_IdentityMixin, unittest.IsolatedAsyncioTestCase):
    """中枢身份（PC-A）保留全量权限（v1.5.0）。"""

    _IDENTITY = "PC-A"

    async def test_hub_sees_and_completes_all_tasks(self):
        await tasks.create_task("给B", "x", assignee="PC-B")
        await tasks.create_task("给C", "y", assignee="PC-C")

        r = _parse(await tasks.get_pending_tasks())
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["role"], "hub")

        first_id = r["tasks"][0]["id"]
        r2 = _parse(await tasks.complete_task(first_id, evidence="test"))
        self.assertTrue(r2["success"])


class HeartbeatTest(_IdentityMixin, unittest.IsolatedAsyncioTestCase):
    """MVP-1：heartbeat 同步队友 last_seen，但不新建未注册队友。"""

    _IDENTITY = "PC-B"

    async def test_heartbeat_updates_last_seen(self):
        await teammates.register_teammate("PC-B", "PowerShell")
        registry_file = COLLAB_DIR / "teammates.json"
        registry = safe_read_json(registry_file) or {}
        registry["PC-B"]["last_seen"] = "2000-01-01T00:00:00+00:00"
        safe_write_json(registry_file, registry)

        r = _parse(await tasks.create_task("心跳任务", "x", assignee="PC-B"))
        await tasks.claim_task(r["task_id"])
        hb = _parse(await tasks.heartbeat())

        self.assertTrue(hb["success"])
        self.assertTrue(hb["last_seen_updated"])
        self.assertEqual(hb["identity"], "PC-B")
        after = safe_read_json(registry_file) or {}
        self.assertNotEqual(after["PC-B"]["last_seen"], "2000-01-01T00:00:00+00:00")

    async def test_heartbeat_does_not_create_unregistered_teammate(self):
        hb = _parse(await tasks.heartbeat())
        self.assertTrue(hb["success"])
        self.assertFalse(hb["last_seen_updated"])
        self.assertFalse((COLLAB_DIR / "teammates.json").exists())


class RequireIdentityTest(unittest.IsolatedAsyncioTestCase):
    """REQUIRE_IDENTITY=1 时，未绑定身份（COLLAB_IDENTITY 为空）的会话被拒绝。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")
        self._saved_require = os.environ.get("REQUIRE_IDENTITY")
        os.environ.pop("COLLAB_IDENTITY", None)
        os.environ["REQUIRE_IDENTITY"] = "1"

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity
        if self._saved_require is None:
            os.environ.pop("REQUIRE_IDENTITY", None)
        else:
            os.environ["REQUIRE_IDENTITY"] = self._saved_require

    async def test_unidentified_session_denied_for_private_tools(self):
        r = _parse(await tasks.get_pending_tasks())
        self.assertFalse(r["success"])
        self.assertIn("需要调用者身份", r["error"])

        r2 = _parse(await tasks.create_task("测试", "x"))
        self.assertTrue(r2["success"])

        r3 = _parse(await tasks.complete_task(r2["task_id"], evidence="test"))
        self.assertFalse(r3["success"])

        r4 = _parse(await teammates.register_teammate("PC-C", "x"))
        self.assertFalse(r4["success"])

    async def test_teammate_can_query_any_assignee(self):
        # M2 修复：队友可以查看 assignee=any 的任务
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        await tasks.create_task("公开", "z", assignee="any")
        r = _parse(await tasks.get_pending_tasks(assignee="any"))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    """团队记忆：remember_fact / search_memory（v2.2.0）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity

    async def test_remember_roundtrip(self):
        r = _parse(await memory.remember_fact(
            "部署约定",
            "VM server.py 改动后需 pkill 重启",
            tags="运维,部署,坑",
        ))
        self.assertTrue(r["success"])
        self.assertEqual(r["created_by"], "PC-A")
        files = list(MEMORY_DIR.glob("*.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("部署约定", text)
        self.assertIn("pkill", text)

    async def test_search_keyword_and_tags(self):
        await memory.remember_fact("路由约定", "hub-vm 走 8022 转发", tags="运维,ssh")
        await memory.remember_fact("记账决策", "采用复式记账", tags="架构,决策")
        r = _parse(await memory.search_memory(query="8022"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["title"], "路由约定")
        r2 = _parse(await memory.search_memory(tags="架构"))
        self.assertEqual(r2["count"], 1)
        self.assertEqual(r2["results"][0]["title"], "记账决策")

    async def test_search_multi_term_and_order(self):
        await memory.remember_fact("a", "alpha beta gamma")
        await memory.remember_fact("b", "alpha only")
        r = _parse(await memory.search_memory(query="alpha beta"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["title"], "a")

    async def test_search_limit_and_empty_query(self):
        for i in range(3):
            await memory.remember_fact(f"事实{i}", f"内容{i}")
        r = _parse(await memory.search_memory(limit=2))
        self.assertEqual(r["count"], 2)
        titles = [x["title"] for x in r["results"]]
        self.assertEqual(titles, ["事实2", "事实1"])  # 新在前

    async def test_remember_validation(self):
        r = _parse(await memory.remember_fact("", "x"))
        self.assertFalse(r["success"])
        r2 = _parse(await memory.remember_fact("t", ""))
        self.assertFalse(r2["success"])

    async def test_identity_guard_under_require_identity(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await memory.remember_fact("t", "x"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
            r2 = _parse(await memory.search_memory(query="x"))
            self.assertFalse(r2["success"])
            os.environ["COLLAB_IDENTITY"] = "PC-B"
            r3 = _parse(await memory.remember_fact("t", "x"))
            self.assertTrue(r3["success"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved

    async def test_corrupted_meta_types_tolerated(self):
        # M1 回归：tags 为字符串 → 归一化后标签检索仍命中
        bad_str = MEMORY_DIR / "20260804-000001_bad_str001.md"
        bad_str.write_text(
            '---\n{"id": "bad1", "title": "字符串标签", "tags": "运维,部署", '
            '"created_by": "x", "created_at": "2026-08-04T00:00:01+00:00", "source": ""}\n'
            "---\n\n内容\n",
            encoding="utf-8",
        )
        r = _parse(await memory.search_memory(tags="运维"))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)
        # tags 为数字 → 检索不崩溃，作为无标签处理
        bad_num = MEMORY_DIR / "20260804-000002_bad_num001.md"
        bad_num.write_text(
            '---\n{"id": "bad2", "title": "数字标签", "tags": 123, '
            '"created_by": "x", "created_at": "2026-08-04T00:00:02+00:00", "source": ""}\n'
            "---\n\n内容\n",
            encoding="utf-8",
        )
        r2 = _parse(await memory.search_memory(query="数字标签"))
        self.assertTrue(r2["success"])
        self.assertEqual(r2["count"], 1)

    async def test_query_matches_tags(self):
        # L3 改进：标签词也进入 query 命中范围
        await memory.remember_fact("决策记录", "正文不含标签词", tags="架构,决策")
        r = _parse(await memory.search_memory(query="决策"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["title"], "决策记录")


class SkillRegistryTest(unittest.IsolatedAsyncioTestCase):
    """v2.5.0：技能注册表 list_skills / find_skill / register_skill。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        import shutil

        if SKILLS_DIR.is_dir():
            for f in SKILLS_DIR.glob("*.md"):
                f.unlink()
        repo_skills = REPO_DIR / "skills"
        if repo_skills.is_dir():
            shutil.copytree(repo_skills, SKILLS_DIR, dirs_exist_ok=True)

    async def test_seed_skills_present(self):
        r = _parse(await skills.list_skills())
        self.assertTrue(r["success"])
        names = {s["name"] for s in r["skills"]}
        self.assertIn("code-review", names)
        self.assertIn("ocr-delegate-review", names)
        self.assertIn("doc-review", names)
        self.assertGreaterEqual(r["count"], 10)

    async def test_find_skill_cjk_wording_regression(self):
        # v2.5.1（PC-B L1）："代码审查"措辞应同时命中 code-review 与 ocr-delegate-review
        r = _parse(await skills.find_skill(query="代码审查"))
        names = {x["name"] for x in r["results"]}
        self.assertIn("code-review", names)
        self.assertIn("ocr-delegate-review", names)

    async def test_register_roundtrip(self):
        r = _parse(await skills.register_skill(
            "fmt-check", "检查代码格式",
            capability_tags="quality:format", entry="doc:progress/ocr-usage.md",
            body="用法说明正文"))
        self.assertTrue(r["success"])
        f = SKILLS_DIR / "fmt-check.md"
        self.assertTrue(f.exists())
        text = f.read_text(encoding="utf-8")
        self.assertIn("fmt-check", text)
        r2 = _parse(await skills.find_skill(query="格式"))
        self.assertTrue(r2["success"])
        self.assertEqual(r2["results"][0]["name"], "fmt-check")
        self.assertEqual(r2["results"][0]["entry"], "doc:progress/ocr-usage.md")

    async def test_register_validation(self):
        r = _parse(await skills.register_skill("", "x"))
        self.assertFalse(r["success"])
        r2 = _parse(await skills.register_skill("Bad Name", "x"))
        self.assertFalse(r2["success"])
        r3 = _parse(await skills.register_skill("ok-name", ""))
        self.assertFalse(r3["success"])
        r4 = _parse(await skills.register_skill("ok-name", "x", entry="http://bad"))
        self.assertFalse(r4["success"])
        r5 = _parse(await skills.register_skill("../evil", "x"))
        self.assertFalse(r5["success"])
        r6 = _parse(await skills.register_skill("ok-name", "x", scope="nope"))
        self.assertFalse(r6["success"])

    async def test_register_update_keeps_created(self):
        r = _parse(await skills.register_skill("abc-skill", "v1", capability_tags="a:b"))
        self.assertTrue(r["success"])
        r2 = _parse(await skills.register_skill("abc-skill", "v2", capability_tags="a:b,c:d"))
        self.assertTrue(r2["success"])
        self.assertEqual(r2["action"], "更新")
        r3 = _parse(await skills.find_skill(query="v2"))
        self.assertEqual(r3["results"][0]["name"], "abc-skill")
        self.assertEqual(r3["results"][0]["capability_tags"], ["a:b", "c:d"])

    async def test_find_by_tag_and_query_and(self):
        await skills.register_skill("skill-a", "alpha beta", capability_tags="x:y")
        await skills.register_skill("skill-b", "alpha only", capability_tags="x:z")
        r = _parse(await skills.find_skill(query="alpha beta"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["name"], "skill-a")
        r2 = _parse(await skills.find_skill(tags="x:z"))
        self.assertEqual(r2["count"], 1)
        self.assertEqual(r2["results"][0]["name"], "skill-b")

    async def test_list_filters(self):
        await skills.register_skill("draft-skill", "draft test", status="draft")
        r = _parse(await skills.list_skills(status="draft"))
        self.assertEqual(r["count"], 1)
        r2 = _parse(await skills.list_skills(tag="collab:lock"))
        names = {s["name"] for s in r2["skills"]}
        self.assertIn("project-lock", names)

    async def test_corrupt_skill_file_skipped(self):
        bad = SKILLS_DIR / "broken-skill.md"
        bad.write_text("not frontmatter\n", encoding="utf-8")
        r = _parse(await skills.list_skills())
        self.assertTrue(r["success"])
        names = {s["name"] for s in r["skills"]}
        self.assertNotIn("broken-skill", names)

    async def test_identity_guard_under_require_identity(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await skills.register_skill("t", "x"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
            r2 = _parse(await skills.list_skills())
            self.assertFalse(r2["success"])
            os.environ["COLLAB_IDENTITY"] = "PC-B"
            r3 = _parse(await skills.register_skill("t", "x"))
            self.assertTrue(r3["success"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved

class DocumentIngestTest(unittest.IsolatedAsyncioTestCase):
    """v2.6.0：文档摄入 add_document / list_documents。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        if DOCS_DIR.is_dir():
            # 递归清理：documents/ 下任何子目录残留（如 web/）都不能污染计数断言
            for f in list(DOCS_DIR.rglob("*.md")):
                f.unlink()
        else:
            DOCS_DIR.mkdir(parents=True, exist_ok=True)

    async def test_md_roundtrip(self):
        src = COLLAB_DIR / "sample.md"
        src.write_text("# 标题\n\n正文内容 hello doc\n", encoding="utf-8")
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        out = DOCS_DIR / "sample.md"
        self.assertTrue(out.exists())
        text = out.read_text(encoding="utf-8")
        self.assertIn("正文内容", text)
        self.assertIn("hello doc", text)
        self.assertIn('"format": "md"', text)

    async def test_shared_relative_source(self):
        src = COLLAB_DIR / "note.txt"
        src.write_text("共享相对路径测试", encoding="utf-8")
        rel = str(src.relative_to(COLLAB_DIR.parent))
        r = _parse(await documents.add_document(rel))
        self.assertTrue(r["success"], r)
        self.assertTrue((DOCS_DIR / "note.md").exists())

    async def test_docx_extraction(self):
        from docx import Document
        src = COLLAB_DIR / "sample.docx"
        d = Document()
        d.add_heading("文档标题", level=1)
        d.add_paragraph("第一段正文")
        table = d.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "A"
        table.cell(0, 1).text = "B"
        d.save(str(src))
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        text = (DOCS_DIR / "sample.md").read_text(encoding="utf-8")
        self.assertIn("文档标题", text)
        self.assertIn("第一段正文", text)
        self.assertIn("| A | B |", text)

    async def test_html_extraction(self):
        src = COLLAB_DIR / "page.html"
        src.write_text(
            "<html><body><h1>大标题</h1><p>段落文本</p>"
            "<ul><li>项目一</li></ul></body></html>",
            encoding="utf-8",
        )
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        text = (DOCS_DIR / "page.md").read_text(encoding="utf-8")
        self.assertIn("# 大标题", text)
        self.assertIn("段落文本", text)
        self.assertIn("- 项目一", text)

    async def test_pdf_broken_clean_error(self):
        src = COLLAB_DIR / "broken.pdf"
        src.write_bytes(b"%PDF-1.4\nnot really a pdf\n%%EOF")
        r = _parse(await documents.add_document(str(src)))
        self.assertFalse(r["success"])
        self.assertIn("pdf", r["error"])

    async def test_pdf_magic_check_rejects_fake_pdf(self):
        # v2.6.1（本机 Claude P1）：非 %PDF- 文件头伪装 .pdf 必须拒绝
        src = COLLAB_DIR / "fake.pdf"
        src.write_text("hello this is not a pdf", encoding="utf-8")
        r = _parse(await documents.add_document(str(src)))
        self.assertFalse(r["success"])
        self.assertIn("有效的 PDF", r["error"])

    async def test_html_ol_and_code_no_duplicate(self):
        # v2.6.1（本机 Claude P2）：有序列表保留编号；<pre><code> 不重复输出
        src = COLLAB_DIR / "list.html"
        src.write_text(
            "<html><body><ol><li>第一步</li><li>第二步</li></ol>"
            "<pre><code>def f(): return 1</code></pre></body></html>",
            encoding="utf-8",
        )
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        text = (DOCS_DIR / "list.md").read_text(encoding="utf-8")
        self.assertIn("1. 第一步", text)
        self.assertIn("2. 第二步", text)
        self.assertEqual(text.count("```"), 2)  # 仅一个围栏代码块

    async def test_dedup_skip_duplicates(self):
        src = COLLAB_DIR / "dup.txt"
        src.write_text("去重测试内容", encoding="utf-8")
        r1 = _parse(await documents.add_document(str(src)))
        self.assertTrue(r1["success"], r1)
        r2 = _parse(await documents.add_document(str(src), skip_duplicates=True))
        self.assertTrue(r2["success"], r2)
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(r1["path"], r2["path"])

    async def test_validation_and_path_safety(self):
        r = _parse(await documents.add_document(""))
        self.assertFalse(r["success"])
        r2 = _parse(await documents.add_document(str(COLLAB_DIR / "nope.md")))
        self.assertFalse(r2["success"])
        src = COLLAB_DIR / "a.txt"
        src.write_text("x", encoding="utf-8")
        r3 = _parse(await documents.add_document(str(src), target_dir="../escape"))
        self.assertFalse(r3["success"])

    async def test_slugified_filename(self):
        src = COLLAB_DIR / "My Report (final)!.txt"
        src.write_text("内容", encoding="utf-8")
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        self.assertTrue((DOCS_DIR / "my-report-final.md").exists())

    async def test_nfkc_normalizes_ligature(self):
        # v2.7.1（本机 Claude L1）：PDF 提取的 Unicode 连字 ﬁ (U+FB01)
        # 摄入时做 NFKC 规范化，避免检索词因字形不匹配而漏命中
        src = COLLAB_DIR / "lig.txt"
        src.write_text("Arti\ufb01cial intelligence overview\n", encoding="utf-8")
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        text = (DOCS_DIR / "lig.md").read_text(encoding="utf-8")
        self.assertIn("Artificial", text)
        self.assertNotIn("\ufb01", text)

    async def test_list_documents(self):
        src = COLLAB_DIR / "doc-one.txt"
        src.write_text("一", encoding="utf-8")
        await documents.add_document(str(src))
        r = _parse(await documents.list_documents())
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["documents"][0]["source"], "doc-one.txt")

    async def test_identity_guard_under_require_identity(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await documents.add_document("x.txt"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
            r2 = _parse(await documents.list_documents())
            self.assertFalse(r2["success"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved


class SearchDocumentsTest(unittest.IsolatedAsyncioTestCase):
    """v2.7.0：文档全文检索 search_documents（SQLite FTS5）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        # 清空测试共享根下的文档/自定义目录（与 DocumentIngestTest 共享 temp/documents）
        for d in (DOCS_DIR, COLLAB_DIR.parent / "notes"):
            if d.is_dir():
                for f in list(d.rglob("*.md")):
                    f.unlink()
        # 重置 FTS 索引缓存，防止跨测试的签名缓存导致 flaky test
        search.reset_state()

    async def _ingest(self, name: str, body: str, target_dir: str = "") -> dict:
        src = COLLAB_DIR / name
        src.write_text(body, encoding="utf-8")
        if target_dir:
            r = _parse(await documents.add_document(str(src), target_dir=target_dir))
        else:
            r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        return r

    async def test_search_hits_ingested_doc(self):
        await self._ingest(
            "quantum.md",
            "# 量子计算综述\n\n正文：量子纠错和量子加速，hello world\n",
        )
        res = _parse(await search.search_documents("hello"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 1)
        hit = res["results"][0]
        self.assertEqual(hit["filename"], "quantum.md")
        self.assertEqual(hit["title"], "量子计算综述")
        self.assertIn("hello", hit["snippet"])

    async def test_cjk_long_and_short_term(self):
        await self._ingest("cjk.md", "# CJK\n\n量子纠错和量子加速\n")
        # 4 字符中文词走 trigram FTS
        r1 = _parse(await search.search_documents("量子纠错"))
        self.assertTrue(r1["success"], r1)
        self.assertEqual(r1["count"], 1)
        # 2 字符中文词走 LIKE 兜底（trigram 不匹配短词）
        r2 = _parse(await search.search_documents("量子"))
        self.assertTrue(r2["success"], r2)
        self.assertEqual(r2["count"], 1)
        self.assertIn("量子", r2["results"][0]["snippet"])

    async def test_and_semantics(self):
        await self._ingest("and.md", "alpha beta 内容\n")
        r1 = _parse(await search.search_documents("alpha beta"))
        self.assertEqual(r1["count"], 1)
        r2 = _parse(await search.search_documents("alpha gamma"))
        self.assertTrue(r2["success"], r2)
        self.assertEqual(r2["count"], 0)

    # 检索加固（2026-08-07 uv311 全量复现）：扫描期间外部进程删除目录
    # （%TEMP% 下 qoder-sdk-auth-* 瞬态目录）→ rglob 抛 FileNotFoundError → 不崩溃降级
    async def test_rglob_vanished_dir_tolerant(self):
        # v3.30.1：search 改为按 scope 建索引，默认 documents 在 setUp 后为空；
        # 先放入一个真实文档，保证下面的 flaky rglob 真的走到“看到一个文件后
        # 目录消失”的分支，而不是因为空目录跳过异常路径。
        await self._ingest("victim.md", "量子纠错 rglob-token\n")
        real_rglob = Path.rglob

        def flaky_rglob(self, pattern):
            it = real_rglob(self, pattern)

            def gen():
                try:
                    next(it)
                except StopIteration:
                    return
                raise FileNotFoundError(2, "系统找不到指定的路径。")

            return gen()

        with patch("pathlib.Path.rglob", new=flaky_rglob):
            r = _parse(await search.search_documents("量子纠错"))
        self.assertTrue(r["success"], r)  # 容忍扫描中断，不抛异常

    async def test_target_dir_filter(self):
        await self._ingest("a.md", "sharedtoken 文档一\n")
        await self._ingest("b.md", "sharedtoken 文档二\n", target_dir="notes")
        r_docs = _parse(await search.search_documents("sharedtoken", target_dir="documents"))
        self.assertEqual(r_docs["count"], 1)
        self.assertTrue(r_docs["results"][0]["path"].startswith("documents/"))
        r_notes = _parse(await search.search_documents("sharedtoken", target_dir="notes"))
        self.assertEqual(r_notes["count"], 1)
        self.assertTrue(r_notes["results"][0]["path"].startswith("notes/"))
        # 默认 scope 是 documents/
        r_default = _parse(await search.search_documents("sharedtoken"))
        self.assertEqual(r_default["count"], 1)

    async def test_index_auto_refresh_on_new_doc(self):
        await self._ingest("one.md", "refreshtoken 第一条\n")
        r1 = _parse(await search.search_documents("refreshtoken"))
        self.assertEqual(r1["count"], 1)
        await self._ingest("two.md", "refreshtoken 第二条\n")
        r2 = _parse(await search.search_documents("refreshtoken"))
        self.assertEqual(r2["count"], 2)

    async def test_limit_clamp(self):
        await self._ingest("l1.md", "duptoken 内容一\n")
        await self._ingest("l2.md", "duptoken 内容二\n")
        r1 = _parse(await search.search_documents("duptoken", limit=1))
        self.assertEqual(r1["count"], 1)
        r_bad = _parse(await search.search_documents("duptoken", limit=0))
        self.assertEqual(r_bad["count"], 1)  # 夹取到下限 1
        r_huge = _parse(await search.search_documents("duptoken", limit=5000))
        self.assertEqual(r_huge["count"], 2)  # 夹取到上限 100

    async def test_empty_query_fails(self):
        r = _parse(await search.search_documents(""))
        self.assertFalse(r["success"])
        self.assertIn("query", r["error"])

    async def test_path_traversal_rejected(self):
        r = _parse(await search.search_documents("x", target_dir="../escape"))
        self.assertFalse(r["success"])
        self.assertIn("共享目录范围", r["error"])
        # 共享根外的绝对路径（Windows 用 C:\Windows 不跨平台——Linux 上是相对路径）。
        # 用仓库目录的兄弟路径构造"必然在测试共享根外"的绝对路径。
        outside = str(REPO_DIR / "escape")
        r2 = _parse(await search.search_documents("x", target_dir=outside))
        self.assertFalse(r2["success"])

    # v3.20.2：损坏 JSON 元数据与 documents/ 外无头 md 仍跳过
    # （原 no-meta 断言反转：documents/ 内无头普通 md 现在应被索引）
    async def test_broken_meta_and_non_documents_plain_md_skipped(self):
        (DOCS_DIR / "broken-meta.md").write_text(
            "---\n{not valid json\n---\n正文 tokenbroken\n", encoding="utf-8"
        )
        notes_dir = COLLAB_DIR.parent / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / "no-meta.md").write_text(
            "documents 外无头普通 md 不应被索引 tokennotes\n", encoding="utf-8"
        )
        await self._ingest("ok.md", "正常文档 tokenok 内容\n")
        r = _parse(await search.search_documents("tokenok"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["filename"], "ok.md")
        r_broken = _parse(await search.search_documents("tokenbroken"))
        self.assertEqual(r_broken["count"], 0)
        r_notes = _parse(await search.search_documents("tokennotes"))
        self.assertEqual(r_notes["count"], 0)

    # v3.20.2：documents/ 下无头普通 Markdown 入索引（标题取首个 # 标题）
    async def test_plain_md_in_documents_indexed(self):
        (DOCS_DIR / "plain.md").write_text(
            "# 手写调研报告\n\n正文包含 tokenplain 关键词\n", encoding="utf-8"
        )
        r = _parse(await search.search_documents("tokenplain"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)
        hit = r["results"][0]
        self.assertEqual(hit["filename"], "plain.md")
        self.assertEqual(hit["title"], "手写调研报告")
        self.assertEqual(hit["format"], "md")
        self.assertIn("tokenplain", hit["snippet"])

    # v3.20.2：无 # 标题时标题回退文件名
    async def test_plain_md_title_fallback(self):
        (DOCS_DIR / "report-a.md").write_text(
            "没有标题的无头文档 tokenfallback\n", encoding="utf-8"
        )
        r = _parse(await search.search_documents("tokenfallback"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["title"], "report-a.md")

    # v3.20.2：YAML frontmatter（--- 开头但非 JSON）维持跳过，不索引不崩溃
    async def test_yaml_frontmatter_skipped(self):
        (DOCS_DIR / "yaml.md").write_text(
            "---\ntags:\n  - demo\n---\n正文 tokenyaml\n", encoding="utf-8"
        )
        r = _parse(await search.search_documents("tokenyaml"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 0)

    async def test_nfkc_query_matches_ligature_source(self):
        # v2.7.1（本机 Claude L1）：摄入侧 NFKC 后，普通拼写查询命中连字源文档
        await self._ingest("lig2.md", "Arti\ufb01cial intelligence survey\n")
        r = _parse(await search.search_documents("artificial"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["filename"], "lig2.md")

    async def test_identity_guard_under_require_identity(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await search.search_documents("x"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved


class CjkSearchTest(unittest.IsolatedAsyncioTestCase):
    """v2.7.2：CJK 中文语料实弹检索（真实 fixture 文档，含中英混排）。"""

    FIXTURES = Path(__file__).resolve().parent / "test_fixtures" / "cjk_docs"

    async def asyncSetUp(self):
        _reset_collab_dir()
        if DOCS_DIR.is_dir():
            # 递归清理：与 DocumentIngestTest 一致，防子目录残留
            for f in list(DOCS_DIR.rglob("*.md")):
                f.unlink()
        else:
            DOCS_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(self.FIXTURES.glob("*.md")):
            r = _parse(await documents.add_document(str(f)))
            self.assertTrue(r["success"], r)

    async def test_pure_chinese_substring_fts(self):
        # 4 字符中文词走 trigram FTS 路径
        r = _parse(await search.search_documents("量子纠错"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)
        hit = r["results"][0]
        self.assertEqual(hit["filename"], "quantum-survey.md")
        self.assertIn("量子纠错", hit["snippet"])
        self.assertGreater(hit["score"], 0)  # FTS 路径有 bm25 分数

    async def test_short_cjk_term_like_fallback(self):
        # 2 字符中文词（<3）走 LIKE 兜底
        r = _parse(await search.search_documents("纠错"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["filename"], "quantum-survey.md")

    async def test_mixed_cn_en_and_semantics(self):
        # 中英混排多词 AND：Python 与 异步 都命中的文档（meeting + code-comments）
        r = _parse(await search.search_documents("Python 异步"))
        self.assertTrue(r["success"], r)
        names = {h["filename"] for h in r["results"]}
        self.assertEqual(names, {"meeting-minutes.md", "code-comments.md"})
        # 单英文词只命中会议纪要（code-comments 无 FastAPI）
        r2 = _parse(await search.search_documents("FastAPI"))
        self.assertEqual(r2["count"], 1)
        self.assertEqual(r2["results"][0]["filename"], "meeting-minutes.md")

    async def test_fullwidth_punctuation_nfkc(self):
        # 全角括号查询经 NFKC → "(AI)"，命中 tech-notes.md（含 人工智能（AI））
        r = _parse(await search.search_documents("（AI）"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["filename"], "tech-notes.md")

    async def test_punctuation_only_clean_error(self):
        # v2.7.4（PC-B L2）：纯标点词过滤后无有效检索词 → 明确报错而非静默空结果
        r = _parse(await search.search_documents("，："))
        self.assertFalse(r["success"])
        self.assertIn("检索词", r["error"])
        # 混合：纯标点词被忽略，有效词仍正常检索
        r2 = _parse(await search.search_documents("， 量子纠错"))
        self.assertTrue(r2["success"], r2)
        self.assertEqual(r2["count"], 1)
        self.assertEqual(r2["results"][0]["filename"], "quantum-survey.md")

    async def test_cjk_scope_filter(self):
        r_docs = _parse(await search.search_documents("量子计算"))
        self.assertEqual(r_docs["count"], 1)
        self.assertEqual(r_docs["results"][0]["filename"], "quantum-survey.md")
        r_skills = _parse(await search.search_documents("量子计算", target_dir="collab/skills"))
        self.assertEqual(r_skills["count"], 0)

    async def test_bm25_ranking_cjk(self):
        # 双词 AND 召回两篇都含两个词的文档；结果按 bm25 分数降序。
        # 注意：SQLite 3.4x（CI actions 构建）与 3.50+（本机）对同一语料的
        # bm25 排序可能不同（词频/文档长度归一化差异），故只断言排序与分数
        # 一致，不把"特定文件排第一"写死（v2.7.3 CI 实测差异）。
        src1 = COLLAB_DIR / "bm-a.txt"
        src1.write_text("量子计算 量子计算 量子计算 神经网络 综述\n", encoding="utf-8")
        src2 = COLLAB_DIR / "bm-b.txt"
        src2.write_text("量子计算 神经网络 简介\n", encoding="utf-8")
        r1 = _parse(await documents.add_document(str(src1)))
        r2 = _parse(await documents.add_document(str(src2)))
        self.assertTrue(r1["success"] and r2["success"])
        r = _parse(await search.search_documents("量子计算 神经网络"))
        self.assertEqual(r["count"], 2)
        paths = {h["path"] for h in r["results"]}
        self.assertTrue(any("bm-a.md" in p for p in paths))
        self.assertTrue(any("bm-b.md" in p for p in paths))
        self.assertGreater(r["results"][0]["score"], 0)  # FTS 路径有 bm25 分数
        self.assertGreaterEqual(r["results"][0]["score"], r["results"][1]["score"])

    async def test_cjk_title_and_snippet(self):
        r = _parse(await search.search_documents("量子计算"))
        self.assertEqual(r["results"][0]["title"], "量子计算综述")
        self.assertIn("量子计算", r["results"][0]["snippet"])


class SeedPipelineTest(unittest.IsolatedAsyncioTestCase):
    """v2.4.0：种子流水线 pipeline-research / pipeline-security-review。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)

    async def _approve_as_hub(self, task_id: str):
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            r = _parse(await tasks.approve_task(task_id, comment="ok"))
            self.assertTrue(r["success"])
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    async def test_pipeline_research_creation(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-research",
            template_params='{"topic": "多智能体协作", "project_path": "C:/research"}'))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 4)
        steps = {s["step_name"]: s for s in r["steps"]}
        self.assertEqual(steps["research"]["status"], "pending")
        self.assertEqual(steps["draft"]["status"], "blocked")
        self.assertEqual(steps["review"]["status"], "blocked")
        self.assertEqual(steps["finalize"]["status"], "blocked")
        self.assertEqual(steps["research"]["template"], "research-doc")
        self.assertEqual(steps["review"]["template"], "doc-review")
        research_task = safe_read_json(INBOX_DIR / f"{steps['research']['task_id']}.json")
        self.assertIn("安全文件名", research_task["content"])  # L2：路径安全提示

    async def test_pipeline_research_full_run(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-research",
            template_params='{"topic": "DAG 看板", "project_path": "C:/research"}'))
        steps = {s["step_name"]: s["task_id"] for s in r["steps"]}
        await tasks.complete_task(steps["research"], evidence="调研完成")
        await tasks.complete_task(steps["draft"], evidence="草稿完成")
        await tasks.complete_task(steps["review"], evidence="复核提交")
        await self._approve_as_hub(steps["review"])
        await tasks.complete_task(steps["finalize"], evidence="定稿完成")
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "completed")
        self.assertEqual(agg["done_steps"], 4)

    async def test_pipeline_security_review_creation(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-security-review",
            template_params='{"target": "gateway.py", "project_path": "C:/app"}'))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        steps = {s["step_name"]: s for s in r["steps"]}
        self.assertEqual(steps["lint"]["status"], "pending")
        self.assertEqual(steps["security-review"]["status"], "blocked")
        sec = safe_read_json(INBOX_DIR / f"{steps['security-review']['task_id']}.json")
        self.assertTrue(sec["review_required"])
        self.assertEqual(sec["template"], "security-review")

    async def test_pipeline_security_review_gate(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-security-review",
            template_params='{"target": "gateway.py", "project_path": "C:/app"}'))
        steps = {s["step_name"]: s["task_id"] for s in r["steps"]}
        await tasks.complete_task(steps["lint"], evidence="lint ok")
        sec = safe_read_json(INBOX_DIR / f"{steps['security-review']}.json")
        self.assertEqual(sec["status"], "pending")
        await tasks.complete_task(steps["security-review"], evidence="无高危")
        await self._approve_as_hub(steps["security-review"])
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "completed")
        self.assertEqual(agg["done_steps"], 2)

    async def test_pipeline_research_requires_topic(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-research",
            template_params='{"project_path": "C:/research"}'))
        self.assertFalse(r["success"])
        self.assertIn("topic", r["error"])

    async def test_failed_creation_no_journal_orphan(self):
        # L1 回归：缺参回滚后 journal 无孤儿 created 事件
        jdir = COLLAB_DIR / "journal"
        before = {p.name for p in jdir.glob("*.jsonl")} if jdir.is_dir() else set()
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-research",
            template_params='{"project_path": "C:/research"}'))
        self.assertFalse(r["success"])
        after = {p.name for p in jdir.glob("*.jsonl")} if jdir.is_dir() else set()
        self.assertEqual(after, before)

    async def test_pipeline_ocr_delegate_review_setup(self):
        # v2.4.2：ocr 委托审查流水线（方案 B）——spec（中枢）→ review（复核闸门）
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-ocr-delegate-review",
            template_params='{"commit": "09737a5"}'))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        steps = {s["step_name"]: s for s in r["steps"]}
        self.assertEqual(steps["spec"]["status"], "pending")
        self.assertEqual(steps["spec"]["template"], "ocr-delegate-spec")
        self.assertEqual(steps["review"]["status"], "blocked")
        self.assertEqual(steps["review"]["template"], "ocr-delegate-review")
        spec_task = safe_read_json(INBOX_DIR / f"{steps['spec']['task_id']}.json")
        self.assertEqual(spec_task["assignee"], "PC-A")
        review_task = safe_read_json(INBOX_DIR / f"{steps['review']['task_id']}.json")
        self.assertIn("ocr-delegate-spec-09737a5.md", review_task["content"])
        self.assertTrue(review_task["review_required"])

    async def test_pipeline_ocr_delegate_review_gate(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-ocr-delegate-review",
            template_params='{"commit": "09737a5"}'))
        steps = {s["step_name"]: s["task_id"] for s in r["steps"]}
        await tasks.complete_task(steps["spec"], evidence="规格已生成")
        review = safe_read_json(INBOX_DIR / f"{steps['review']}.json")
        self.assertEqual(review["status"], "pending")
        await tasks.complete_task(steps["review"], evidence="行级意见 4 条，无高危")
        await self._approve_as_hub(steps["review"])
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "completed")
        self.assertEqual(agg["done_steps"], 2)

    async def test_pipeline_step_assignee_override(self):
        # v2.4.3（PC-B L3）：spec_assignee 参数化，spec 步骤不再单点绑定 PC-A
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-ocr-delegate-review",
            template_params='{"commit": "abc123", "spec_assignee": "PC-B"}'))
        self.assertTrue(r["success"])
        steps = {s["step_name"]: s for s in r["steps"]}
        spec_task = safe_read_json(INBOX_DIR / f"{steps['spec']['task_id']}.json")
        self.assertEqual(spec_task["assignee"], "PC-B")
        review_task = safe_read_json(INBOX_DIR / f"{steps['review']['task_id']}.json")
        self.assertIn("ocr-delegate-spec-abc123.md", review_task["content"])


class ToolRegistrationTest(unittest.TestCase):
    def test_all_11_tools_registered(self):
        tools = asyncio.run(server.list_tools())
        names = sorted(t.name for t in tools)
        self.assertEqual(names, sorted(TOOL_NAMES))


class SearchTroubleshootingTest(unittest.IsolatedAsyncioTestCase):
    """v3.29.0：本地排障索引 fixit 的薄 MCP 工具。"""

    async def test_empty_query_rejected(self):
        result = _parse(await fixit.search_troubleshooting("   "))
        self.assertFalse(result["success"])
        self.assertIn("非空", result["error"])

    async def test_search_hits_repo_rule(self):
        # fixit 语料是仓库旁的独立部署（不随 collab 发行，公开镜像 pwdh2026/collab-mcp
        # 不含 fixit/）；缺失时工具按设计返回明确报错，本用例跳过而非失败，保证 hermetic。
        if not fixit._FIXIT_DIR.is_dir():
            self.skipTest(f"fixit 语料未部署: {fixit._FIXIT_DIR}")
        result = _parse(await fixit.search_troubleshooting("acquire_project_lock", limit=3))
        self.assertTrue(result["success"], result)
        self.assertGreaterEqual(result["count"], 1)
        keys = {item["key"] for item in result["results"]}
        self.assertIn("C5", keys)


class RagQueryTest(unittest.IsolatedAsyncioTestCase):
    """v3.30.0：本地 RAG 薄层 rag_query（关键词 + 语义召回，降级只回资料）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        search.reset_state()
        semantic._close_index()
        semantic._STATE.clear()
        for d in (DOCS_DIR, COLLAB_DIR.parent / "notes"):
            if d.is_dir():
                for f in list(d.rglob("*.md")):
                    f.unlink()

    async def _ingest_txt(self, name, body):
        src = COLLAB_DIR / name
        src.write_text(body, encoding="utf-8")
        r = _parse(await documents.add_document(str(src)))
        self.assertTrue(r["success"], r)
        return r

    async def test_empty_query_rejected(self):
        r = _parse(await rag.rag_query("   "))
        self.assertFalse(r["success"])
        self.assertIn("非空", r["error"])

    async def test_target_escape_rejected(self):
        r = _parse(await rag.rag_query("ragtoken", target_dir="../escape"))
        self.assertFalse(r["success"])
        self.assertIn("超出共享目录范围", r["error"])

    async def test_retrieval_only_returns_existing_document(self):
        await self._ingest_txt(
            "rag-source.txt",
            "# RAG\n\nragtoken 是本次检索的专属锚点，用于验证资料片段回传。",
        )
        search.reset_state()
        with patch.object(rag, "_semantic_hits", new=AsyncMock(return_value=([], ""))):
            r = _parse(await rag.rag_query("ragtoken", use_llm=False))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["answering_mode"], "retrieval_only")
        self.assertGreaterEqual(r["retrieved"], 1)
        self.assertTrue(any("rag-source.md" in c["path"] for c in r["citations"]))

    async def test_rrf_merge_semantic_and_lexical_sources(self):
        # v3.30.1：不依赖真实 Ollama，只验证关键词/语义两路命中同一文档时
        # RRF 会去重并保留两路来源标记，citations 仍指向目标文件。
        await self._ingest_txt(
            "rag-merge.txt",
            "# RAG Merge\n\nragtoken merge 是合并锚点。",
        )
        search.reset_state()
        semantic_hit = [{
            "path": "documents/rag-merge.md",
            "title": "RAG Merge",
            "score": 0.91,
            "snippet": "ragtoken merge 是合并锚点。",
        }]
        with patch.object(rag, "_semantic_hits", new=AsyncMock(return_value=(semantic_hit, ""))):
            r = _parse(await rag.rag_query("ragtoken merge", use_llm=False))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["answering_mode"], "retrieval_only")
        self.assertGreaterEqual(r["retrieved"], 1)
        hit = next(c for c in r["citations"] if "rag-merge.md" in c["path"])
        self.assertEqual(set(hit["sources"]), {"semantic", "lexical"})

    async def test_llm_prompt_grounds_answer_in_source(self):
        # v3.30.2：真实链路里 3B 模型容易被旧 prompt 带偏，回答“资料未说明”；
        # 这里断言生成 prompt 明确要求“依据资料直接回答”，并把正文标出来。
        await self._ingest_txt(
            "rag-prompt.txt",
            "# RAG Prompt\n\nragtoken 使用 qwen3-embedding:0.6b 和 qwen2.5:3b。",
        )
        search.reset_state()
        semantic_hit = [{
            "path": "documents/rag-prompt.md",
            "title": "RAG Prompt",
            "score": 0.9,
            "snippet": "ragtoken 使用 qwen3-embedding:0.6b 和 qwen2.5:3b。",
        }]
        with patch.object(rag, "_semantic_hits", new=AsyncMock(return_value=(semantic_hit, ""))), \
             patch.object(rag, "_lexical_hits", new=AsyncMock(return_value=([], ""))), \
             patch.object(rag, "_ollama_available", return_value=True), \
             patch.object(rag, "_ollama_answer", return_value="qwen3-embedding:0.6b 和 qwen2.5:3b [来源 1]") as answer_mock:
            r = _parse(await rag.rag_query("ragtoken", use_llm=True))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["answering_mode"], "llm")
        system, prompt, _timeout = answer_mock.call_args.args
        self.assertIn("依据用户消息中“资料”部分回答", system)
        self.assertIn("请依据下面的资料直接回答问题", prompt)
        self.assertIn("正文：", prompt)


class TestSsrGuard(unittest.TestCase):
    """v2.8.0：web_fetch SSRF 防护单元测试（不触发真实 DNS/网络）。"""

    def _check(self, url):
        return websearch._check_url(url)

    def test_private_ip_literal_rejected(self):
        for u in (
            "http://127.0.0.1/",
            "http://10.1.2.3/",
            "http://192.168.1.1/",
            "http://169.254.1.1/",
            "http://[::1]/",
        ):
            self.assertIn("拒绝", self._check(u) or "", u)

    def test_public_ip_literal_allowed(self):
        self.assertIsNone(self._check("http://8.8.8.8/x"))
        self.assertIsNone(self._check("https://1.1.1.1/"))

    def test_scheme_and_host_validation(self):
        self.assertIn("http/https", self._check("file:///C:/x"))
        self.assertIn("主机名", self._check("https:///path"))
        self.assertIn("拒绝", self._check("http://localhost/") or "")
        self.assertIn("拒绝", self._check("http://foo.local/") or "")

    def test_hostname_resolution_private_rejected(self):
        with patch("collab_mcp.websearch.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 80)),
        ]):
            self.assertIn("拒绝", self._check("http://internal.example/") or "")

    def test_hostname_resolution_public_allowed(self):
        with patch("collab_mcp.websearch.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]):
            self.assertIsNone(self._check("http://public.example/"))


class TestWebSearch(unittest.IsolatedAsyncioTestCase):
    """v2.8.0：外部检索 web_search / web_fetch（mock 网络，hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_key = os.environ.get("FIRECRAWL_API_KEY")
        os.environ.pop("FIRECRAWL_API_KEY", None)
        # v3.4.0：本类不测 scrapling——钉死探测 False，保证即使本机装了 scrapling 也 hermetic
        self._scrapling_off = patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=False))
        self._scrapling_off.start()
        self.addCleanup(self._scrapling_off.stop)
        self._browser_off = patch("collab_mcp.websearch._scrapling_browser_available", new=AsyncMock(return_value=False))
        self._browser_off.start()
        self.addCleanup(self._browser_off.stop)
        # web_fetch 入库写 documents/web/（DOCS_DIR 落在 %TEMP% 持久目录），
        # 前后都清理，避免跨用例/跨进程残留污染其他测试类的计数断言
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    async def asyncTearDown(self):
        if self._saved_key is None:
            os.environ.pop("FIRECRAWL_API_KEY", None)
        else:
            os.environ["FIRECRAWL_API_KEY"] = self._saved_key
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    async def test_search_parses_bing_results(self):
        payload = [
            {"title": "Python", "url": "https://www.python.org/", "snippet": "官方站", "source": "python.org"},
            {"title": "教程", "url": "https://docs.python.org/", "snippet": "入门", "source": "docs.python.org"},
        ]
        with patch("collab_mcp.websearch._bing_search", return_value=payload):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "bing")
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["results"][0]["url"], "https://www.python.org/")
        self.assertEqual(r["results"][0]["rank"], 1)
        self.assertEqual(r["results"][1]["rank"], 2)
        self.assertEqual(r["results"][1]["snippet"], "入门")

    async def test_search_empty_results(self):
        # 2026-08-06：补 mock _ddg_search——此前降级链落到真实 DuckDuckGo（v2.9 L5 非 hermetic 面）
        with patch("collab_mcp.websearch._bing_search", return_value=[]), patch(
            "collab_mcp.websearch._ddg_search", return_value=[]
        ):
            r = _parse(await websearch.web_search("zzz_no_such"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["results"], [])

    async def test_search_empty_results_falls_through(self):
        # PC-B L2：空结果视为无命中，继续降级链
        fallback = [{"title": "D", "url": "https://ddg.example/", "snippet": "s", "source": "ddg.example"}]
        with patch("collab_mcp.websearch._bing_search", return_value=[]), patch(
            "collab_mcp.websearch._ddg_search", return_value=fallback
        ):
            r = _parse(await websearch.web_search("x"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "duckduckgo")
        self.assertEqual(r["count"], 1)

    async def test_search_all_empty_returns_zero(self):
        with patch("collab_mcp.websearch._bing_search", return_value=[]), patch(
            "collab_mcp.websearch._ddg_search", return_value=[]
        ):
            r = _parse(await websearch.web_search("x"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["provider"], "duckduckgo")

    async def test_bing_title_whitespace_normalized(self):
        # PC-B L1：剥标签后的内部多空格要归一为单空格
        html_body = (
            "<html><body><h2><a href=\"https://www.python.org/\">Welcome to  Python .org</a></h2>"
            "<p>Mission  of  the  PSF</p><cite>www.python.org</cite></body></html>"
        ).encode("utf-8")
        with patch(
            "collab_mcp.websearch._fetch_raw",
            return_value=("https://www.bing.com/search", html_body),
        ):
            results = await websearch._bing_search("python", 5, 15)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Welcome to Python .org")
        self.assertEqual(results[0]["snippet"], "Mission of the PSF")
        self.assertEqual(results[0]["source"], "www.python.org")

    async def test_search_error_mapping(self):
        cases = [
            (websearch._NetworkError("timeout", "外部请求超时（15s）"), "超时"),
            (websearch._NetworkError("rate_limited", "外部服务限流（HTTP 429），请稍后重试"), "限流"),
            (websearch._NetworkError("network", "所有搜索后端均失败: bing: 网络请求失败"), "网络"),
        ]
        for err, keyword in cases:
            with self.subTest(err=err.kind):
                with patch("collab_mcp.websearch._search_with_fallback", side_effect=err):
                    r = _parse(await websearch.web_search("x"))
                self.assertFalse(r["success"])
                self.assertIn(keyword, r["error"])

    async def test_search_fallback_to_next_provider(self):
        # Bing 不可达 → 自动降级 DuckDuckGo（无 key 时链为 bing→duckduckgo）
        fallback = [{"title": "D", "url": "https://ddg.example/", "snippet": "s", "source": "ddg.example"}]
        with patch(
            "collab_mcp.websearch._bing_search",
            side_effect=websearch._NetworkError("network", "网络请求失败"),
        ), patch("collab_mcp.websearch._ddg_search", return_value=fallback):
            r = _parse(await websearch.web_search("x"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "duckduckgo")
        self.assertEqual(r["count"], 1)

    async def test_search_all_providers_fail(self):
        err = websearch._NetworkError("network", "网络请求失败")
        with patch("collab_mcp.websearch._bing_search", side_effect=err), patch(
            "collab_mcp.websearch._ddg_search", side_effect=err
        ):
            r = _parse(await websearch.web_search("x"))
        self.assertFalse(r["success"])
        self.assertIn("所有搜索后端均失败", r["error"])

    async def test_search_pure_punctuation_rejected(self):
        r = _parse(await websearch.web_search("？？？"))
        self.assertFalse(r["success"])
        self.assertIn("有效检索词", r["error"])

    async def test_search_empty_query_rejected(self):
        r = _parse(await websearch.web_search("   "))
        self.assertFalse(r["success"])
        self.assertIn("query", r["error"])

    async def test_search_max_results_clamp(self):
        topics = [
            {"title": f"t{i}", "url": f"https://e{i}.com/", "snippet": f"d{i}", "source": f"e{i}.com"}
            for i in range(5)
        ]
        with patch("collab_mcp.websearch._bing_search", return_value=topics):
            r2 = _parse(await websearch.web_search("x", max_results=2))
            self.assertEqual(r2["count"], 2)
            r0 = _parse(await websearch.web_search("x", max_results=0))
            self.assertEqual(r0["count"], 1)
            rbad = _parse(await websearch.web_search("x", max_results="abc"))
            self.assertEqual(rbad["count"], 5)

    async def test_fetch_stdlib_and_save(self):
        html_body = b"<html><head><title>\xe6\xb5\x8b\xe8\xaf\x95\xe9\xa1\xb5</title></head><body><p>hello world \xe5\x86\x85\xe5\xae\xb9</p></body></html>"
        with patch(
            "collab_mcp.websearch._stdlib_fetch",
            return_value=("https://8.8.8.8/page", html_body),
        ):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "stdlib")
        self.assertEqual(r["title"], "测试页")
        self.assertIn("hello world", r["markdown_preview"])
        self.assertTrue(r["saved"])
        self.assertTrue(str(r["saved"]["path"]).startswith("documents/web/"))
        saved_file = DOCS_DIR / "web" / r["saved"]["filename"]
        self.assertTrue(saved_file.exists())

    async def test_fetch_firecrawl_when_key_set(self):
        os.environ["FIRECRAWL_API_KEY"] = "test-key"
        with patch(
            "collab_mcp.websearch._firecrawl_markdown",
            return_value={"title": "Fire 页", "markdown": "# 外部内容\n\n正文 ABC"},
        ):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "firecrawl")
        self.assertEqual(r["title"], "Fire 页")
        self.assertIn("ABC", r["markdown_preview"])
        self.assertTrue(r["saved"])

    async def test_fetch_save_disabled(self):
        with patch(
            "collab_mcp.websearch._stdlib_fetch",
            return_value=("https://8.8.8.8/", b"<html><body>abc</body></html>"),
        ):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/", save=False))
        self.assertTrue(r["success"], r)
        self.assertIsNone(r["saved"])

    async def test_fetch_ssrf_rejected_before_request(self):
        r = _parse(await websearch.web_fetch("http://127.0.0.1/"))
        self.assertFalse(r["success"])
        self.assertIn("拒绝", r["error"])
        r2 = _parse(await websearch.web_fetch("file:///C:/x"))
        self.assertFalse(r2["success"])
        self.assertIn("http/https", r2["error"])

    async def test_fetch_redirect_to_internal_rejected(self):
        with patch(
            "collab_mcp.websearch._stdlib_fetch",
            return_value=("http://10.0.0.1/evil", b"<html>hack</html>"),
        ):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/"))
        self.assertFalse(r["success"])
        self.assertIn("重定向", r["error"])

    async def test_fetch_too_large_maps_to_error(self):
        with patch(
            "collab_mcp.websearch._fetch_raw",
            side_effect=websearch._NetworkError("too_large", "外部响应超过上限（10MB）"),
        ):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/"))
        self.assertFalse(r["success"])
        self.assertIn("上限", r["error"])

    async def test_identity_guard_under_require_identity(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await websearch.web_search("x"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
            r2 = _parse(await websearch.web_fetch("https://8.8.8.8/"))
            self.assertFalse(r2["success"])
            self.assertIn("需要调用者身份", r2["error"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved

class TestWebSearchWigoloV31(unittest.IsolatedAsyncioTestCase):
    """v3.1.0：wigolo 可选搜索 provider（mock /health 与 /v1/search，hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
            "WIGOLO_API_TOKEN": os.environ.get("WIGOLO_API_TOKEN"),
            "WIGOLO_PROBE_TIMEOUT": os.environ.get("WIGOLO_PROBE_TIMEOUT"),
            "WEB_SEARCH_PROVIDER": os.environ.get("WEB_SEARCH_PROVIDER"),
            "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
        }
        for k in self._saved:
            os.environ.pop(k, None)
        websearch._reset_wigolo_cache()

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        websearch._reset_wigolo_cache()

    def _set_wigolo(self, url="http://127.0.0.1:3333"):
        os.environ["WIGOLO_REST_URL"] = url
        os.environ["WIGOLO_PROBE_TIMEOUT"] = "1"

    def _payload(self, n=2, prefix="w"):
        return [
            {
                "title": f"{prefix}-t{i}",
                "url": f"https://example.com/{prefix}/{i}",
                "snippet": f"{prefix}-s{i}",
                "source": "example.com",
            }
            for i in range(n)
        ]

    # 1) 未配置 WIGOLO_REST_URL → 不探测、不走 wigolo、链保持 bing 优先
    async def test_wigolo_not_configured_keeps_bing_chain(self):
        payload = self._payload()
        with patch("collab_mcp.websearch._bing_search", new=AsyncMock(return_value=payload)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock()) as wsearch:
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "bing")
        wsearch.assert_not_called()

    # 2) 探测可用 → wigolo 成为首选 provider
    async def test_wigolo_available_becomes_first_provider(self):
        self._set_wigolo()
        payload = self._payload()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock(return_value=payload)):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["results"][0]["rank"], 1)
        self.assertEqual(r["results"][0]["snippet"], "w-s0")

    # 3) wigolo 空结果 → 自动降级 bing（与 L2 语义一致）
    async def test_wigolo_empty_falls_through_to_bing(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock(return_value=[])), \
             patch("collab_mcp.websearch._bing_search", new=AsyncMock(return_value=self._payload(1, "b"))):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "bing")
        self.assertEqual(r["count"], 1)

    # 4) 探测失败 → 保持 bing 链（wigolo 不进入链）
    async def test_wigolo_probe_failure_keeps_bing_chain(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=False)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock()) as wsearch, \
             patch("collab_mcp.websearch._bing_search", new=AsyncMock(return_value=self._payload(1, "b"))):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "bing")
        wsearch.assert_not_called()

    # 5) wigolo 调用失败（超时）→ 自动降级 bing
    async def test_wigolo_error_falls_through(self):
        self._set_wigolo()
        err = websearch._NetworkError("timeout", "外部请求超时（1s）")
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock(side_effect=err)), \
             patch("collab_mcp.websearch._bing_search", new=AsyncMock(return_value=self._payload(1, "b"))):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "bing")

    # 6) 结果映射 + 脏条目过滤（url 缺失/非 http）、snippet 回退至 evidence
    async def test_wigolo_result_mapping_and_filter(self):
        self._set_wigolo()
        body = json.dumps({
            "results": [
                {"title": "  T1  ", "url": "https://a.com/1", "snippet": "  s1  ", "relevance_score": 0.9},
                {"title": "T2", "url": "https://b.com/2", "snippet": "", "evidence": [{"excerpt": "fallback-snippet"}]},
                {"title": "Bad1", "url": "", "snippet": "x"},
                {"title": "Bad2", "url": "javascript:alert(1)", "snippet": "x"},
                {"title": "Bad3", "url": "ftp://c.com/3", "snippet": "x"},
                {"title": "Bad4", "url": "not-a-url", "snippet": "x"},
            ]
        }).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=("http://127.0.0.1:3333/v1/search", body))):
            results = await websearch._wigolo_search("Python", 5, 10)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "T1")
        self.assertEqual(results[0]["snippet"], "s1")
        self.assertEqual(results[0]["source"], "a.com")
        self.assertEqual(results[1]["snippet"], "fallback-snippet")

    # 7) 强制 WEB_SEARCH_PROVIDER=wigolo
    async def test_wigolo_forced_provider(self):
        self._set_wigolo()
        os.environ["WEB_SEARCH_PROVIDER"] = "wigolo"
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock(return_value=self._payload(2, "w"))):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")

    # 8) 探测结果进程内缓存：连续两次只探一次 /health
    async def test_wigolo_probe_cached(self):
        self._set_wigolo()
        calls = []

        async def fake_fetch(url, timeout, **kw):
            calls.append(url)
            if url.endswith("/health"):
                return (url, b'{"status":"healthy"}')
            return (url, json.dumps({"results": self._payload(1, "w")}).encode("utf-8"))

        with patch("collab_mcp.websearch._fetch_raw", new=fake_fetch), \
             patch("collab_mcp.websearch._bing_search", new=AsyncMock()):
            r1 = _parse(await websearch.web_search("Python"))
            r2 = _parse(await websearch.web_search("asyncio"))
        health_calls = [u for u in calls if u.endswith("/health")]
        self.assertEqual(len(health_calls), 1, calls)
        self.assertEqual(r1["provider"], "wigolo")
        self.assertEqual(r2["provider"], "wigolo")

    # 9) max_results 钳制仍生效
    async def test_wigolo_max_results_clamp(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_search", new=AsyncMock(return_value=self._payload(12, "w"))):
            r = _parse(await websearch.web_search("Python", max_results=5))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 5)

    # 10) 配置 WIGOLO_API_TOKEN 时注入 Authorization 头；请求体含 search_depth=fast
    async def test_wigolo_auth_header_and_payload(self):
        self._set_wigolo()
        os.environ["WIGOLO_API_TOKEN"] = "secret-token"
        seen = {}

        async def fake_fetch(url, timeout, **kw):
            seen["url"] = url
            seen["headers"] = kw.get("headers", {})
            seen["data"] = kw.get("data")
            if url.endswith("/health"):
                return (url, b'{"status":"healthy"}')
            return (url, json.dumps({"results": self._payload(1, "w")}).encode("utf-8"))

        with patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_search("Python"))
        self.assertTrue(r["success"], r)
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer secret-token")
        self.assertIn(b'"search_depth": "fast"', seen["data"])


class TestWebFetchWigoloV32(unittest.IsolatedAsyncioTestCase):
    """v3.2.0：wigolo 可选 fetch 兑底后端（stdlib 快速路径失败/空正文时启用；mock hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
            "WIGOLO_API_TOKEN": os.environ.get("WIGOLO_API_TOKEN"),
            "WIGOLO_PROBE_TIMEOUT": os.environ.get("WIGOLO_PROBE_TIMEOUT"),
            "WEB_FETCH_PROVIDER": os.environ.get("WEB_FETCH_PROVIDER"),
            "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
        }
        for k in self._saved:
            os.environ.pop(k, None)
        websearch._reset_wigolo_cache()
        # v3.3.0：本类不测 scrapling——钉死探测 False，保证即使本机装了 scrapling 也 hermetic
        self._scrapling_off = patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=False))
        self._scrapling_off.start()
        self.addCleanup(self._scrapling_off.stop)
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        websearch._reset_wigolo_cache()
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    def _set_wigolo(self, url="http://127.0.0.1:3333"):
        os.environ["WIGOLO_REST_URL"] = url
        os.environ["WIGOLO_PROBE_TIMEOUT"] = "1"

    HTML = b"<html><head><title>\xe6\xb5\x8b\xe8\xaf\x95\xe9\xa1\xb5</title></head><body><p>hello world \xe5\x86\x85\xe5\xae\xb9</p></body></html>"
    EMPTY_HTML = b"<html><body></body></html>"

    # 1) 未配置 WIGOLO_REST_URL → 不走 wigolo，stdlib 路径不变
    async def test_fetch_not_configured_keeps_stdlib(self):
        with patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch, \
             patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.HTML)):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "stdlib")
        wfetch.assert_not_called()

    # 2) stdlib 空正文 → wigolo 兑底成功，映射 title/markdown
    async def test_fetch_stdlib_empty_falls_to_wigolo(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(return_value=("Wigolo 页", "# 外部内容\n\n正文 ABC"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")
        self.assertEqual(r["title"], "Wigolo 页")
        self.assertIn("ABC", r["markdown_preview"])

    # 3) stdlib 成功 → 不调 wigolo（普通页快路径，零探测开销）
    async def test_fetch_stdlib_success_skips_wigolo(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.HTML)), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)) as probe, \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "stdlib")
        probe.assert_not_called()
        wfetch.assert_not_called()

    # 4) 有 FIRECRAWL_API_KEY 时 firecrawl 优先
    async def test_fetch_firecrawl_still_priority(self):
        self._set_wigolo()
        os.environ["FIRECRAWL_API_KEY"] = "test-key"
        with patch("collab_mcp.websearch._firecrawl_markdown", return_value={"title": "Fire 页", "markdown": "# 外部内容\n\n正文 ABC"}), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "firecrawl")
        wfetch.assert_not_called()

    # 5) stdlib 失败 + wigolo 兑底失败 → 明确失败（stdlib 错误优先）
    async def test_fetch_both_fail_returns_error(self):
        self._set_wigolo()
        stdlib_err = websearch._NetworkError("network", "网络请求失败: boom")
        wigolo_err = websearch._NetworkError("http", "wigolo fetch 目标返回 HTTP 403")
        with patch("collab_mcp.websearch._stdlib_fetch", side_effect=stdlib_err), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(side_effect=wigolo_err)):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("网络请求失败", r["error"])

    # 6) stdlib 空正文 + wigolo 兑底抛错误 → 明确失败
    async def test_fetch_stdlib_empty_then_wigolo_error_fails(self):
        self._set_wigolo()
        wigolo_err = websearch._NetworkError("provider", "wigolo fetch 未返回文本")
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(side_effect=wigolo_err)):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)

    # 7) SSRF 前置：内网 URL 拒绝且不发起任何请求
    async def test_fetch_ssrf_rejected_before_wigolo(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch, \
             patch("collab_mcp.websearch._stdlib_fetch", new=AsyncMock()) as sfetch:
            r = _parse(await websearch.web_fetch("http://127.0.0.1:22/"))
        self.assertFalse(r["success"], r)
        self.assertIn("拒绝", r["error"])
        wfetch.assert_not_called()
        sfetch.assert_not_called()

    # 8) WIGOLO_API_TOKEN 注入 Authorization；请求体含 render_js=auto
    async def test_fetch_wigolo_token_injected(self):
        self._set_wigolo()
        os.environ["WIGOLO_API_TOKEN"] = "secret-token"
        seen = {}

        async def fake_fetch(url, timeout, **kw):
            seen["headers"] = kw.get("headers", {})
            seen["data"] = kw.get("data")
            if url.endswith("/health"):
                return (url, b'{"status":"healthy"}')
            if "/v1/fetch" in url:
                return (url, json.dumps({"url": "https://8.8.8.8/page", "title": "T", "markdown": "# M", "http_status": 200}).encode("utf-8"))
            return (url, b"<html><body></body></html>")

        with patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer secret-token")
        self.assertIn(b'"render_js": "auto"', seen["data"])

    # 9) 入库元数据 provider=wigolo + 检索联动
    async def test_fetch_wigolo_ingest_metadata_and_search(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(return_value=("Wigolo 页", "unique-token-xyz 正文"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")
        self.assertTrue(r["saved"])
        saved_file = DOCS_DIR / "web" / r["saved"]["filename"]
        self.assertTrue(saved_file.exists())
        content = saved_file.read_text(encoding="utf-8")
        self.assertIn("wigolo", content)
        res = _parse(await search.search_documents("unique-token-xyz"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["filename"], r["saved"]["filename"])

    # 10) 强制 WEB_FETCH_PROVIDER=wigolo：可用命中 / 不可用明确失败
    async def test_fetch_forced_wigolo(self):
        self._set_wigolo()
        os.environ["WEB_FETCH_PROVIDER"] = "wigolo"
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(return_value=("W", "# M"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")

    async def test_fetch_forced_wigolo_unavailable_fails(self):
        os.environ["WEB_FETCH_PROVIDER"] = "wigolo"
        r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("强制 wigolo", r["error"])

    # 11) _wigolo_fetch 映射与错误分支（http_status / error / 空 markdown）
    async def test_wigolo_fetch_mapping_and_errors(self):
        self._set_wigolo()
        base = "http://127.0.0.1:3333"
        body = json.dumps({"url": base, "title": "T", "markdown": "# M\n\n正文", "http_status": 200}).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=(base, body))):
            title, md = await websearch._wigolo_fetch("https://8.8.8.8/", 10)
        self.assertEqual(title, "T")
        self.assertIn("正文", md)
        body403 = json.dumps({"url": base, "title": "", "markdown": "", "http_status": 403}).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=(base, body403))):
            with self.assertRaises(websearch._NetworkError) as cm:
                await websearch._wigolo_fetch("https://8.8.8.8/", 10)
        self.assertEqual(cm.exception.kind, "http")
        bodyerr = json.dumps({"url": base, "error": "blocked_by_challenge"}).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=(base, bodyerr))):
            with self.assertRaises(websearch._NetworkError) as cm2:
                await websearch._wigolo_fetch("https://8.8.8.8/", 10)
        self.assertEqual(cm2.exception.kind, "provider")
        bodyempty = json.dumps({"url": base, "title": "", "markdown": "", "http_status": 200}).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=(base, bodyempty))):
            with self.assertRaises(websearch._NetworkError):
                await websearch._wigolo_fetch("https://8.8.8.8/", 10)

    # 中1 回归（PC-C）：title 空时从 markdown 首 # 标题兑底
    async def test_wigolo_fetch_title_fallback(self):
        self._set_wigolo()
        base = "http://127.0.0.1:3333"
        body = json.dumps({"url": base, "title": "", "markdown": "# Welcome to Python\n\n正文", "http_status": 200}).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=(base, body))):
            title, md = await websearch._wigolo_fetch("https://8.8.8.8/", 10)
        self.assertEqual(title, "Welcome to Python")
        body_noheading = json.dumps({"url": base, "title": "", "markdown": "正文无标题", "http_status": 200}).encode("utf-8")
        with patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(return_value=(base, body_noheading))):
            title2, _ = await websearch._wigolo_fetch("https://8.8.8.8/", 10)
        self.assertEqual(title2, "")


class TestWebFetchScraplingV33(unittest.IsolatedAsyncioTestCase):
    """v3.3.0：scrapling 可选 fetch 兜底后端（stdlib 失败/空正文时启用；mock hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {
            "WEB_FETCH_PROVIDER": os.environ.get("WEB_FETCH_PROVIDER"),
            "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
            "SCRAPLING_IMPERSONATE": os.environ.get("SCRAPLING_IMPERSONATE"),
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
            "WIGOLO_API_TOKEN": os.environ.get("WIGOLO_API_TOKEN"),
            "WIGOLO_PROBE_TIMEOUT": os.environ.get("WIGOLO_PROBE_TIMEOUT"),
        }
        for k in self._saved:
            os.environ.pop(k, None)
        websearch._reset_scrapling_cache()
        # v3.4.0：本类不测浏览器路径——钉死 False，保证 _scrapling_fetch 直接单测 hermetic
        self._browser_off = patch("collab_mcp.websearch._scrapling_browser_available", new=AsyncMock(return_value=False))
        self._browser_off.start()
        self.addCleanup(self._browser_off.stop)
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        websearch._reset_scrapling_cache()
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    def _set_wigolo(self, url="http://127.0.0.1:3333"):
        os.environ["WIGOLO_REST_URL"] = url
        os.environ["WIGOLO_PROBE_TIMEOUT"] = "1"

    def _scrapling_on(self, result):
        """打开 scrapling 探测并注入 _scrapling_fetch 返回 (final_url, title, markdown, fetch_method)。"""
        if len(result) == 3:
            result = result + ("http",)
        p1 = patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True))
        p1.start()
        self.addCleanup(p1.stop)
        p2 = patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(return_value=result))
        p2.start()
        self.addCleanup(p2.stop)

    HTML = b"<html><head><title>\xe6\xb5\x8b\xe8\xaf\x95\xe9\xa1\xb5</title></head><body><p>hello world \xe5\x86\x85\xe5\xae\xb9</p></body></html>"
    EMPTY_HTML = b"<html><body></body></html>"

    # 1) 未安装 scrapling（探测 False）→ 自动链=stdlib→wigolo，scrapling_fetch 不被调用
    async def test_not_installed_keeps_stdlib_wigolo_chain(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=False)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock()) as sfetch, \
             patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(return_value=("W", "wigolo markdown body"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")
        sfetch.assert_not_called()

    # 2) stdlib 失败 → scrapling 兜底成功（provider=scrapling，title/markdown 映射）
    async def test_stdlib_fail_scrapling_fallback(self):
        self._scrapling_on(("https://8.8.8.8/final", "Scrapling Title", "scrapling body content enough words here"))
        with patch("collab_mcp.websearch._stdlib_fetch", side_effect=websearch._NetworkError("network", "网络请求失败")), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")
        self.assertEqual(r["title"], "Scrapling Title")
        wfetch.assert_not_called()

    # 3) stdlib 空正文 → scrapling 兜底成功
    async def test_stdlib_empty_scrapling_fallback(self):
        self._scrapling_on(("https://8.8.8.8/final", "S", "scrapling extracted body content here"))
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")

    # 4) stdlib 成功 → scrapling/wigolo 均不触发（零开销）
    async def test_stdlib_success_skips_scrapling_wigolo(self):
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.HTML)), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock()) as s_avail, \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock()) as sfetch, \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "stdlib")
        s_avail.assert_not_awaited()
        sfetch.assert_not_called()
        wfetch.assert_not_called()

    # 5) scrapling 失败 → wigolo 兜底（链继续）
    async def test_scrapling_fail_falls_to_wigolo(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(side_effect=websearch._NetworkError("http", "scrapling fetch 目标返回 HTTP 403"))), \
             patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(return_value=("W", "wigolo markdown body"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")

    # 6) stdlib+scrapling+wigolo 全失败 → 明确失败（stdlib 错误优先）
    async def test_all_fail_stdlib_error_priority(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._stdlib_fetch", side_effect=websearch._NetworkError("network", "网络请求失败: boom")), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(side_effect=websearch._NetworkError("http", "scrapling 403"))), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(side_effect=websearch._NetworkError("http", "wigolo 403"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("网络请求失败", r["error"])

    # 6b) 低3（PC-C）：stdlib 空正文 + scrapling 失败 + wigolo 不可用 → 保留 scrapling 具体错误
    async def test_stdlib_empty_scrapling_error_preserved(self):
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(side_effect=websearch._NetworkError("http", "scrapling fetch 目标返回 HTTP 403"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("403", r["error"])

    # 7) SSRF 前置：内网 URL 拒绝且不发起任何请求
    async def test_ssrf_rejected_before_scrapling(self):
        with patch("collab_mcp.websearch._stdlib_fetch", new=AsyncMock()) as sfetch, \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock()) as scfetch, \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("http://127.0.0.1:22/"))
        self.assertFalse(r["success"], r)
        self.assertIn("拒绝", r["error"])
        sfetch.assert_not_called()
        scfetch.assert_not_called()
        wfetch.assert_not_called()

    # 8) 重定向最终 URL 复查：scrapling 返回内网 final_url → 拒绝且不落到 wigolo（安全硬失败）
    async def test_redirect_final_url_rechecked(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._stdlib_fetch", side_effect=websearch._NetworkError("network", "网络请求失败")), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(return_value=("http://127.0.0.1/x", "T", "body content here", "http"))), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("重定向目标被拒绝", r["error"])
        wfetch.assert_not_called()

    # 9) 强制 WEB_FETCH_PROVIDER=scrapling：可用+成功 → provider=scrapling
    async def test_forced_scrapling_success(self):
        os.environ["WEB_FETCH_PROVIDER"] = "scrapling"
        self._scrapling_on(("https://8.8.8.8/final", "Forced", "forced scrapling body content"))
        r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")
        self.assertEqual(r["title"], "Forced")

    # 10) 强制 scrapling 不可用 → 明确失败（不静默降级）
    async def test_forced_scrapling_unavailable_fails(self):
        os.environ["WEB_FETCH_PROVIDER"] = "scrapling"
        with patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=False)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock()) as sfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("强制 scrapling", r["error"])
        sfetch.assert_not_called()

    # 11) 未知 WEB_FETCH_PROVIDER → 明确失败
    async def test_unknown_provider_fails(self):
        os.environ["WEB_FETCH_PROVIDER"] = "scraplingg"
        r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("未知 WEB_FETCH_PROVIDER", r["error"])

    # 12) 入库元数据 provider=scrapling + dedup + 检索联动
    async def test_ingest_metadata_and_search(self):
        self._scrapling_on(("https://8.8.8.8/q", "Quantum Basics", "# Quantum Basics\n\nQuantum computing uses qubits for computation. Entanglement is a key resource in quantum algorithms."))
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/q", self.EMPTY_HTML)):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")
        self.assertTrue(r["saved"])
        res = _parse(await search.search_documents("entanglement"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["filename"], r["saved"]["filename"])
        self.assertEqual(res["results"][0]["title"], "Quantum Basics")

    # ---------- _scrapling_fetch 直接单元（fake Fetcher 注入 _SCRAPLING_FETCHER）----------
    async def test_scrapling_fetch_title_and_final_url(self):
        class _FakePage:
            status = 200
            url = "https://8.8.8.8/final"
            body = ("<html><head><title>Scrapling Title</title></head><body>"
                    "<h1>Scrapling Title</h1>"
                    "<p>this is a sufficiently long real body paragraph with many words for extraction "
                    "so that the body extraction keeps it in the result</p></body></html>").encode("utf-8")
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
            final_url, title, markdown, fetch_method = await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(final_url, "https://8.8.8.8/final")
        self.assertEqual(title, "Scrapling Title")
        self.assertIn("real body paragraph", markdown)
        self.assertEqual(fetch_method, "http")

    async def test_scrapling_fetch_http_status(self):
        class _FakePage:
            status = 403
            url = "https://8.8.8.8/page"
            body = b"<html><body>forbidden</body></html>"
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
            with self.assertRaises(websearch._NetworkError) as ctx:
                await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(ctx.exception.kind, "http")
        self.assertIn("403", ctx.exception.message)

    async def test_scrapling_fetch_status_string(self):
        # 冒烟发现（2026-08-06）：scrapling 200 时 status 为字符串 '200'，4xx 时 int——两者都要正确映射
        class _FakePage:
            status = "403"
            url = "https://8.8.8.8/page"
            body = b"<html><body>forbidden</body></html>"
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
            with self.assertRaises(websearch._NetworkError) as ctx:
                await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(ctx.exception.kind, "http")
        self.assertIn("403", ctx.exception.message)

    async def test_scrapling_fetch_too_large(self):
        class _FakePage:
            status = 200
            url = "https://8.8.8.8/page"
            body = b"x" * (10 * 1024 * 1024 + 1)
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
            with self.assertRaises(websearch._NetworkError) as ctx:
                await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(ctx.exception.kind, "too_large")

    async def test_scrapling_fetch_empty_text(self):
        class _FakePage:
            status = 200
            url = "https://8.8.8.8/page"
            body = b""
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
            with self.assertRaises(websearch._NetworkError) as ctx:
                await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(ctx.exception.kind, "provider")

    async def test_scrapling_fetch_impersonate_and_timeout(self):
        seen = {}
        class _FakePage:
            status = 200
            url = "https://8.8.8.8/page"
            body = ("<html><body><p>some sufficiently long body text for the extraction pipeline "
                    "to pass the threshold and produce markdown content here</p></body></html>").encode("utf-8")
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                seen["timeout"] = timeout
                seen["impersonate"] = impersonate
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
            await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(seen["impersonate"], "chrome")
        self.assertEqual(seen["timeout"], 15)
        os.environ["SCRAPLING_IMPERSONATE"] = "firefox"
        try:
            with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher):
                await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        finally:
            os.environ.pop("SCRAPLING_IMPERSONATE", None)
        self.assertEqual(seen["impersonate"], "firefox")

class TestWebFetchScraplingBrowserV34(unittest.IsolatedAsyncioTestCase):
    """v3.4.0：scrapling 浏览器重路径（HTTP 失败 → StealthyFetcher/DynamicFetcher 升级；mock hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {
            "WEB_FETCH_PROVIDER": os.environ.get("WEB_FETCH_PROVIDER"),
            "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
            "SCRAPLING_IMPERSONATE": os.environ.get("SCRAPLING_IMPERSONATE"),
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
            "WIGOLO_API_TOKEN": os.environ.get("WIGOLO_API_TOKEN"),
            "WIGOLO_PROBE_TIMEOUT": os.environ.get("WIGOLO_PROBE_TIMEOUT"),
        }
        for k in self._saved:
            os.environ.pop(k, None)
        websearch._reset_scrapling_cache()
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        websearch._reset_scrapling_cache()
        web_dir = DOCS_DIR / "web"
        if web_dir.is_dir():
            shutil.rmtree(web_dir)

    def _set_wigolo(self, url="http://127.0.0.1:3333"):
        os.environ["WIGOLO_REST_URL"] = url
        os.environ["WIGOLO_PROBE_TIMEOUT"] = "1"

    def _browser(self, available=True):
        p = patch("collab_mcp.websearch._scrapling_browser_available", new=AsyncMock(return_value=available))
        p.start()
        self.addCleanup(p.stop)

    EMPTY_HTML = b"<html><body></body></html>"
    _BODY = b"<html><head><title>H</title></head><body><p>some sufficiently long body text for the extraction pipeline to produce markdown here</p></body></html>"

    # 1) 浏览器不可用 → HTTP-only（HTTP 成功，fetch_method=http，浏览器不触发）
    async def test_browser_not_available_keeps_http_only(self):
        self._browser(False)
        class _FakePage:
            status = 200
            url = "https://8.8.8.8/page"
            body = self._BODY
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher), \
             patch("collab_mcp.websearch._scrapling_browser_fetch", new=AsyncMock()) as bfetch:
            final_url, title, markdown, fetch_method = await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(fetch_method, "http")
        bfetch.assert_not_called()

    # 2) HTTP 403 + 浏览器可用 → 浏览器成功（fetch_method=browser）
    async def test_http_403_escalates_to_browser(self):
        self._browser(True)
        class _FakePage403:
            status = 403
            url = "https://8.8.8.8/page"
            body = b"forbidden"
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage403()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher), \
             patch("collab_mcp.websearch._scrapling_browser_fetch", new=AsyncMock(
                 return_value=("https://8.8.8.8/final", "Browser Title", "browser extracted body content here"))):
            final_url, title, markdown, fetch_method = await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(fetch_method, "browser")
        self.assertEqual(title, "Browser Title")
        self.assertEqual(final_url, "https://8.8.8.8/final")

    # 3) HTTP 空正文 + 浏览器可用 → 浏览器成功
    async def test_http_empty_escalates_to_browser(self):
        self._browser(True)
        class _FakePageEmpty:
            status = 200
            url = "https://8.8.8.8/page"
            body = b""
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePageEmpty()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher), \
             patch("collab_mcp.websearch._scrapling_browser_fetch", new=AsyncMock(
                 return_value=("https://8.8.8.8/final", "B", "browser body text"))):
            _, _, _, fetch_method = await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(fetch_method, "browser")

    # 4) HTTP 成功 → 浏览器不触发
    async def test_http_success_no_browser_trigger(self):
        self._browser(True)
        class _FakePage:
            status = 200
            url = "https://8.8.8.8/page"
            body = self._BODY
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher), \
             patch("collab_mcp.websearch._scrapling_browser_fetch", new=AsyncMock()) as bfetch:
            _, _, _, fetch_method = await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        self.assertEqual(fetch_method, "http")
        bfetch.assert_not_called()

    # 5) HTTP 失败 + 浏览器失败 → 抛 HTTP 层原始错误（403 保留）
    async def test_browser_fail_raises_http_error(self):
        self._browser(True)
        class _FakePage403:
            status = 403
            url = "https://8.8.8.8/page"
            body = b"forbidden"
        class _FakeFetcher:
            @classmethod
            def get(cls, url, timeout=None, impersonate=None):
                return _FakePage403()
        with patch("collab_mcp.websearch._SCRAPLING_FETCHER", _FakeFetcher), \
             patch("collab_mcp.websearch._scrapling_browser_fetch", new=AsyncMock(
                 side_effect=websearch._NetworkError("timeout", "浏览器超时"))):
            with self.assertRaises(websearch._NetworkError) as ctx:
                await websearch._scrapling_fetch("https://8.8.8.8/page", 15)
        # 中1（PC-C 1d8b49c7aee7）：HTTP 层 403 保留优先，浏览器 timeout 不覆盖
        self.assertEqual(ctx.exception.kind, "http")
        self.assertIn("403", ctx.exception.message)

    # 6) 链：stdlib 失败 + scrapling 浏览器成功 → provider=scrapling/fetch_method=browser，wigolo 不试
    async def test_chain_scrapling_browser_success(self):
        self._browser(True)
        with patch("collab_mcp.websearch._stdlib_fetch", side_effect=websearch._NetworkError("network", "网络请求失败")), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(
                 return_value=("https://8.8.8.8/final", "B", "browser body text", "browser"))), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")
        self.assertEqual(r["fetch_method"], "browser")
        wfetch.assert_not_called()

    # 7) 链：scrapling 无浏览器 + scrapling 失败 → wigolo 兜底（v3.3 回归）
    async def test_chain_wigolo_when_scrapling_no_browser(self):
        self._set_wigolo()
        self._browser(False)
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(
                 side_effect=websearch._NetworkError("http", "scrapling 403"))), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock(return_value=("W", "wigolo body"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")

    # 8) 链：scrapling 有浏览器但失败 → 跳过 wigolo（避免 4 级串行长尾）
    async def test_chain_wigolo_skipped_when_scrapling_browser_fails(self):
        self._set_wigolo()
        self._browser(True)
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/page", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(
                 side_effect=websearch._NetworkError("http", "scrapling 403"))), \
             patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        wfetch.assert_not_called()

    # 9) 链：SSRF 前置（浏览器路径同样拒绝内网 URL，不发起任何请求）
    async def test_ssrf_rejected_before_browser(self):
        self._browser(True)
        with patch("collab_mcp.websearch._stdlib_fetch", new=AsyncMock()) as sfetch, \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock()) as scfetch, \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("http://127.0.0.1:22/"))
        self.assertFalse(r["success"], r)
        self.assertIn("拒绝", r["error"])
        sfetch.assert_not_called()
        scfetch.assert_not_called()
        wfetch.assert_not_called()

    # 10) 链：scrapling 浏览器重定向 final_url 复查拒绝
    async def test_browser_redirect_final_url_rechecked(self):
        self._browser(True)
        with patch("collab_mcp.websearch._stdlib_fetch", side_effect=websearch._NetworkError("network", "网络请求失败")), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(
                 return_value=("http://127.0.0.1/x", "T", "body", "browser"))), \
             patch("collab_mcp.websearch._wigolo_fetch", new=AsyncMock()) as wfetch:
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertFalse(r["success"], r)
        self.assertIn("重定向目标被拒绝", r["error"])
        wfetch.assert_not_called()

    # 11) 链：入库 extra_meta.fetch_method=browser + 检索联动
    async def test_ingest_fetch_method_audit(self):
        self._browser(True)
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/q", self.EMPTY_HTML)), \
             patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(
                 return_value=("https://8.8.8.8/final", "Quantum Basics", "# Quantum Basics\n\nQuantum computing uses qubits. Entanglement is a key resource.", "browser"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")
        self.assertEqual(r["fetch_method"], "browser")
        res = _parse(await search.search_documents("entanglement"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["title"], "Quantum Basics")

    # 12) 强制 scrapling + 浏览器路径 → provider=scrapling/fetch_method=browser
    async def test_forced_scrapling_browser(self):
        self._browser(True)
        os.environ["WEB_FETCH_PROVIDER"] = "scrapling"
        with patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._scrapling_fetch", new=AsyncMock(
                 return_value=("https://8.8.8.8/final", "Forced", "forced body", "browser"))):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "scrapling")
        self.assertEqual(r["fetch_method"], "browser")

class TestWebResearchWigoloV35(unittest.IsolatedAsyncioTestCase):
    """v3.5.0：wigolo research/agent 工具（web_research/web_agent；mock /v1/research、/v1/agent，hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
            "WIGOLO_API_TOKEN": os.environ.get("WIGOLO_API_TOKEN"),
            "WIGOLO_PROBE_TIMEOUT": os.environ.get("WIGOLO_PROBE_TIMEOUT"),
        }
        for k in self._saved:
            os.environ.pop(k, None)
        websearch._reset_wigolo_cache()

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        websearch._reset_wigolo_cache()

    def _set_wigolo(self, url="http://127.0.0.1:3333"):
        os.environ["WIGOLO_REST_URL"] = url
        os.environ["WIGOLO_PROBE_TIMEOUT"] = "1"

    def _research_body(self, heuristic=False):
        report = ("## Brief\n\n**Summary (heuristic):** no substantive findings." if heuristic
                  else "## Brief\n\nQuantum entanglement is a key resource in quantum algorithms.")
        return json.dumps({
            "report": report,
            "citations": [{"index": 1, "url": "https://example.com/1", "title": "T1", "snippet": "s1"}],
            "sources": [{"url": "https://example.com/1", "title": "T1"}],
        }).encode("utf-8")

    def _agent_body(self):
        return json.dumps({
            "result": "## Findings\n\nGathered data with sources.",
            "citations": [{"index": 1, "url": "https://example.com/a", "title": "A", "snippet": "sa"}],
            "sources": [{"url": "https://example.com/a", "title": "A"}],
        }).encode("utf-8")

    # 1) 未配置 wigolo → 明确失败（research/agent 一致）
    async def test_not_configured_fails(self):
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=False)):
            r = _parse(await websearch.web_research("q"))
        self.assertFalse(r["success"], r)
        self.assertIn("wigolo", r["error"])
        r2 = _parse(await websearch.web_agent("p"))
        self.assertFalse(r2["success"], r2)
        self.assertIn("wigolo", r2["error"])

    # 2) research 成功映射（report/citations/sources 结构）
    async def test_research_success_mapping(self):
        self._set_wigolo()
        async def fake_fetch(url, timeout, **kw):
            return (url, self._research_body(heuristic=False))
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("quantum entanglement"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")
        self.assertEqual(r["mode"], "research")
        self.assertEqual(r["depth"], "standard")
        self.assertIn("quantum", r["report"])
        self.assertEqual(r["citations"][0]["url"], "https://example.com/1")
        self.assertEqual(r["sources_count"], 1)
        self.assertFalse(r["heuristic"])

    # 3) depth 非法 → 钳制 standard；max_sources 钳制上限 50
    async def test_research_clamps(self):
        self._set_wigolo()
        seen = {}
        async def fake_fetch(url, timeout, **kw):
            seen["payload"] = json.loads(kw.get("data", b"{}"))
            return (url, self._research_body())
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q", depth="bogus", max_sources=999))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["depth"], "standard")
        self.assertEqual(seen["payload"]["max_sources"], 50)

    # 4) schema 400 → 明确失败（契约错误）
    async def test_research_schema_400_fails(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=AsyncMock(
                 side_effect=websearch._NetworkError("http", "外部服务返回 HTTP 400"))):
            r = _parse(await websearch.web_research("q"))
        self.assertFalse(r["success"], r)
        self.assertIn("契约", r["error"])

    # 5) heuristic 报告（无 LLM 兜底）→ heuristic=True
    async def test_research_heuristic_flag(self):
        self._set_wigolo()
        async def fake_fetch(url, timeout, **kw):
            return (url, self._research_body(heuristic=True))
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        self.assertTrue(r["heuristic"])

    # 6) agent 成功映射（prompt/urls/max_pages 传递）
    async def test_agent_success_mapping(self):
        self._set_wigolo()
        seen = {}
        async def fake_fetch(url, timeout, **kw):
            seen["payload"] = json.loads(kw.get("data", b"{}"))
            return (url, self._agent_body())
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_agent("gather data about X", urls="https://example.com/seed", max_pages=3))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["mode"], "agent")
        self.assertEqual(seen["payload"]["prompt"], "gather data about X")
        self.assertEqual(seen["payload"]["urls"], ["https://example.com/seed"])
        self.assertEqual(seen["payload"]["max_pages"], 3)
        self.assertEqual(r["sources_count"], 1)

    # 7) agent 种子 URL SSRF 拒绝（不传给 wigolo）
    async def test_agent_seed_url_ssrf_rejected(self):
        self._set_wigolo()
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=AsyncMock()) as ff:
            r = _parse(await websearch.web_agent("p", urls="http://127.0.0.1:22/"))
        self.assertFalse(r["success"], r)
        self.assertIn("种子 URL 被拒绝", r["error"])
        ff.assert_not_called()

    # 8) max_pages/max_time_ms 钳制
    async def test_agent_clamps(self):
        self._set_wigolo()
        seen = {}
        async def fake_fetch(url, timeout, **kw):
            seen["payload"] = json.loads(kw.get("data", b"{}"))
            return (url, self._agent_body())
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_agent("p", max_pages=999, max_time_ms=1000))
        self.assertTrue(r["success"], r)
        self.assertEqual(seen["payload"]["max_pages"], 100)
        self.assertEqual(seen["payload"]["max_time_ms"], 5000)

    # 8b) 低1（PC-C）：空 {} 响应 → 明确失败（不静默空成功）
    async def test_empty_response_fails(self):
        self._set_wigolo()
        async def fake_fetch(url, timeout, **kw):
            return (url, b"{}")
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
            self.assertFalse(r["success"], r)
            self.assertIn("空结果", r["error"])
            r2 = _parse(await websearch.web_agent("p"))
            self.assertFalse(r2["success"], r2)
            self.assertIn("空结果", r2["error"])

    # 9) 身份闸门（REQUIRE_IDENTITY=1 无身份被拒）
    async def test_identity_guard(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await websearch.web_research("q"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
            r2 = _parse(await websearch.web_agent("p"))
            self.assertFalse(r2["success"])
            self.assertIn("需要调用者身份", r2["error"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved

    # 10) depth 预算为生效下限（comprehensive → timeout>=120）
    async def test_research_depth_budget(self):
        self._set_wigolo()
        seen = {}
        async def fake_fetch(url, timeout, **kw):
            seen["timeout"] = timeout
            return (url, self._research_body())
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q", depth="comprehensive"))
        self.assertTrue(r["success"], r)
        self.assertGreaterEqual(seen["timeout"], 150)

    # 11) 输出结构齐全（provider/mode/report/citations/sources_count/heuristic/chars/estimated_tokens）
    async def test_output_structure(self):
        self._set_wigolo()
        async def fake_fetch(url, timeout, **kw):
            return (url, self._agent_body())
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_agent("p"))
        for key in ("provider", "mode", "report", "truncated", "citations", "sources_count", "heuristic", "chars", "estimated_tokens"):
            self.assertIn(key, r)

class TestWebIngestV290(unittest.IsolatedAsyncioTestCase):
    """v2.9.0：网页抓取入库增强——正文清洗 / 去重统计 / 元数据 / 检索联动（mock 网络，hermetic）。"""

    _PAGE = (
        b"<html><head><title>Quantum Basics</title></head><body>"
        b"<h1>Quantum Basics Guide</h1>"
        b"<p>Quantum computing uses qubits for computation. Entanglement is a key resource in quantum algorithms.</p>"
        b"<p>This second paragraph explains error correction in more detail.</p>"
        b"</body></html>"
    )
    _NOISY_HTML = (
        "<html><head><title>Article Title X</title></head><body>"
        "<nav id='main-nav'><a href='/'>Home</a> <a href='/about'>About</a> "
        "<a href='/pricing'>Pricing</a></nav>"
        "<div class='ad'>BUY NOW cheap watches BUY NOW</div>"
        "<header><h1>Real Article Heading</h1></header>"
        "<article>"
        "<p>This is the first real paragraph of the article. It contains substantial meaningful content about topic alpha and beta.</p>"
        "<p>The second paragraph continues the discussion with more details and examples for the reader to understand.</p>"
        "<ul><li>point one about alpha</li><li>point two about beta</li></ul>"
        "</article>"
        "<footer><a href='/privacy'>Privacy</a> <a href='/terms'>Terms</a> Copyright 2026</footer>"
        "</body></html>"
    )

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_key = os.environ.get("FIRECRAWL_API_KEY")
        os.environ.pop("FIRECRAWL_API_KEY", None)
        # v3.3.0：本类不测 scrapling——钉死探测 False，保证 hermetic
        self._scrapling_off = patch("collab_mcp.websearch._scrapling_available", new=AsyncMock(return_value=False))
        self._scrapling_off.start()
        self.addCleanup(self._scrapling_off.stop)
        # 本轮去重计数是进程级状态，必须清零保证 hermetic
        documents._SESSION_DEDUP.clear()
        if DOCS_DIR.is_dir():
            for f in list(DOCS_DIR.rglob("*.md")):
                f.unlink()

    async def asyncTearDown(self):
        if self._saved_key is None:
            os.environ.pop("FIRECRAWL_API_KEY", None)
        else:
            os.environ["FIRECRAWL_API_KEY"] = self._saved_key
        if DOCS_DIR.is_dir():
            for f in list(DOCS_DIR.rglob("*.md")):
                f.unlink()
        documents._SESSION_DEDUP.clear()

    # ---------- 正文清洗 ----------
    async def test_body_extraction_strips_noise(self):
        body = websearch._extract_body(self._NOISY_HTML)
        self.assertIn("# Real Article Heading", body)
        self.assertIn("first real paragraph", body)
        self.assertIn("second paragraph", body)
        self.assertIn("- point one about alpha", body)
        # 导航/广告/页脚噪声应被剔除
        self.assertNotIn("Home", body)
        self.assertNotIn("Pricing", body)
        self.assertNotIn("BUY NOW", body)
        self.assertNotIn("Privacy", body)

    async def test_body_extraction_fallback_short_page(self):
        # 打分结果总长过短 → 回退全文本，兼容简单页面
        body = websearch._extract_body("<html><body>abc</body></html>")
        self.assertEqual(body, "abc")

    async def test_body_extraction_skips_link_lists(self):
        # 纯链接列表（高链接密度）不得入选主正文
        html = (
            "<html><body>"
            "<ul><li><a href='/x'>Alpha</a></li><li><a href='/y'>Beta</a></li>"
            "<li><a href='/z'>Gamma</a></li><li><a href='/w'>Delta</a></li>"
            "<li><a href='/v'>Epsilon</a></li></ul>"
            "<p>Main article first paragraph with real substantive content about science topics.</p>"
            "<p>Main article second paragraph adding more meaningful details and examples here.</p>"
            "</body></html>"
        )
        body = websearch._extract_body(html)
        self.assertIn("Main article first paragraph", body)
        self.assertIn("Main article second paragraph", body)
        self.assertNotIn("Alpha", body)
        self.assertNotIn("Epsilon", body)

    # ---------- URL 归一 ----------
    async def test_normalize_url_strips_tracking_and_fragment(self):
        from collab_mcp.utils import normalize_url
        self.assertEqual(
            normalize_url("https://Example.COM:443/a/b?utm_source=x&b=2&a=1#sec"),
            "https://example.com/a/b?a=1&b=2",
        )
        self.assertEqual(
            normalize_url("https://e.com/x?fbclid=1&q=hi&utm_medium=email"),
            "https://e.com/x?q=hi",
        )
        self.assertEqual(
            normalize_url("http://e.com/x#frag"), normalize_url("http://e.com/x")
        )
        self.assertEqual(
            normalize_url("https://e.com/x"), normalize_url("https://e.com:443/x")
        )

    async def test_fetch_raw_decompresses_gzip(self):
        # v2.9.0：python.org 等站点对无 Accept-Encoding 的请求也返回 gzip，
        # urllib 不自动解压 → _fetch_raw 必须解压（真实冒烟发现）
        import gzip

        class _FakeResp:
            def __init__(self, headers, body, url="https://8.8.8.8/x"):
                self._headers, self._body, self._url = headers, body, url
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, n=-1):
                return self._body
            def geturl(self):
                return self._url
            @property
            def headers(self):
                return self._headers

        raw = "<html><body><p>gzip 压缩正文 hello world</p></body></html>".encode("utf-8")
        payload = gzip.compress(raw)
        fake = _FakeResp({"Content-Encoding": "gzip"}, payload)
        with patch("collab_mcp.websearch.urllib.request.urlopen", return_value=fake):
            final_url, body = await websearch._fetch_raw("https://8.8.8.8/x", 15)
        self.assertEqual(final_url, "https://8.8.8.8/x")
        self.assertEqual(body, raw)

    async def test_decompress_bomb_capped(self):
        # PC-C 验证 M2：解压炸弹防护——高压缩比 gzip 在累计上限处必须中止
        import gzip
        payload = gzip.compress(b"x" * 200_000)  # 压缩后很小，解压后远超 limit
        with self.assertRaises(websearch._NetworkError) as ctx:
            websearch._decompress_bytes(payload, "gzip", limit=1000)
        self.assertEqual(ctx.exception.kind, "too_large")
        # 正常小体量解压不受影响
        raw = "hello gzip 正文".encode("utf-8")
        self.assertEqual(
            websearch._decompress_bytes(gzip.compress(raw), "gzip", limit=1_000_000),
            raw,
        )

    async def test_extra_meta_cannot_override_core_fields(self):
        # PC-C 验证 M1：estimated_tokens 等核心字段不被 extra_meta 覆盖
        src = COLLAB_DIR / "core.txt"
        src.write_text("核心字段保护测试内容", encoding="utf-8")
        r = _parse(await documents.add_document(
            str(src), skip_duplicates=True,
            extra_meta={"estimated_tokens": 999, "chars": 1, "source": "hacked", "title": "T"},
        ))
        self.assertTrue(r["success"], r)
        out = DOCS_DIR / "core.md"
        meta = json.loads(out.read_text(encoding="utf-8").split("---")[1])
        self.assertNotEqual(meta["estimated_tokens"], 999)
        self.assertNotEqual(meta["chars"], 1)
        self.assertNotEqual(meta["source"], "hacked")
        self.assertEqual(meta["title"], "T")

    async def test_extra_meta_non_serializable_rejected(self):
        # PC-C 验证 L4：非 JSON 序列化 extra_meta 提前拒绝
        src = COLLAB_DIR / "badmeta.txt"
        src.write_text("内容", encoding="utf-8")
        r = _parse(await documents.add_document(str(src), extra_meta={"bad": object()}))
        self.assertFalse(r["success"])
        self.assertIn("序列化", r["error"])

    # ---------- 元数据增强 ----------
    async def test_fetch_metadata_enhanced(self):
        with patch(
            "collab_mcp.websearch._stdlib_fetch",
            return_value=("https://8.8.8.8/page", self._PAGE),
        ):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/page?utm_source=x#sec"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["host"], "8.8.8.8")
        self.assertEqual(r["provider"], "stdlib")
        self.assertEqual(r["normalized_url"], "https://8.8.8.8/page")
        self.assertTrue(r["fetched_at"])
        self.assertTrue(r["dedup_key"])
        saved_file = DOCS_DIR / "web" / r["saved"]["filename"]
        self.assertTrue(saved_file.exists())
        meta = json.loads(saved_file.read_text(encoding="utf-8").split("---")[1])
        for k in ("title", "url", "host", "provider", "fetched_at", "body_length", "dedup_key"):
            self.assertIn(k, meta, k)
        # search_documents 已读取的核心字段不得被破坏
        for k in ("source", "source_path", "format", "method", "chars", "sha256_16", "ingested_by", "ingested_at"):
            self.assertIn(k, meta, k)
        self.assertEqual(meta["url"], "https://8.8.8.8/page?utm_source=x#sec")
        self.assertEqual(meta["host"], "8.8.8.8")
        self.assertEqual(meta["format"], "md")
        self.assertGreaterEqual(meta["chars"], 1)

    # ---------- 去重统计 ----------
    async def test_fetch_dedup_same_content_different_url(self):
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/a", self._PAGE)):
            r1 = _parse(await websearch.web_fetch("https://8.8.8.8/a"))
        self.assertFalse(r1["saved"]["duplicate"])
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/b", self._PAGE)):
            r2 = _parse(await websearch.web_fetch("https://8.8.8.8/b"))
        self.assertTrue(r2["saved"]["duplicate"], r2)
        self.assertGreaterEqual(r2["saved"]["duplicate_count"], 1)
        self.assertEqual(r2["saved"]["path"], r1["saved"]["path"])
        self.assertEqual(len(list((DOCS_DIR / "web").glob("*.md"))), 1)

    async def test_fetch_dedup_same_url_different_tracking_params(self):
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/a", self._PAGE)):
            r1 = _parse(await websearch.web_fetch("https://8.8.8.8/a?utm_source=news#read"))
        self.assertFalse(r1["saved"]["duplicate"])
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/a", self._PAGE)):
            r2 = _parse(await websearch.web_fetch("https://8.8.8.8/a"))
        self.assertTrue(r2["saved"]["duplicate"], r2)
        self.assertGreaterEqual(r2["saved"]["duplicate_count"], 1)
        self.assertEqual(r2["saved"]["path"], r1["saved"]["path"])

    async def test_fetch_dedup_session_counting(self):
        r = None
        for _ in range(3):
            with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/a", self._PAGE)):
                r = _parse(await websearch.web_fetch("https://8.8.8.8/a"))
        # 第 1 次摄入，第 2/3 次重复；本轮计数递增，累计计数不变（仅 1 份文件）
        self.assertTrue(r["saved"]["duplicate"])
        self.assertEqual(r["saved"]["duplicate_count"], 1)
        self.assertGreaterEqual(r["saved"]["session_duplicates"], 2)

    # ---------- 检索联动 ----------
    async def test_search_hits_web_fetched_doc(self):
        with patch("collab_mcp.websearch._stdlib_fetch", return_value=("https://8.8.8.8/q", self._PAGE)):
            r = _parse(await websearch.web_fetch("https://8.8.8.8/q"))
        self.assertTrue(r["success"], r)
        res = _parse(await search.search_documents("entanglement"))
        self.assertTrue(res["success"], res)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["filename"], r["saved"]["filename"])
        # 标题来自清洗后正文的 h1
        self.assertEqual(res["results"][0]["title"], "Quantum Basics Guide")

    # ---------- add_document 直接扩展（非 web）----------
    async def test_add_document_extra_meta_and_dedup_key(self):
        src = COLLAB_DIR / "meta.txt"
        src.write_text("同内容不同文件名 dedup", encoding="utf-8")
        r1 = _parse(await documents.add_document(
            str(src), skip_duplicates=True,
            dedup_key="abc123", extra_meta={"title": "T", "url": "https://e.com/x"},
        ))
        self.assertTrue(r1["success"], r1)
        self.assertEqual(r1["dedup_key"], "abc123")
        src2 = COLLAB_DIR / "meta2.txt"
        src2.write_text("同内容不同文件名 dedup", encoding="utf-8")
        r2 = _parse(await documents.add_document(
            str(src2), skip_duplicates=True,
            dedup_key="abc123", extra_meta={"title": "T2", "url": "https://e.com/y"},
        ))
        self.assertTrue(r2["success"], r2)
        self.assertTrue(r2["duplicate"], r2)
        self.assertGreaterEqual(r2["duplicate_count"], 1)
        out = DOCS_DIR / "meta.md"
        meta = json.loads(out.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(meta["title"], "T")
        self.assertEqual(meta["url"], "https://e.com/x")
        self.assertEqual(meta["format"], "md")
        self.assertIn("chars", meta)
        self.assertIn("source", meta)


class TestMediaTranscribeV300(unittest.IsolatedAsyncioTestCase):
    """v3.0：媒体转写入库 transcribe_media（mock 后端，hermetic，不依赖真实音频/网络）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        documents._SESSION_DEDUP.clear()
        if DOCS_DIR.is_dir():
            for f in list(DOCS_DIR.rglob("*.md")):
                f.unlink()

    async def asyncTearDown(self):
        if self._saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved_key
        if DOCS_DIR.is_dir():
            for f in list(DOCS_DIR.rglob("*.md")):
                f.unlink()
        documents._SESSION_DEDUP.clear()

    def _media(self, name="meeting.mp3"):
        src = COLLAB_DIR / name
        src.write_bytes(b"ID3\x03\x00fake audio payload for hermetic tests")
        return src

    async def test_transcribe_success_and_ingest(self):
        src = self._media()
        with patch("collab_mcp.media._detect_backend", return_value="faster-whisper"), patch(
            "collab_mcp.media._run_faster_whisper",
            return_value=("这是一段会议转写文本，包含独特检索词 zzzmediatoken", "small"),
        ):
            r = _parse(await media.transcribe_media(str(src)))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["backend"], "faster-whisper")
        self.assertEqual(r["model"], "small")
        self.assertEqual(r["media_type"], "mp3")
        self.assertIn("zzzmediatoken", r["transcript_preview"])
        self.assertTrue(r["saved"])
        self.assertTrue(str(r["saved"]["path"]).startswith("documents/media/"))
        saved_file = DOCS_DIR / "media" / r["saved"]["filename"]
        self.assertTrue(saved_file.exists())
        meta = json.loads(saved_file.read_text(encoding="utf-8").split("---")[1])
        for k in ("media_file", "media_type", "backend", "model", "language", "transcript_length", "dedup_key"):
            self.assertIn(k, meta, k)
        for k in ("source", "source_path", "format", "method", "chars", "ingested_by", "ingested_at"):
            self.assertIn(k, meta, k)
        self.assertEqual(meta["media_file"], "meeting.mp3")
        self.assertEqual(meta["backend"], "faster-whisper")
        self.assertEqual(meta["format"], "md")

    async def test_transcribe_search_hit(self):
        src = self._media("lecture.wav")
        with patch("collab_mcp.media._detect_backend", return_value="faster-whisper"), patch(
            "collab_mcp.media._run_faster_whisper",
            return_value=("量子计算讲座转写 zzzquantumtoken 内容", "small"),
        ):
            r = _parse(await media.transcribe_media(str(src)))
        self.assertTrue(r["success"], r)
        res = _parse(await search.search_documents("zzzquantumtoken"))
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["filename"], r["saved"]["filename"])
        self.assertTrue(res["results"][0]["path"].startswith("documents/media/"))

    async def test_transcribe_detects_backend_order(self):
        # faster-whisper 缺 → whisper 在
        with patch(
            "collab_mcp.media._backend_available",
            side_effect=lambda name: name == "whisper",
        ):
            self.assertEqual(media._detect_backend(""), "whisper")
        # 显式指定但不可用 → 空
        with patch("collab_mcp.media._backend_available", return_value=False):
            self.assertEqual(media._detect_backend("faster-whisper"), "")
        # 非法后端名 → 空
        self.assertEqual(media._detect_backend("bogus"), "")

    async def test_transcribe_no_backend_clear_error(self):
        src = self._media()
        with patch("collab_mcp.media._detect_backend", return_value=""):
            r = _parse(await media.transcribe_media(str(src)))
        self.assertFalse(r["success"])
        self.assertIn("转写后端不可用", r["error"])

    async def test_transcribe_backend_failure_mapped(self):
        src = self._media()
        with patch("collab_mcp.media._detect_backend", return_value="faster-whisper"), patch(
            "collab_mcp.media._run_faster_whisper",
            side_effect=RuntimeError("模型加载失败"),
        ):
            r = _parse(await media.transcribe_media(str(src)))
        self.assertFalse(r["success"])
        self.assertIn("媒体转写失败", r["error"])

    async def test_transcribe_save_failure_mapped(self):
        # PC-C L1：入库块（add_document）抛异常 → 干净 fail()，不向上抛原始异常
        from unittest.mock import AsyncMock
        src = self._media()
        with patch("collab_mcp.media._detect_backend", return_value="faster-whisper"), patch(
            "collab_mcp.media._run_faster_whisper",
            return_value=("转写内容 zzzsavefail", "small"),
        ), patch(
            "collab_mcp.media.add_document",
            new=AsyncMock(side_effect=OSError("disk full")),
        ):
            r = _parse(await media.transcribe_media(str(src)))
        self.assertFalse(r["success"])
        self.assertIn("转写文本入库失败", r["error"])

    async def test_transcribe_openai_backend(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        src = self._media("podcast.m4a")
        with patch("collab_mcp.media._detect_backend", return_value="openai"), patch(
            "collab_mcp.media._run_openai",
            return_value=("cloud transcription zzzcloudtoken", "whisper-1"),
        ):
            r = _parse(await media.transcribe_media(str(src), backend="openai"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["backend"], "openai")
        self.assertEqual(r["model"], "whisper-1")

    async def test_transcribe_empty_transcript_rejected(self):
        src = self._media()
        with patch("collab_mcp.media._detect_backend", return_value="whisper"), patch(
            "collab_mcp.media._run_whisper", return_value=("", "small"),
        ):
            r = _parse(await media.transcribe_media(str(src)))
        self.assertFalse(r["success"])
        self.assertIn("未能从媒体提取到转写文本", r["error"])

    async def test_transcribe_path_safety_and_missing(self):
        r = _parse(await media.transcribe_media("../escape/x.mp3"))
        self.assertFalse(r["success"])
        self.assertIn("共享目录范围", r["error"])
        r2 = _parse(await media.transcribe_media(str(COLLAB_DIR / "nope.mp3")))
        self.assertFalse(r2["success"])
        self.assertIn("源文件不存在", r2["error"])

    async def test_transcribe_dedup_same_file(self):
        src = self._media("dup.mp3")
        with patch("collab_mcp.media._detect_backend", return_value="faster-whisper"), patch(
            "collab_mcp.media._run_faster_whisper",
            return_value=("同一段转写内容 zzzduptoken", "small"),
        ):
            r1 = _parse(await media.transcribe_media(str(src)))
            r2 = _parse(await media.transcribe_media(str(src)))
        self.assertFalse(r1["saved"]["duplicate"])
        self.assertTrue(r2["saved"]["duplicate"], r2)
        self.assertEqual(r2["saved"]["path"], r1["saved"]["path"])
        self.assertEqual(len(list((DOCS_DIR / "media").glob("*.md"))), 1)

    async def test_transcribe_identity_guard(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await media.transcribe_media("x.mp3"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved



class CliTest(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        self.env["COLLAB_DIR"] = str(_TEST_DIR)

    def test_version_cli(self):
        p = subprocess.run(
            [sys.executable, str(REPO_DIR / "server.py"), "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.env, timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"v{__version__}", p.stdout)

    def test_health_cli_utf8_safe(self):
        p = subprocess.run(
            [sys.executable, str(REPO_DIR / "server.py"), "--health"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.env, timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("协作目录", p.stdout)
        self.assertIn("✅", p.stdout)  # Windows GBK 控制台也能输出 emoji
        self.assertIn("sqlite3", p.stdout)


class UtilsTest(unittest.TestCase):
    def test_atomic_write_leaves_no_tmp(self):
        target = _TEST_DIR / "atomic_test.json"
        self.assertTrue(safe_write_json(target, {"a": 1, "中文": "值"}))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["a"], 1)
        # 不应残留临时文件
        leftovers = [f for f in _TEST_DIR.glob("*.tmp") if f.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_missing_file_silent_corrupt_still_warns(self):
        # v3.36.2：文件不存在 → None 且零 WARNING；内容损坏 → None 且保留 WARNING
        missing = _TEST_DIR / "definitely_missing_3362.json"
        with self.assertNoLogs("collab-mcp", level="WARNING"):
            self.assertIsNone(safe_read_json(missing))

        broken = _TEST_DIR / "broken_3362.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertLogs("collab-mcp", level="WARNING") as cap:
            self.assertIsNone(safe_read_json(broken))
        self.assertTrue(any("broken_3362" in r.getMessage() for r in cap.records))


class StdioProtocolTest(unittest.TestCase):
    """真实 MCP stdio 协议端到端测试（initialize → tools/call，含中文）。"""

    def _spawn(self):
        env = dict(os.environ)
        env["COLLAB_DIR"] = str(_TEST_DIR)
        proc = subprocess.Popen(
            [sys.executable, str(REPO_DIR / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env,
        )
        return proc

    def _send(self, proc, msg: dict) -> None:
        payload = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(payload)
        proc.stdin.flush()

    def _read_responses(self, proc, expected_ids: set, timeout: float = 30) -> tuple:
        """并发读取 stdio 响应，直到收齐 expected_ids（或超时）。

        v2.8.2（CI 修复）：旧实现发送请求后立即 proc.stdin.close() 触发 EOF，
        MCP stdio 服务端收到 EOF 后关闭事件循环，可能来不及写出最后一条响应
        （CI/本地实测约 5% 概率丢 id=2 → KeyError: 'result'）。真实客户端
        （Claude）整个会话期间保持 stdin 打开，这里与之一致：不关 stdin，
        先并发读响应，收齐后由调用方 kill 子进程结束会话。
        """
        responses: dict = {}
        errors: list = []

        def reader():
            try:
                for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        # 服务器偶发输出非 JSON 行（如启动期日志）→ 跳过，不崩测试
                        continue
                    if msg.get("id") is not None:
                        responses[msg["id"]] = msg
                        if expected_ids.issubset(responses):
                            return
            except Exception as e:  # pragma: no cover
                errors.append(e)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return responses, errors

    def test_initialize_and_create_task_with_chinese(self):
        _reset_collab_dir()
        proc = self._spawn()
        try:
            self._send(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "unittest", "version": "1.0"},
                },
            })
            self._send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._send(proc, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "create_task",
                    "arguments": {
                        "task_title": "中文协议测试",
                        "task_content": "验证 UTF-8 传输",
                    },
                },
            })
            # 不要先关 stdin（见 _read_responses 说明）：并发读响应，收齐后 kill
            responses, errors = self._read_responses(proc, expected_ids={1, 2})
            self.assertTrue(
                {1, 2}.issubset(responses),
                f"stdio 未收到全部响应: ids={sorted(responses)} errors={errors}",
            )
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
            if proc.stdout:
                proc.stdout.close()
            proc.wait(timeout=60)

        init = responses.get(1, {})
        self.assertEqual(
            init["result"]["serverInfo"]["version"], __version__
        )

        call = responses.get(2, {})
        self.assertFalse(call["result"]["isError"])
        text = call["result"]["content"][0]["text"]
        data = json.loads(text)
        self.assertTrue(data["success"])
        self.assertEqual(data["message"], "任务「中文协议测试」已创建")

        # 落盘文件里的中文必须完好
        saved = json.loads(
            (INBOX_DIR / f"{data['task_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["title"], "中文协议测试")


class FileAccessTest(unittest.IsolatedAsyncioTestCase):
    """list_shared_dir / read_shared_file 的路径安全与读写测试。"""

    async def asyncSetUp(self):
        # 在共享根（测试临时目录的上一级）里建一个受控子目录
        self.shared_root = COLLAB_DIR.parent
        # "000_" 前缀保证在根目录排序后位于前 200 项内
        self.subdir = self.shared_root / f"000_collab_test_{os.getpid()}"
        self.subdir.mkdir(parents=True, exist_ok=True)
        (self.subdir / "guide.md").write_text("# 测试指南\n中文内容", encoding="utf-8")
        (self.subdir / "data.txt").write_text("hello", encoding="utf-8")

    async def asyncTearDown(self):
        for f in self.subdir.glob("*"):
            f.unlink()
        self.subdir.rmdir()

    async def test_list_shared_dir(self):
        r = _parse(await files.list_shared_dir(self.subdir.name))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        names = {e["name"] for e in r["entries"]}
        self.assertEqual(names, {"guide.md", "data.txt"})

    async def test_list_shared_root(self):
        r = _parse(await files.list_shared_dir(""))
        self.assertTrue(r["success"])
        self.assertIn(self.subdir.name, {e["name"] for e in r["entries"]})

    async def test_read_shared_file(self):
        r = _parse(await files.read_shared_file(f"{self.subdir.name}/guide.md"))
        self.assertTrue(r["success"])
        self.assertIn("中文内容", r["content"])

    async def test_read_missing_file_fails(self):
        r = _parse(await files.read_shared_file(f"{self.subdir.name}/nope.md"))
        self.assertFalse(r["success"])
        self.assertIn("文件不存在", r["error"])

    async def test_path_traversal_blocked(self):
        r = _parse(await files.read_shared_file("../secret.txt"))
        self.assertFalse(r["success"])
        self.assertIn("超出共享文件夹范围", r["error"])

    async def test_absolute_path_rejected(self):
        r = _parse(await files.read_shared_file(str(self.subdir / "guide.md")))
        self.assertFalse(r["success"])
        self.assertIn("相对路径", r["error"])


class NotifyDaemonTest(unittest.IsolatedAsyncioTestCase):
    """notify_daemon 扫描逻辑与 get_notifications 工具测试。"""

    def setUp(self):
        self.collab = Path(tempfile.mkdtemp(prefix="collab_notify_"))
        (self.collab / "inbox").mkdir()
        (self.collab / "chat").mkdir()
        self.state = self.collab / "notifications" / "state.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.collab, ignore_errors=True)

    def test_default_collab_dir_prefers_env(self):
        saved = os.environ.get("COLLAB_DIR")
        try:
            os.environ["COLLAB_DIR"] = str(self.collab)
            self.assertEqual(notify_daemon._default_collab_dir(), self.collab)
        finally:
            if saved is None:
                os.environ.pop("COLLAB_DIR", None)
            else:
                os.environ["COLLAB_DIR"] = saved

    def test_default_collab_dir_falls_back_to_script_dir(self):
        saved = os.environ.get("COLLAB_DIR")
        saved_vm = notify_daemon.VM_DEFAULT_DIR
        try:
            os.environ.pop("COLLAB_DIR", None)
            notify_daemon.VM_DEFAULT_DIR = str(self.collab / "no_such_vm_mount")
            self.assertEqual(
                notify_daemon._default_collab_dir(),
                Path(notify_daemon.__file__).resolve().parent,
            )
        finally:
            if saved is None:
                os.environ.pop("COLLAB_DIR", None)
            else:
                os.environ["COLLAB_DIR"] = saved
            notify_daemon.VM_DEFAULT_DIR = saved_vm

    def test_scan_dir_floors_mtime_for_cross_platform_consistency(self):
        f = self.collab / "inbox" / "mtime_test.json"
        f.write_text("{}", encoding="utf-8")
        ns = 1_785_319_080_522_071_800  # 2026-07-29 17:58:00.522 +08:00
        os.utime(f, ns=(ns, ns))
        scanned = notify_daemon._scan_dir(self.collab / "inbox")
        self.assertEqual(scanned[f.name], 1785319080.0)

    def _write_inbox_task(self, task_id="task12345678", assignee="PC-B",
                          created_at=None):
        task = {
            "id": task_id,
            "title": "通知测试任务",
            "content": "x",
            "assignee": assignee,
            "status": "pending",
        }
        if created_at:
            task["created_at"] = created_at
        (self.collab / "inbox" / f"{task_id}.json").write_text(
            json.dumps(task, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_new_task_emits_event_and_system_message(self):
        notify_daemon.scan_once(self.collab, self.state)  # 首次 = 基线
        self._write_inbox_task()
        events = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "new_task")
        self.assertEqual(events[0]["title"], "通知测试任务")
        self.assertEqual(events[0]["assignee"], "PC-B")

        # feed 落盘
        feed = self.collab / "notifications" / "feed.jsonl"
        self.assertTrue(feed.exists())
        self.assertIn("通知测试任务", feed.read_text(encoding="utf-8"))

        # 聊天出现系统通知
        chat_files = list((self.collab / "chat").glob("*.json"))
        self.assertEqual(len(chat_files), 1)
        msg = json.loads(chat_files[0].read_text(encoding="utf-8"))
        self.assertEqual(msg["sender"], notify_daemon.SYSTEM_SENDER)

    def test_state_prevents_duplicate_notification(self):
        notify_daemon.scan_once(self.collab, self.state)  # 首次 = 基线
        self._write_inbox_task()
        notify_daemon.scan_once(self.collab, self.state)
        events2 = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(events2, [])
        self.assertEqual(len(list((self.collab / "chat").glob("*.json"))), 1)

    def test_new_message_event_and_system_skip(self):
        notify_daemon.scan_once(self.collab, self.state)  # 首次 = 基线
        (self.collab / "chat" / "20260803-000000_abc12345.json").write_text(
            json.dumps({
                "id": "abc12345",
                "sender": "PC-B",
                "content": "你好",
                "timestamp": "2026-08-03T00:00:00+08:00",
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        events = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "new_message")
        self.assertEqual(events[0]["sender"], "PC-B")

        # 系统消息本身不产生 new_message 事件
        (self.collab / "chat" / "20260803-000001_sys11111.json").write_text(
            json.dumps({
                "id": "sys11111",
                "sender": notify_daemon.SYSTEM_SENDER,
                "content": "x",
                "timestamp": "2026-08-03T00:00:01+08:00",
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        events2 = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(len(events2), 0)

    async def test_get_notifications_tool(self):
        notify_daemon.scan_once(self.collab, self.state)  # 首次 = 基线
        self._write_inbox_task()
        notify_daemon.scan_once(self.collab, self.state)
        # 工具读的是配置里的 COLLAB_DIR，测试里已指向 _TEST_DIR，因此直接注入扫描产物
        feed = _TEST_DIR / "notifications"
        feed.mkdir(parents=True, exist_ok=True)
        (feed / "feed.jsonl").write_text(
            json.dumps({"type": "new_task", "title": "工具测试"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        r = _parse(await notifications.get_notifications())
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["events"][0]["title"], "工具测试")

    def test_feed_is_trimmed_to_bounded_size(self):
        from unittest import mock

        notify_daemon.scan_once(self.collab, self.state)  # 首次 = 基线
        with mock.patch.object(notify_daemon, "MAX_FEED_LINES", 5):
            for i in range(7):
                self._write_inbox_task(task_id=f"task{i:08d}")
            notify_daemon.scan_once(self.collab, self.state)

        feed = self.collab / "notifications" / "feed.jsonl"
        lines = feed.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 5)  # 只保留最近 5 条

    def test_crash_before_state_save_recovers_without_message_flood(self):
        from unittest import mock

        notify_daemon.scan_once(self.collab, self.state)  # 首次 = 基线
        self._write_inbox_task(task_id="crash000001")

        # 模拟：事件已发出，但保存 state 前崩溃
        with mock.patch.object(
            notify_daemon, "_save_state", side_effect=RuntimeError("模拟崩溃")
        ):
            with self.assertRaises(RuntimeError):
                notify_daemon.scan_once(self.collab, self.state)

        # 事件已落盘、系统消息已发出
        feed = self.collab / "notifications" / "feed.jsonl"
        self.assertTrue(feed.exists())
        self.assertEqual(len(list((self.collab / "chat").glob("*.json"))), 1)

        # 下一轮补发：系统消息去重（不刷屏），feed 再记一条（稳定 id 相同）
        notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(len(list((self.collab / "chat").glob("*.json"))), 1)
        feed_events = [
            json.loads(line)
            for line in feed.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(feed_events), 2)
        self.assertEqual(feed_events[0]["id"], feed_events[1]["id"])  # 稳定 id 可去重

    async def test_get_notifications_clamps_limit(self):
        feed = _TEST_DIR / "notifications"
        feed.mkdir(parents=True, exist_ok=True)
        (feed / "feed.jsonl").write_text(
            json.dumps({"type": "new_task", "title": "x"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        r = _parse(await notifications.get_notifications(limit=-5))
        self.assertTrue(r["success"])
        self.assertLessEqual(r["count"], 1)  # 负值被夹到 1

    def test_stale_task_emits_urgent_event_once(self):
        os.environ["NOTIFY_STALE_MINUTES"] = "5"
        try:
            old_ts = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat()
            notify_daemon.scan_once(self.collab, self.state)  # 基线
            self._write_inbox_task(created_at=old_ts)
            events = notify_daemon.scan_once(self.collab, self.state)
            types = {e["type"] for e in events}
            self.assertIn("new_task", types)
            self.assertIn("stale_task", types)
            stale = next(e for e in events if e["type"] == "stale_task")
            self.assertGreaterEqual(stale["age_minutes"], 30)
            self.assertEqual(stale["assignee"], "PC-B")

            # URGENT 系统消息落盘
            msgs = []
            for p in (self.collab / "chat").glob("*.json"):
                msgs.append(json.loads(p.read_text(encoding="utf-8")))
            self.assertTrue(any("URGENT" in m.get("content", "") for m in msgs))

            # 第二次扫描不重复告警
            events2 = notify_daemon.scan_once(self.collab, self.state)
            self.assertEqual(len(events2), 0)
        finally:
            os.environ.pop("NOTIFY_STALE_MINUTES", None)

    def test_stale_alert_clears_after_task_removed(self):
        os.environ["NOTIFY_STALE_MINUTES"] = "5"
        try:
            old_ts = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat()
            notify_daemon.scan_once(self.collab, self.state)
            self._write_inbox_task(created_at=old_ts)
            notify_daemon.scan_once(self.collab, self.state)

            state = json.loads(self.state.read_text(encoding="utf-8"))
            self.assertIn("task12345678", state["stale_alerted"])

            # 任务被完成/删除后，告警记录自动清理
            (self.collab / "inbox" / "task12345678.json").unlink()
            notify_daemon.scan_once(self.collab, self.state)
            state2 = json.loads(self.state.read_text(encoding="utf-8"))
            self.assertEqual(state2["stale_alerted"], {})
        finally:
            os.environ.pop("NOTIFY_STALE_MINUTES", None)

    def test_needs_review_task_skips_stale_alert(self):
        # v1.8.0：待复核任务等 hub 审批，不算执行超时
        os.environ["NOTIFY_STALE_MINUTES"] = "5"
        try:
            old_ts = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat()
            notify_daemon.scan_once(self.collab, self.state)  # 基线
            self._write_inbox_task(created_at=old_ts)
            task_file = self.collab / "inbox" / "task12345678.json"
            task = json.loads(task_file.read_text(encoding="utf-8"))
            task["status"] = "needs_review"
            task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

            events = notify_daemon.scan_once(self.collab, self.state)
            types = [e["type"] for e in events]
            self.assertIn("new_task", types)
            self.assertNotIn("stale_task", types)
        finally:
            os.environ.pop("NOTIFY_STALE_MINUTES", None)

    def test_claim_timeout_releases_and_emits_event(self):
        # v1.8.1：认领超时自动释放，防止"僵尸任务"
        os.environ["NOTIFY_CLAIM_TIMEOUT_MINUTES"] = "5"
        try:
            old_ts = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat()
            notify_daemon.scan_once(self.collab, self.state)  # 基线
            self._write_inbox_task()
            task_file = self.collab / "inbox" / "task12345678.json"
            task = json.loads(task_file.read_text(encoding="utf-8"))
            task["status"] = "in_progress"
            task["claimed_by"] = "PC-B"
            task["claimed_at"] = old_ts
            task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

            events = notify_daemon.scan_once(self.collab, self.state)
            types = [e["type"] for e in events]
            self.assertIn("claim_stale", types)
            stale = next(e for e in events if e["type"] == "claim_stale")
            self.assertEqual(stale["claimed_by"], "PC-B")
            self.assertGreaterEqual(stale["age_minutes"], 30)

            # 已自动释放：回到 pending，清除认领
            released = json.loads(task_file.read_text(encoding="utf-8"))
            self.assertEqual(released["status"], "pending")
            self.assertNotIn("claimed_by", released)

            # journal 记录了 timeout_released
            journal_file = self.collab / "journal" / "task12345678.jsonl"
            self.assertTrue(journal_file.exists())
            lines = journal_file.read_text(encoding="utf-8").strip().splitlines()
            last = json.loads(lines[-1])
            self.assertEqual(last["type"], "timeout_released")
            self.assertEqual(last["identity"], "system")

            # 不重复告警，且不再重复 new_task
            events2 = notify_daemon.scan_once(self.collab, self.state)
            self.assertEqual(events2, [])
        finally:
            os.environ.pop("NOTIFY_CLAIM_TIMEOUT_MINUTES", None)

    def test_claim_timeout_ignores_fresh_claim(self):
        os.environ["NOTIFY_CLAIM_TIMEOUT_MINUTES"] = "5"
        try:
            fresh_ts = datetime.now().astimezone().isoformat()
            notify_daemon.scan_once(self.collab, self.state)  # 基线
            self._write_inbox_task()
            task_file = self.collab / "inbox" / "task12345678.json"
            task = json.loads(task_file.read_text(encoding="utf-8"))
            task["status"] = "in_progress"
            task["claimed_by"] = "PC-B"
            task["claimed_at"] = fresh_ts
            task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

            events = notify_daemon.scan_once(self.collab, self.state)
            self.assertNotIn("claim_stale", [e["type"] for e in events])
            kept = json.loads(task_file.read_text(encoding="utf-8"))
            self.assertEqual(kept["status"], "in_progress")
        finally:
            os.environ.pop("NOTIFY_CLAIM_TIMEOUT_MINUTES", None)

    def test_daemon_skips_blocked_step_notification(self):
        # v1.9.0：流水线未解锁步骤不通知；解锁后（状态转 pending）再触发 new_task
        notify_daemon.scan_once(self.collab, self.state)  # 基线
        self._write_inbox_task()
        task_file = self.collab / "inbox" / "task12345678.json"
        task = json.loads(task_file.read_text(encoding="utf-8"))
        task["status"] = "blocked"
        task["pipeline_id"] = "pipeline123"
        task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

        events = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(events, [])

        task = json.loads(task_file.read_text(encoding="utf-8"))
        task["status"] = "pending"
        task["unblocked_at"] = "2026-08-03T12:00:00+08:00"
        task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        # 两次写入可能落在同一秒：daemon 的 mtime 截断到整秒，需显式推进 mtime
        import time

        now = time.time()
        os.utime(task_file, ns=(int((now + 2) * 1e9), int((now + 2) * 1e9)))
        events2 = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual([e["type"] for e in events2], ["new_task"])

    def test_per_task_claim_timeout_override(self):
        # v1.9.0：任务级 claim_timeout_minutes 覆盖全局阈值
        os.environ["NOTIFY_CLAIM_TIMEOUT_MINUTES"] = "999"
        try:
            old_ts = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat()
            notify_daemon.scan_once(self.collab, self.state)  # 基线
            self._write_inbox_task()
            task_file = self.collab / "inbox" / "task12345678.json"
            task = json.loads(task_file.read_text(encoding="utf-8"))
            task["status"] = "in_progress"
            task["claimed_by"] = "PC-B"
            task["claimed_at"] = old_ts
            task["claim_timeout_minutes"] = 1
            task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

            events = notify_daemon.scan_once(self.collab, self.state)
            types = [e["type"] for e in events]
            self.assertIn("claim_stale", types)
            stale = next(e for e in events if e["type"] == "claim_stale")
            self.assertEqual(stale["threshold_minutes"], 1)
        finally:
            os.environ.pop("NOTIFY_CLAIM_TIMEOUT_MINUTES", None)

    def test_daemon_skips_failed_status(self):
        # v1.9.1：失败/跳过步骤不通知（中止告警由 fail_task 主动发）
        notify_daemon.scan_once(self.collab, self.state)  # 基线
        self._write_inbox_task()
        task_file = self.collab / "inbox" / "task12345678.json"
        task = json.loads(task_file.read_text(encoding="utf-8"))
        task["status"] = "failed"
        task["failure_reason"] = "x"
        task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

        events = notify_daemon.scan_once(self.collab, self.state)
        self.assertEqual(events, [])


class TaskOpsV16Test(unittest.IsolatedAsyncioTestCase):
    """v1.6.0：force_assign / deadline / evidence 验证闭环。"""

    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_create_task_stores_deadline(self):
        r = _parse(await tasks.create_task(
            "带截止", "x", assignee="PC-B",
            deadline="2026-08-03T18:00:00+08:00"))
        self.assertTrue(r["success"])
        task = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(task["deadline"], "2026-08-03T18:00:00+08:00")

    async def test_complete_task_stores_evidence(self):
        r = _parse(await tasks.create_task("验证任务", "x"))
        task_id = r["task_id"]
        r2 = _parse(await tasks.complete_task(
            task_id, evidence="测试 46/46 通过；改动见 tasks.py"))
        self.assertTrue(r2["success"])
        self.assertTrue(r2["has_evidence"])

        done = safe_read_json(DONE_DIR / f"{task_id}.json")
        self.assertIn("46/46", done["evidence"])

        # 中枢可通过 get_task_context 读取已完成任务核实 evidence
        r3 = _parse(await tasks.get_task_context(task_id))
        self.assertTrue(r3["success"])
        self.assertIn("evidence", r3["task"])

    async def test_force_assign_by_hub(self):
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            r = _parse(await tasks.create_task("死信任务", "x", assignee="PC-B"))
            task_id = r["task_id"]
            r2 = _parse(await tasks.force_assign(task_id, "PC-C", reason="PC-B 离线"))
            self.assertTrue(r2["success"])
            self.assertEqual(r2["new_assignee"], "PC-C")
            task = safe_read_json(INBOX_DIR / f"{task_id}.json")
            self.assertEqual(task["assignee"], "PC-C")
            self.assertEqual(task["assign_history"][0]["reason"], "PC-B 离线")
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    async def test_force_assign_denied_for_teammate(self):
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        try:
            r = _parse(await tasks.create_task("任务", "x", assignee="PC-B"))
            r2 = _parse(await tasks.force_assign(r["task_id"], "PC-C"))
            self.assertFalse(r2["success"])
            self.assertIn("仅中枢", r2["error"])
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    async def test_evidence_truncated_and_result_path_stored(self):
        # v1.7.0：超长 evidence 截断 + result_path 落盘
        r = _parse(await tasks.create_task("大结果任务", "x"))
        task_id = r["task_id"]
        big_evidence = "长" * 10000
        r2 = _parse(await tasks.complete_task(
            task_id, evidence=big_evidence, result_path="results/big.md"))
        self.assertTrue(r2["success"])
        done = safe_read_json(DONE_DIR / f"{task_id}.json")
        self.assertLessEqual(len(done["evidence"]), 8000 + 200)
        self.assertIn("已截断", done["evidence"])
        self.assertEqual(done["result_path"], "results/big.md")


class DelegationTest(unittest.IsolatedAsyncioTestCase):
    """第 5 项：should_delegate 协作评分。"""

    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_high_complexity_scores_high(self):
        r = _parse(await delegation.should_delegate(
            "重构登录模块和支付模块，涉及 5 个文件，架构调整，需要代码审查"))
        self.assertTrue(r["success"])
        self.assertGreaterEqual(r["score"], 70)
        self.assertEqual(r["level"], "必须协作拆解")
        self.assertEqual(r["suggest_assignee"], "any")

    async def test_mechanical_task_scores_low(self):
        r = _parse(await delegation.should_delegate(
            "把三个文件的日志格式统一改成同一种格式，改文案和排版"))
        self.assertTrue(r["success"])
        self.assertLess(r["score"], 45)
        self.assertEqual(r["level"], "自己执行更快")

    async def test_single_file_lowers_score(self):
        r = _parse(await delegation.should_delegate("修复 1 个文件的登录 bug，小改动"))
        self.assertLess(r["score"], 45)

    async def test_required_skills_matches_teammate_exactly(self):
        await teammates.register_teammate(
            "PC-B", "Python 后端", skills="code-review:python,testing:unit"
        )
        r = _parse(await delegation.should_delegate(
            "审查登录模块代码", required_skills="code-review:python"))
        self.assertTrue(r["success"])
        self.assertEqual(r["suggest_teammate"], "PC-B")
        self.assertEqual(r["suggest_assignee"], "PC-B")
        match = r["teammate_match"][0]
        self.assertEqual(match["matched"], ["code-review:python"])
        self.assertEqual(match["missing"], [])

    async def test_required_skills_fallback_to_description(self):
        await teammates.register_teammate(
            "PC-C", capabilities="精通 Python 代码审查与单元测试")
        r = _parse(await delegation.should_delegate(
            "审查 python 代码", required_skills="code-review:python"))
        self.assertTrue(r["success"])
        self.assertEqual(r["suggest_teammate"], "PC-C")
        self.assertEqual(
            r["teammate_match"][0]["fallback_hits"],
            ["code-review:python"],
        )

    async def test_required_skills_no_teammate(self):
        r = _parse(await delegation.should_delegate(
            "审查代码", required_skills="code-review:rust"))
        self.assertTrue(r["success"])
        self.assertEqual(r["suggest_teammate"], "")
        self.assertEqual(r["teammate_match"], [])

    async def test_light_scan_suggests_by_skills_without_required(self):
        await teammates.register_teammate("PC-B", "Python", skills="code-review:python")
        r = _parse(await delegation.should_delegate("代码审查 python 模块"))
        self.assertEqual(r["suggest_teammate"], "PC-B")
        self.assertIn("code-review:python", r["teammate_match"][0]["matched"])


class ReviewGateTest(unittest.IsolatedAsyncioTestCase):
    """v1.8.0：高风险任务人工复核闸门（human-in-the-loop）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")
        os.environ.pop("COLLAB_IDENTITY", None)

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity

    async def test_create_with_review_required(self):
        r = _parse(await tasks.create_task("高风险", "改共享代码", review_required=True))
        self.assertTrue(r["success"])
        saved = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertTrue(saved["review_required"])

    async def test_complete_submits_to_review_stays_in_inbox(self):
        r = _parse(await tasks.create_task("高风险", "x", review_required=True))
        task_id = r["task_id"]
        c = _parse(await tasks.complete_task(task_id, evidence="测试通过"))
        self.assertTrue(c["success"])
        self.assertTrue(c["needs_review"])
        self.assertEqual(c["status"], "needs_review")
        # 留在 inbox，不进 done
        self.assertTrue((INBOX_DIR / f"{task_id}.json").exists())
        self.assertFalse((DONE_DIR / f"{task_id}.json").exists())
        saved = safe_read_json(INBOX_DIR / f"{task_id}.json")
        self.assertEqual(saved["status"], "needs_review")
        self.assertIn("evidence", saved)
        events = journal.read_task_journal(task_id)
        self.assertEqual(events[-1]["type"], "submitted")

    async def test_approve_moves_to_done(self):
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        r = _parse(await tasks.create_task("高风险", "x", review_required=True))
        task_id = r["task_id"]
        await tasks.complete_task(task_id, evidence="通过")
        a = _parse(await tasks.approve_task(task_id, comment="LGTM"))
        self.assertTrue(a["success"])
        self.assertEqual(a["approved_by"], "PC-A")
        self.assertFalse((INBOX_DIR / f"{task_id}.json").exists())
        done = safe_read_json(DONE_DIR / f"{task_id}.json")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["review_comment"], "LGTM")
        events = journal.read_task_journal(task_id)
        self.assertEqual(events[-1]["type"], "approved")

    async def test_request_changes_resets_to_pending(self):
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        r = _parse(await tasks.create_task("高风险", "x", review_required=True))
        task_id = r["task_id"]
        await tasks.complete_task(task_id, evidence="v1")
        rc = _parse(await tasks.request_changes(task_id, reason="缺测试"))
        self.assertTrue(rc["success"])
        saved = safe_read_json(INBOX_DIR / f"{task_id}.json")
        self.assertEqual(saved["status"], "pending")
        self.assertNotIn("claimed_by", saved)
        self.assertEqual(saved["review_notes"][0]["reason"], "缺测试")
        events = journal.read_task_journal(task_id)
        self.assertEqual(events[-1]["type"], "changes_requested")

    async def test_rework_cycle_after_changes(self):
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        r = _parse(await tasks.create_task("高风险", "x", review_required=True))
        task_id = r["task_id"]
        await tasks.complete_task(task_id, evidence="v1")
        await tasks.request_changes(task_id, reason="重做")
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        c = _parse(await tasks.claim_task(task_id))
        self.assertTrue(c["success"])
        c2 = _parse(await tasks.complete_task(task_id, evidence="v2"))
        self.assertTrue(c2["success"])
        self.assertTrue(c2["needs_review"])
        types = [e["type"] for e in journal.read_task_journal(task_id)]
        self.assertEqual(
            types,
            ["created", "submitted", "changes_requested", "claimed", "submitted"],
        )

    async def test_approve_denied_for_teammate(self):
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        r = _parse(await tasks.create_task("高风险", "x", review_required=True))
        task_id = r["task_id"]
        await tasks.complete_task(task_id, evidence="x")
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        a = _parse(await tasks.approve_task(task_id))
        self.assertFalse(a["success"])
        self.assertIn("仅中枢", a["error"])

    async def test_approve_fails_for_non_review_task(self):
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        r = _parse(await tasks.create_task("普通任务", "x"))
        task_id = r["task_id"]
        a = _parse(await tasks.approve_task(task_id))
        self.assertFalse(a["success"])
        self.assertIn("不在待审核状态", a["error"])

    async def test_needs_review_hidden_from_teammate(self):
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        r = _parse(await tasks.create_task("高风险", "x", review_required=True))
        await tasks.complete_task(r["task_id"], evidence="x")

        os.environ["COLLAB_IDENTITY"] = "PC-B"
        pending = _parse(await tasks.get_pending_tasks())
        self.assertEqual(pending["count"], 0)

        os.environ["COLLAB_IDENTITY"] = "PC-A"
        hub = _parse(await tasks.get_pending_tasks())
        self.assertEqual(hub["count"], 1)
        self.assertEqual(hub["tasks"][0]["status"], "needs_review")


class TemplateTest(unittest.IsolatedAsyncioTestCase):
    """v1.8.4：任务模板 / Playbook。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)

    async def test_list_templates(self):
        r = _parse(await tasks.list_templates())
        self.assertTrue(r["success"])
        self.assertGreaterEqual(r["count"], 3)
        names = {t["name"] for t in r["templates"]}
        self.assertIn("code-review", names)
        self.assertIn("run-tests", names)
        self.assertIn("write-doc", names)

    async def test_create_task_with_template_expands(self):
        r = _parse(await tasks.create_task(
            "占位", "占位", template="code-review",
            template_params='{"target": "login.py"}'))
        self.assertTrue(r["success"])
        self.assertEqual(r["template"], "code-review")
        task = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(task["title"], "代码审查：login.py")
        self.assertIn("login.py", task["content"])
        self.assertIn("代码审查", task["content"])
        self.assertEqual(task["template"], "code-review")
        self.assertTrue(task["review_required"])  # 模板默认复核
        self.assertIn("deadline", task)  # 模板默认 deadline 已展开
        events = journal.read_task_journal(r["task_id"])
        self.assertEqual(events[0]["type"], "created")

    async def test_template_missing_required_param_fails(self):
        r = _parse(await tasks.create_task("x", "x", template="code-review"))
        self.assertFalse(r["success"])
        self.assertIn("缺少必填参数: target", r["error"])

    async def test_unknown_template_fails(self):
        r = _parse(await tasks.create_task("x", "x", template="no-such"))
        self.assertFalse(r["success"])
        self.assertIn("模板不存在", r["error"])

    async def test_template_invalid_params_json_fails(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="code-review", template_params="not-json"))
        self.assertFalse(r["success"])
        self.assertIn("不是合法 JSON", r["error"])

    async def test_template_explicit_params_override_defaults(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="write-doc",
            template_params='{"topic": "任务模板", "file": "docs/templates.md"}'))
        self.assertTrue(r["success"])
        task = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(task["title"], "编写文档：任务模板")
        self.assertIn("docs/templates.md", task["content"])
        self.assertIn("开发者", task["content"])  # 默认参数 audience 仍生效

    async def test_template_run_tests_defaults_and_assignee_precedence(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="run-tests",
            template_params='{"project_path": "C:/myshare/collab"}'))
        self.assertTrue(r["success"])
        task = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(task["execution_env"], "hub VM")
        self.assertFalse(task.get("review_required", False))

        # 显式 assignee 优先于模板默认
        r2 = _parse(await tasks.create_task(
            "y", "y", assignee="PC-B", template="code-review",
            template_params='{"target": "auth.py"}'))
        self.assertTrue(r2["success"])
        task2 = safe_read_json(INBOX_DIR / f"{r2['task_id']}.json")
        self.assertEqual(task2["assignee"], "PC-B")


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    """v1.9.0：多步骤流水线（依赖解锁 + 每步超时）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)

    async def test_create_pipeline_creates_steps(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "login.py", "project_path": "C:/app"}'))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["steps"][0]["step_name"], "lint")
        self.assertEqual(r["steps"][0]["status"], "pending")
        self.assertEqual(r["steps"][1]["step_name"], "review")
        self.assertEqual(r["steps"][1]["status"], "blocked")

        lint_id = r["steps"][0]["task_id"]
        review_id = r["steps"][1]["task_id"]
        lint = safe_read_json(INBOX_DIR / f"{lint_id}.json")
        review = safe_read_json(INBOX_DIR / f"{review_id}.json")
        self.assertEqual(lint["pipeline_id"], r["pipeline_id"])
        self.assertEqual(lint["execution_env"], "hub VM")
        self.assertEqual(lint["claim_timeout_minutes"], 15)
        self.assertEqual(review["status"], "blocked")
        self.assertEqual(review["depends_on"], [lint_id])
        self.assertTrue(review["review_required"])
        self.assertIn("login.py", review["title"])  # 占位符已填

    async def test_blocked_step_hidden_from_teammate(self):
        await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}')
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        try:
            pending = _parse(await tasks.get_pending_tasks())
            self.assertEqual(pending["count"], 1)
            self.assertEqual(pending["tasks"][0]["step_name"], "lint")
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    async def test_complete_step_unblocks_next(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id = r["steps"][0]["task_id"]
        review_id = r["steps"][1]["task_id"]

        c = _parse(await tasks.complete_task(lint_id, evidence="10/10 通过"))
        self.assertTrue(c["success"])
        self.assertTrue((DONE_DIR / f"{lint_id}.json").exists())

        review = safe_read_json(INBOX_DIR / f"{review_id}.json")
        self.assertEqual(review["status"], "pending")
        self.assertIn("unblocked_at", review)
        events = journal.read_task_journal(review_id)
        self.assertEqual(events[-1]["type"], "unblocked")

    async def test_blocked_step_cannot_complete_or_claim(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        review_id = r["steps"][1]["task_id"]
        c = _parse(await tasks.complete_task(review_id, evidence="test"))
        self.assertFalse(c["success"])
        self.assertIn("尚未解锁", c["error"])
        cl = _parse(await tasks.claim_task(review_id))
        self.assertFalse(cl["success"])
        self.assertIn("尚未解锁", cl["error"])

    async def test_pipeline_review_step_approval_flow(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id = r["steps"][0]["task_id"]
        review_id = r["steps"][1]["task_id"]
        await tasks.complete_task(lint_id, evidence="ok")

        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            c = _parse(await tasks.complete_task(review_id, evidence="审查完成"))
            self.assertTrue(c["success"])
            self.assertTrue(c["needs_review"])
            a = _parse(await tasks.approve_task(review_id, comment="LGTM"))
            self.assertTrue(a["success"])
            self.assertTrue((DONE_DIR / f"{review_id}.json").exists())
            done = safe_read_json(DONE_DIR / f"{review_id}.json")
            self.assertEqual(done["status"], "done")
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    async def test_create_task_with_claim_timeout_minutes_field(self):
        r = _parse(await tasks.create_task("超时任务", "x", claim_timeout_minutes=30))
        self.assertTrue(r["success"])
        saved = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(saved["claim_timeout_minutes"], 30)

    async def test_list_templates_shows_pipeline(self):
        r = _parse(await tasks.list_templates())
        pipe = next(t for t in r["templates"] if t["name"] == "pipeline-code-review")
        self.assertTrue(pipe["pipeline"])
        self.assertEqual(pipe["step_count"], 2)

    def _write_three_step_pipeline_template(self):
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)
        tpl = {
            "name": "pipeline-three",
            "description": "三步骤流水线测试",
            "required_params": ["target"],
            "default_params": {},
            "pipeline": [
                {
                    "name": "step1",
                    "template": "run-tests",
                    "params": {"project_path": "{target}"},
                    "depends_on": [],
                    "claim_timeout_minutes": 5,
                },
                {
                    "name": "step2",
                    "template": "run-tests",
                    "params": {"project_path": "{target}"},
                    "depends_on": ["step1"],
                },
                {
                    "name": "step3",
                    "template": "run-tests",
                    "params": {"project_path": "{target}"},
                    "depends_on": ["step2"],
                },
            ],
        }
        (COLLAB_DIR / "templates" / "pipeline-three.json").write_text(
            json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def test_fail_step_skips_dependents_and_alerts(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id = r["steps"][0]["task_id"]
        review_id = r["steps"][1]["task_id"]

        f = _parse(await tasks.fail_task(lint_id, reason="测试全挂"))
        self.assertTrue(f["success"])
        self.assertEqual(f["pipeline_status"], "failed")
        self.assertEqual(f["skipped_steps"], [review_id])

        review = safe_read_json(INBOX_DIR / f"{review_id}.json")
        self.assertEqual(review["status"], "skipped")
        self.assertEqual(review["skipped_reason"], "upstream_failed")

        lint_types = [e["type"] for e in journal.read_task_journal(lint_id)]
        self.assertIn("step_failed", lint_types)
        self.assertIn("pipeline_status", lint_types)
        review_types = [e["type"] for e in journal.read_task_journal(review_id)]
        self.assertIn("step_skipped", review_types)

        feed_text = (COLLAB_DIR / "notifications" / "feed.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("pipeline_aborted", feed_text)
        chat_msgs = _parse(await chat.get_chat_history())
        urgent = [m for m in chat_msgs["messages"] if "URGENT" in m["content"]]
        self.assertEqual(len(urgent), 1)

        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "failed")
        self.assertEqual(agg["failed_steps"], 1)
        self.assertEqual(agg["skipped_steps"], 1)

    async def test_fail_standalone_task(self):
        r = _parse(await tasks.create_task("普通任务", "x"))
        f = _parse(await tasks.fail_task(r["task_id"], reason="不可行"))
        self.assertTrue(f["success"])
        saved = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["failure_reason"], "不可行")
        pending = _parse(await tasks.get_pending_tasks())
        self.assertEqual(pending["count"], 0)

    async def test_fail_requires_owner(self):
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        try:
            r = _parse(await tasks.create_task("任务", "x", assignee="any"))
            await tasks.claim_task(r["task_id"])
            os.environ["COLLAB_IDENTITY"] = "PC-C"
            f = _parse(await tasks.fail_task(r["task_id"], reason="抢"))
            self.assertFalse(f["success"])
            self.assertIn("认领", f["error"])
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

    async def test_failed_step_cannot_complete_or_claim(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id = r["steps"][0]["task_id"]
        await tasks.fail_task(lint_id, reason="挂了")
        c = _parse(await tasks.complete_task(lint_id, evidence="test"))
        self.assertFalse(c["success"])
        self.assertIn("已失败", c["error"])
        cl = _parse(await tasks.claim_task(lint_id))
        self.assertFalse(cl["success"])
        self.assertIn("已失败", cl["error"])

    async def test_aggregate_pipeline_completed(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id = r["steps"][0]["task_id"]
        review_id = r["steps"][1]["task_id"]
        await tasks.complete_task(lint_id, evidence="ok")
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "running")
        self.assertEqual(agg["done_steps"], 1)

        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            await tasks.complete_task(review_id, evidence="审查完毕")
            await tasks.approve_task(review_id, comment="ok")
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved
        agg2 = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg2["status"], "completed")
        self.assertEqual(agg2["done_steps"], 2)

    async def test_transitive_cascade(self):
        self._write_three_step_pipeline_template()
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-three",
            template_params='{"target": "C:/app"}'))
        s1, s2, s3 = [s["task_id"] for s in r["steps"]]
        await tasks.fail_task(s1, reason="根因失败")

        t2 = safe_read_json(INBOX_DIR / f"{s2}.json")
        t3 = safe_read_json(INBOX_DIR / f"{s3}.json")
        self.assertEqual(t2["status"], "skipped")
        self.assertEqual(t3["status"], "skipped")
        self.assertIn(
            "step_skipped",
            [e["type"] for e in journal.read_task_journal(s3)],
        )

    async def test_fanout_unblocks_multiple_steps(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-parallel-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id, unit_id, intg_id, review_id = [s["task_id"] for s in r["steps"]]
        self.assertEqual(r["steps"][1]["status"], "blocked")
        self.assertEqual(r["steps"][2]["status"], "blocked")

        await tasks.complete_task(lint_id, evidence="ok")
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{unit_id}.json")["status"], "pending"
        )
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{intg_id}.json")["status"], "pending"
        )
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{review_id}.json")["status"], "blocked"
        )

    async def test_fanin_waits_for_all(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-parallel-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id, unit_id, intg_id, review_id = [s["task_id"] for s in r["steps"]]
        await tasks.complete_task(lint_id, evidence="ok")
        await tasks.complete_task(unit_id, evidence="unit ok")
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{review_id}.json")["status"], "blocked"
        )
        await tasks.complete_task(intg_id, evidence="intg ok")
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{review_id}.json")["status"], "pending"
        )

    async def test_partial_failure_in_parallel(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-parallel-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id, unit_id, intg_id, review_id = [s["task_id"] for s in r["steps"]]
        await tasks.complete_task(lint_id, evidence="ok")
        await tasks.fail_task(intg_id, reason="集成测试挂")
        # review 因依赖失败被跳过；test-unit 独立分支不受影响
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{review_id}.json")["status"], "skipped"
        )
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{unit_id}.json")["status"], "pending"
        )
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "failed")

    async def test_retry_resets_pending_until_exhausted(self):
        # pipeline-parallel-review 的 test-unit 声明 max_retries=2
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-parallel-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        lint_id = r["steps"][0]["task_id"]
        unit_id = r["steps"][1]["task_id"]
        review_id = r["steps"][3]["task_id"]
        await tasks.complete_task(lint_id, evidence="ok")

        # 第一次失败 → 重试（不级联、不终态）
        f1 = _parse(await tasks.fail_task(unit_id, reason="偶发失败1"))
        self.assertTrue(f1["success"])
        self.assertEqual(f1["retry_count"], 1)
        self.assertEqual(f1["max_retries"], 2)
        unit = safe_read_json(INBOX_DIR / f"{unit_id}.json")
        self.assertEqual(unit["status"], "pending")
        self.assertNotIn("claimed_by", unit)
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{review_id}.json")["status"], "blocked"
        )
        self.assertIn(
            "step_retried",
            [e["type"] for e in journal.read_task_journal(unit_id)],
        )

        # 第二次失败 → 仍重试
        f2 = _parse(await tasks.fail_task(unit_id, reason="偶发失败2"))
        self.assertTrue(f2["success"])
        self.assertEqual(f2["retry_count"], 2)

        # 第三次失败 → 重试耗尽 → 终态失败 + 级联跳过 review
        f3 = _parse(await tasks.fail_task(unit_id, reason="彻底失败"))
        self.assertTrue(f3["success"])
        self.assertEqual(f3["pipeline_status"], "failed")
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{unit_id}.json")["status"], "failed"
        )
        self.assertEqual(
            safe_read_json(INBOX_DIR / f"{review_id}.json")["status"], "skipped"
        )

    async def test_retry_then_complete(self):
        r = _parse(await tasks.create_task("重试后完成", "x", max_retries=1))
        await tasks.fail_task(r["task_id"], reason="第一次失败")
        c = _parse(await tasks.complete_task(r["task_id"], evidence="重试后成功"))
        self.assertTrue(c["success"])
        self.assertTrue((DONE_DIR / f"{r['task_id']}.json").exists())
        done = safe_read_json(DONE_DIR / f"{r['task_id']}.json")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["retry_count"], 1)

    async def test_standalone_retry_param_stored(self):
        r = _parse(await tasks.create_task("重试任务", "x", max_retries=3))
        saved = safe_read_json(INBOX_DIR / f"{r['task_id']}.json")
        self.assertEqual(saved["max_retries"], 3)
        self.assertEqual(saved["retry_count"], 0)

    async def test_conditional_skip_on_creation(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-conditional-review",
            template_params='{"target": "a.py", "project_path": "C:/app", "lang": "python"}'))
        self.assertTrue(r["success"])
        py_id = r["steps"][1]["task_id"]
        js_id = r["steps"][2]["task_id"]
        self.assertEqual(r["steps"][1]["status"], "blocked")  # 条件满足，等 lint
        self.assertEqual(r["steps"][2]["status"], "skipped")  # 条件不满足，创建即跳过

        py = safe_read_json(INBOX_DIR / f"{py_id}.json")
        js = safe_read_json(INBOX_DIR / f"{js_id}.json")
        self.assertEqual(js["skipped_reason"], "condition_not_met")
        self.assertIn(
            "step_skipped",
            [e["type"] for e in journal.read_task_journal(js_id)],
        )
        # 完成 lint 后 python 分支解锁
        await tasks.complete_task(r["steps"][0]["task_id"], evidence="ok")
        self.assertEqual(safe_read_json(INBOX_DIR / f"{py_id}.json")["status"], "pending")
        self.assertEqual(safe_read_json(INBOX_DIR / f"{js_id}.json")["status"], "skipped")

    async def test_conditional_cascade_to_dependents(self):
        # 依赖条件跳过步骤的 blocked 步骤，创建时同步级联跳过
        self._write_conditional_cascade_template()
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-cond-cascade",
            template_params='{"target": "C:/app", "lang": "other"}'))
        self.assertEqual(r["steps"][1]["status"], "skipped")  # 条件不满足
        self.assertEqual(r["steps"][2]["status"], "skipped")  # 依赖跳过 → 级联
        t3 = safe_read_json(INBOX_DIR / f"{r['steps'][2]['task_id']}.json")
        self.assertEqual(t3["skipped_reason"], "upstream_failed")

    async def test_sub_pipeline_creation(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-release",
            template_params='{"target": "svc", "project_path": "C:/app"}'))
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 6)  # 2 外层 + 4 子步骤
        steps = {s["step_name"]: s for s in r["steps"]}
        self.assertIn("quality", steps)
        self.assertIn("deploy-check", steps)
        self.assertIn("lint", steps)

        quality = safe_read_json(INBOX_DIR / f"{steps['quality']['task_id']}.json")
        self.assertTrue(quality["is_pipeline_parent"])
        self.assertTrue(quality["sub_pipeline_id"])
        self.assertEqual(quality["status"], "in_progress")  # 无依赖，子流水线即刻可跑

        lint = safe_read_json(INBOX_DIR / f"{steps['lint']['task_id']}.json")
        self.assertEqual(lint["parent_step_id"], steps["quality"]["task_id"])
        self.assertEqual(lint["status"], "pending")  # 父无依赖 → 首波直接放行
        deploy = safe_read_json(INBOX_DIR / f"{steps['deploy-check']['task_id']}.json")
        self.assertEqual(deploy["status"], "blocked")

        created = journal.read_task_journal(lint["id"])
        self.assertIn("parent_step=", created[0].get("detail", ""))

    async def test_sub_pipeline_full_completion(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-release",
            template_params='{"target": "svc", "project_path": "C:/app"}'))
        steps = {s["step_name"]: s for s in r["steps"]}
        quality_id = steps["quality"]["task_id"]
        lint_id = steps["lint"]["task_id"]
        unit_id = steps["test-unit"]["task_id"]
        intg_id = steps["test-integration"]["task_id"]
        review_id = steps["review"]["task_id"]
        deploy_id = steps["deploy-check"]["task_id"]

        await tasks.complete_task(lint_id, evidence="lint ok")
        await tasks.complete_task(unit_id, evidence="unit ok")
        await tasks.complete_task(intg_id, evidence="intg ok")
        saved = os.environ.get("COLLAB_IDENTITY")
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            await tasks.complete_task(review_id, evidence="review ok")
            await tasks.approve_task(review_id, comment="LGTM")
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved

        # 子流水线完成 → 父步骤 done（移入 done/）→ deploy-check 解锁
        self.assertTrue((DONE_DIR / f"{quality_id}.json").exists())
        deploy = safe_read_json(INBOX_DIR / f"{deploy_id}.json")
        self.assertEqual(deploy["status"], "pending")
        q_events = [e["type"] for e in journal.read_task_journal(quality_id)]
        self.assertIn("sub_pipeline_completed", q_events)

        # deploy-check 完成 + 复核 → 外层 completed
        os.environ["COLLAB_IDENTITY"] = "PC-A"
        try:
            await tasks.complete_task(deploy_id, evidence="发布检查通过")
            await tasks.approve_task(deploy_id, comment="go")
        finally:
            if saved is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "completed")
        self.assertEqual(agg["done_steps"], 2)
        # v2.1.2：终态聚合回写 done/ 文件，pipeline_status 与 journal/聚合一致
        done_quality = safe_read_json(DONE_DIR / f"{quality_id}.json")
        self.assertEqual(done_quality["pipeline_status"], "completed")
        done_review = safe_read_json(DONE_DIR / f"{review_id}.json")
        self.assertEqual(done_review["pipeline_status"], "completed")
        done_deploy = safe_read_json(DONE_DIR / f"{deploy_id}.json")
        self.assertEqual(done_deploy["pipeline_status"], "completed")

    async def test_sub_pipeline_failure_fails_parent(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-release",
            template_params='{"target": "svc", "project_path": "C:/app"}'))
        steps = {s["step_name"]: s for s in r["steps"]}
        quality_id = steps["quality"]["task_id"]
        lint_id = steps["lint"]["task_id"]
        deploy_id = steps["deploy-check"]["task_id"]

        await tasks.fail_task(lint_id, reason="子流水线 lint 失败")
        quality = safe_read_json(INBOX_DIR / f"{quality_id}.json")
        self.assertEqual(quality["status"], "failed")
        deploy = safe_read_json(INBOX_DIR / f"{deploy_id}.json")
        self.assertEqual(deploy["status"], "skipped")
        agg = tasks.aggregate_pipeline(r["pipeline_id"])
        self.assertEqual(agg["status"], "failed")
        q_events = [e["type"] for e in journal.read_task_journal(quality_id)]
        self.assertIn("sub_pipeline_failed", q_events)

    async def test_parent_not_claimable(self):
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-release",
            template_params='{"target": "svc", "project_path": "C:/app"}'))
        steps = {s["step_name"]: s for s in r["steps"]}
        quality_id = steps["quality"]["task_id"]
        c = _parse(await tasks.claim_task(quality_id))
        self.assertFalse(c["success"])
        self.assertIn("父步骤", c["error"])
        cm = _parse(await tasks.complete_task(quality_id, evidence="test"))
        self.assertFalse(cm["success"])
        f = _parse(await tasks.fail_task(quality_id, reason="x"))
        self.assertFalse(f["success"])

    async def test_pending_hides_parent(self):
        await tasks.create_task(
            "x", "x", template="pipeline-release",
            template_params='{"target": "svc", "project_path": "C:/app"}')
        pending = _parse(await tasks.get_pending_tasks())
        names = {t["step_name"] for t in pending["tasks"]}
        self.assertNotIn("quality", names)
        self.assertIn("lint", names)

    async def test_depth_guard(self):
        self._write_deep_pipeline_templates(5)
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-level1",
            template_params='{"target": "C:/app"}'))
        self.assertFalse(r["success"])
        self.assertIn("超过最大深度", r["error"])
        self.assertIn("已回滚", r["error"])
        # v2.1.1：失败创建不残留孤儿步骤
        leftovers = [f.name for f in INBOX_DIR.glob("*.json")]
        self.assertEqual(leftovers, [])

    async def test_cycle_guard(self):
        tpl = {
            "name": "pipeline-cycle",
            "required_params": ["target"],
            "default_params": {},
            "pipeline": [
                {
                    "name": "s1",
                    "template": "pipeline-cycle",
                    "params": {"project_path": "{target}"},
                }
            ],
        }
        (COLLAB_DIR / "templates" / "pipeline-cycle.json").write_text(
            json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-cycle",
            template_params='{"target": "C:/app"}'))
        self.assertFalse(r["success"])
        self.assertIn("循环引用", r["error"])

    def _write_conditional_cascade_template(self):
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)
        tpl = {
            "name": "pipeline-cond-cascade",
            "description": "条件级联测试",
            "required_params": ["target", "lang"],
            "default_params": {},
            "pipeline": [
                {
                    "name": "step1",
                    "template": "run-tests",
                    "params": {"project_path": "{target}"},
                    "depends_on": [],
                },
                {
                    "name": "step2",
                    "template": "run-tests",
                    "params": {"project_path": "{target}"},
                    "depends_on": ["step1"],
                    "if": {"param": "lang", "equals": "python"},
                },
                {
                    "name": "step3",
                    "template": "run-tests",
                    "params": {"project_path": "{target}"},
                    "depends_on": ["step2"],
                },
            ],
        }
        (COLLAB_DIR / "templates" / "pipeline-cond-cascade.json").write_text(
            json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_deep_pipeline_templates(self, levels: int):
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)
        for level in range(1, levels + 1):
            nxt = f"pipeline-level{level + 1}" if level < levels else "run-tests"
            # level1 由 create 参数注入 target；level2+ 只要求逐层传递 project_path
            required = ["target"] if level == 1 else ["project_path"]
            param_src = "{target}" if level == 1 else "{project_path}"
            tpl = {
                "name": f"pipeline-level{level}",
                "required_params": required,
                "default_params": {},
                "pipeline": [
                    {
                        "name": "s1",
                        "template": nxt,
                        "params": {"project_path": param_src},
                    }
                ],
            }
            (COLLAB_DIR / "templates" / f"pipeline-level{level}.json").write_text(
                json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8"
            )


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    """v1.8.5：静态只读看板生成。"""

    async def asyncSetUp(self):
        _reset_collab_dir()

    async def test_generate_dashboard_writes_html(self):
        await tasks.create_task("看板任务", "x")
        r = _parse(await dashboard.generate_dashboard())
        self.assertTrue(r["success"])
        out = Path(r["path"])
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("Claude 协作看板", content)
        self.assertIn("看板任务", content)
        self.assertIn(__version__, content)
        self.assertIn("待办任务", content)

    async def test_dashboard_escapes_html(self):
        await tasks.create_task("<script>alert(1)</script>", "x")
        r = _parse(await dashboard.generate_dashboard())
        content = Path(r["path"]).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("&lt;script&gt;", content)

    async def test_dashboard_loop_state_section(self):
        # v3.19：闭环状态区块（最新观察 + 身体状态 + 动作史）
        vision_dir = COLLAB_DIR / "vision"
        vision_dir.mkdir(parents=True, exist_ok=True)
        (vision_dir / "latest.json").write_text(json.dumps({
            "schema": "collab-vision-observation-v1", "observation_id": "obs-test-000001",
            "timestamp": "2026-08-08T12:00:00+08:00", "source": "camera:0",
            "stats": {"brightness": 0.5, "sat_mean": 62.9, "edge_density": 0.054,
                      "dominant_color": "#a0a0a0",
                      "dominant_colors": [{"color": "#e0e060", "share": 0.10}]},
            "detections": [], "notes": [],
        }, ensure_ascii=False), encoding="utf-8")
        body_dir = COLLAB_DIR / "body"
        body_dir.mkdir(parents=True, exist_ok=True)
        (body_dir / "state.json").write_text(json.dumps({
            "schema": "collab-body-state-v1", "position": {"x": 1, "y": 0},
            "heading_deg": 90, "light": True, "camera": {"pan_deg": 30, "tilt_deg": 0},
            "last_action": "forward", "last_action_at": "2026-08-08T12:01:00+08:00",
            "history": [{
                "at": "2026-08-08T12:01:00+08:00", "norm": "forward",
                "action": "前进", "summary": "前进 1 步",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        r = _parse(await dashboard.generate_dashboard())
        self.assertTrue(r["success"])
        content = Path(r["path"]).read_text(encoding="utf-8")
        self.assertIn("闭环状态", content)
        self.assertIn("obs-test-000001", content)
        self.assertIn("朝向 90", content)
        self.assertIn("前进 1 步", content)

    async def test_dashboard_metrics_section(self):
        # v3.20：平台指标区块（吞吐/周期/活跃队友/事件分布）；数值断言防"今日恒0"
        r = _parse(await tasks.create_task("指标任务", "x"))
        await tasks.complete_task(r["task_id"], evidence="done")
        r = _parse(await dashboard.generate_dashboard())
        self.assertTrue(r["success"])
        content = Path(r["path"]).read_text(encoding="utf-8")
        self.assertIn("平台指标", content)
        # 今日完成应有 1（刚完成的任务，completed_at=now）——PC-C 中1 数值断言
        self.assertIn("<b>1</b><span>今日完成</span>", content)
        self.assertIn("近7日完成", content)
        self.assertIn("24h活跃队友", content)
        self.assertIn("事件分布", content)

    async def test_is_today_local_timezone_window(self):
        # v3.23.0 回归：本地 00:00-08:00 窗口内，UTC 时间戳的"今天"仍是昨天日期，
        # 比较前必须转本地时区（否则刚完成的任务被判成昨天，今日完成=0）
        import datetime as _dt
        from collab_mcp import dashboard as _dash

        # v3.23.1 回归：比较时区由 _now() 决定（单一事实源），
        # 测试不再依赖系统时区（GitHub Actions UTC 环境曾挂：裸 astimezone() 转 UTC）
        fake_now = _dt.datetime(2026, 8, 9, 6, 0, 0,
                                tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        with patch.object(_dash, "_now", return_value=fake_now):
            # 2026-08-08T22:00Z == 2026-08-09T06:00+08:00 → 今天
            self.assertTrue(
                _dash._is_today({"completed_at": "2026-08-08T22:00:00+00:00"})
            )
            # 2026-08-07T22:00Z == 2026-08-08T06:00+08:00 → 不是今天
            self.assertFalse(
                _dash._is_today({"completed_at": "2026-08-07T22:00:00+00:00"})
            )
        # UTC 场景（等价 CI 环境）：now 是 UTC 时，"今天"按 UTC 自然日判断
        fake_utc = _dt.datetime(2026, 8, 9, 6, 0, 0, tzinfo=_dt.timezone.utc)
        with patch.object(_dash, "_now", return_value=fake_utc):
            self.assertTrue(
                _dash._is_today({"completed_at": "2026-08-09T06:00:00+00:00"})
            )
            self.assertFalse(
                _dash._is_today({"completed_at": "2026-08-08T22:00:00+00:00"})
            )

    async def test_generate_dashboard_custom_path(self):
        out = COLLAB_DIR / "custom-dashboard.html"
        r = _parse(await dashboard.generate_dashboard(str(out)))
        self.assertTrue(r["success"])
        self.assertTrue(out.exists())
        self.assertIn("协作看板", out.read_text(encoding="utf-8"))

    async def test_dashboard_shows_pipeline_grouping(self):
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-code-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))

        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        self.assertIn("流水线", content)
        self.assertIn("pipeline-code-review", content)
        self.assertIn("running", content)

        await tasks.complete_task(r["steps"][0]["task_id"], evidence="ok")
        out2 = _parse(await dashboard.generate_dashboard())
        content2 = Path(out2["path"]).read_text(encoding="utf-8")
        self.assertIn("1/2", content2)
        self.assertIn("chip-done", content2)

    async def _create_parallel_pipeline(self) -> dict:
        import shutil

        repo_templates = REPO_DIR / "templates"
        if repo_templates.is_dir():
            shutil.copytree(repo_templates, COLLAB_DIR / "templates", dirs_exist_ok=True)
        r = _parse(await tasks.create_task(
            "x", "x", template="pipeline-parallel-review",
            template_params='{"target": "a.py", "project_path": "C:/app"}'))
        self.assertTrue(r["success"])
        return {s["step_name"]: s["task_id"] for s in r["steps"]}

    async def test_dashboard_contains_dag_svg(self):
        steps = await self._create_parallel_pipeline()
        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        self.assertIn("流水线 DAG", content)
        self.assertIn("<svg", content)
        self.assertIn("图例", content)
        self.assertTrue(steps["lint"] in content and steps["review"] in content)

    async def test_dag_edges_reflect_depends_on(self):
        import re

        steps = await self._create_parallel_pipeline()
        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        expect = {
            f'{steps["lint"]}->{steps["test-unit"]}',
            f'{steps["lint"]}->{steps["test-integration"]}',
            f'{steps["test-unit"]}->{steps["review"]}',
            f'{steps["test-integration"]}->{steps["review"]}',
        }
        found = {m.group(1) for m in re.finditer(r'data-dep="([^"]+)"', content)}
        self.assertTrue(expect.issubset(found), f"missing edges: {expect - found}")

    async def test_dag_node_status_colors(self):
        steps = await self._create_parallel_pipeline()
        lint_id = steps["lint"]
        await tasks.complete_task(lint_id, evidence="ok")
        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        self.assertIn(f'data-task="{lint_id}" data-status="done"', content)
        self.assertIn('fill="#d1fae5"', content)  # done 状态填充色

    async def test_dag_empty_pipeline_safe(self):
        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        self.assertIn("流水线 DAG", content)
        self.assertIn("暂无流水线", content)
        # v3.24 起整页必然含趋势 SVG；此处只断言流水线 DAG 未渲染
        self.assertNotIn('class="dag-node"', content)
        self.assertNotIn('class="dag-edge"', content)

    async def test_dag_missing_id_skipped(self):
        # M1 回归：缺 id 的坏任务不拖垮整份看板（无 id 步骤被跳过）
        steps = await self._create_parallel_pipeline()
        bad = COLLAB_DIR / "done" / "bad_no_id.json"
        bad.write_text(
            json.dumps(
                {"pipeline_id": "badpipe", "step_name": "ghost", "status": "pending"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        self.assertIn("流水线 DAG", content)
        self.assertIn("badpipe", content)  # 坏流水线仍出现在聚合 chips 中
        self.assertIn(steps["lint"], content)  # 正常流水线 DAG 不受影响

    async def test_dag_cycle_does_not_crash(self):
        # L1 回归：循环依赖不崩溃（visiting 集合防护）
        a = {
            "id": "cyc_a", "pipeline_id": "cycpipe", "step_name": "a",
            "status": "pending", "depends_on": ["cyc_b"],
        }
        b = {
            "id": "cyc_b", "pipeline_id": "cycpipe", "step_name": "b",
            "status": "pending", "depends_on": ["cyc_a"],
        }
        (COLLAB_DIR / "inbox" / "cyc_a.json").write_text(
            json.dumps(a, ensure_ascii=False), encoding="utf-8")
        (COLLAB_DIR / "inbox" / "cyc_b.json").write_text(
            json.dumps(b, ensure_ascii=False), encoding="utf-8")
        out = _parse(await dashboard.generate_dashboard())
        content = Path(out["path"]).read_text(encoding="utf-8")
        self.assertIn("cycpipe", content)
        self.assertIn("cyc_a", content)


class TestDashboardTrendV324(unittest.IsolatedAsyncioTestCase):
    """v3.24：指标看板趋势（吞吐/周期 SVG 序列，分桶时区与「今日」同源）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()

    def _put_done(self, tid, title, created_at, completed_at):
        safe_write_json(DONE_DIR / f"{tid}.json", {
            "id": tid, "title": title, "status": "done",
            "created_at": created_at, "completed_at": completed_at,
            "completed_by": "PC-A", "evidence": "e",
        })

    def test_day_series_buckets_local_days(self):
        import datetime as _dt
        from collab_mcp import dashboard as _dash

        fake_now = _dt.datetime(2026, 8, 9, 10, 0, 0,
                                tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        done = [
            {"id": "a", "created_at": "2026-08-09T00:30:00+08:00",
             "completed_at": "2026-08-09T01:00:00+08:00"},
            # UTC 2026-08-08T22:00 == 本地 08-09 06:00 → 计入 08-09（跨天同源）
            {"id": "b", "created_at": "2026-08-08T22:00:00+00:00",
             "completed_at": "2026-08-09T06:00:00+08:00"},
            {"id": "c", "created_at": "2026-08-07T09:00:00+08:00",
             "completed_at": "2026-08-07T10:00:00+08:00"},
        ]
        with patch.object(_dash, "_now", return_value=fake_now):
            series = _dash._day_series(done, days=5)
        self.assertEqual(len(series), 5)
        self.assertEqual(series[-1]["label"], "08-09")
        self.assertEqual(series[-1]["count"], 2)
        self.assertEqual(series[-1]["avg_cycle_min"], 15.0)  # (30 + 0) / 2
        self.assertEqual(series[-3]["count"], 1)  # 08-07
        self.assertEqual(series[-3]["avg_cycle_min"], 60.0)
        self.assertEqual(series[-2]["count"], 0)  # 08-08 无
        self.assertIsNone(series[-2]["avg_cycle_min"])

    def test_day_series_empty_and_svg_safe(self):
        import datetime as _dt
        from collab_mcp import dashboard as _dash

        fake_now = _dt.datetime(2026, 8, 9, 10, 0, 0,
                                tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        with patch.object(_dash, "_now", return_value=fake_now):
            series = _dash._day_series([], days=3)
            bar_svg = _dash._render_trend_svg(series, "count")
            line_svg = _dash._render_trend_svg(series, "cycle")
        self.assertEqual(len(series), 3)
        self.assertTrue(all(s["count"] == 0 for s in series))
        self.assertIn("<svg", bar_svg)
        self.assertIn("暂无数据", bar_svg)
        self.assertIn("<svg", line_svg)
        self.assertIn("暂无数据", line_svg)

    def test_trend_svg_has_data_attrs(self):
        import datetime as _dt
        from collab_mcp import dashboard as _dash

        fake_now = _dt.datetime(2026, 8, 9, 10, 0, 0,
                                tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        done = [
            {"id": "a", "created_at": "2026-08-08T09:00:00+08:00",
             "completed_at": "2026-08-08T10:00:00+08:00"},
            {"id": "b", "created_at": "2026-08-09T08:00:00+08:00",
             "completed_at": "2026-08-09T09:00:00+08:00"},
        ]
        with patch.object(_dash, "_now", return_value=fake_now):
            series = _dash._day_series(done, days=3)
            bar_svg = _dash._render_trend_svg(series, "count")
            line_svg = _dash._render_trend_svg(series, "cycle")
        self.assertIn('class="trend-bar"', bar_svg)
        self.assertIn('data-value="1"', bar_svg)
        self.assertIn('class="trend-dot"', line_svg)
        self.assertIn('class="trend-line"', line_svg)

    def test_negative_cycle_clamped_to_zero(self):
        # LOW-1（PC-C v3.24 闸门）：created_at 晚于 completed_at 的脏数据
        # 周期钳到 0，不产生负值（SVG 点不出界）
        import datetime as _dt
        from collab_mcp import dashboard as _dash

        fake_now = _dt.datetime(2026, 8, 9, 10, 0, 0,
                                tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        done = [
            {"id": "dirty", "created_at": "2026-08-09T09:00:00+08:00",
             "completed_at": "2026-08-09T08:00:00+08:00"},
        ]
        with patch.object(_dash, "_now", return_value=fake_now):
            series = _dash._day_series(done, days=1)
            svg = _dash._render_trend_svg(series, "cycle")
        self.assertEqual(series[-1]["count"], 1)
        self.assertEqual(series[-1]["avg_cycle_min"], 0.0)
        # 点 y 坐标不越出绘图区（0 值落在底部基线 146，而不是负值出上界）
        self.assertIn('cy="146.0"', svg)

    async def test_dashboard_contains_trend_section(self):
        import datetime as _dt
        from collab_mcp import dashboard as _dash

        fake_now = _dt.datetime(2026, 8, 9, 10, 0, 0,
                                tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
        self._put_done("d1", "趋势任务1", "2026-08-09T08:00:00+08:00",
                       "2026-08-09T09:00:00+08:00")
        with patch.object(_dash, "_now", return_value=fake_now):
            r = _parse(await dashboard.generate_dashboard())
        self.assertTrue(r["success"])
        content = Path(r["path"]).read_text(encoding="utf-8")
        self.assertIn("近 14 日趋势", content)
        self.assertIn("每日完成数", content)
        self.assertIn("每日平均周期", content)
        self.assertIn('class="trend-bar"', content)


class ProjectLockTest(unittest.IsolatedAsyncioTestCase):
    """第 6 项：项目级中央锁。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = os.environ.get("COLLAB_IDENTITY")

    async def asyncTearDown(self):
        if self._saved_identity is None:
            os.environ.pop("COLLAB_IDENTITY", None)
        else:
            os.environ["COLLAB_IDENTITY"] = self._saved_identity

    async def test_acquire_and_exclusive(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r = _parse(await locks.acquire_project_lock(
            "C:/shared-app", reason="重构 main.py"))
        self.assertTrue(r["success"])
        self.assertEqual(r["owner"], "PC-B")

        # 同一人重复获取 = 幂等成功
        r2 = _parse(await locks.acquire_project_lock("C:/shared-app"))
        self.assertTrue(r2["success"])

        # 其他人获取被拒
        os.environ["COLLAB_IDENTITY"] = "PC-C"
        r3 = _parse(await locks.acquire_project_lock("C:/shared-app"))
        self.assertFalse(r3["success"])
        self.assertIn("已被 PC-B 锁定", r3["error"])

    async def test_expired_lock_can_be_taken_over(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        await locks.acquire_project_lock("C:/shared-app")
        # 直接把锁改成已过期
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        import hashlib
        digest = hashlib.sha1(str(Path("C:/shared-app").resolve()).encode("utf-8")).hexdigest()[:12]
        lock_file = COLLAB_DIR / "locks" / f"{digest}.json"
        data = safe_read_json(lock_file)
        data["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        safe_write_json(lock_file, data)

        os.environ["COLLAB_IDENTITY"] = "PC-C"
        r = _parse(await locks.acquire_project_lock("C:/shared-app"))
        self.assertTrue(r["success"])
        self.assertEqual(r["owner"], "PC-C")

    async def test_release_permissions_and_list(self):
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        await locks.acquire_project_lock("C:/shared-app", reason="改接口")

        # 非属主释放被拒
        os.environ["COLLAB_IDENTITY"] = "PC-C"
        r = _parse(await locks.release_project_lock("C:/shared-app"))
        self.assertFalse(r["success"])
        self.assertIn("只有其本人或中枢", r["error"])

        # 列表能看到该锁
        rl = _parse(await locks.list_project_locks())
        self.assertEqual(rl["count"], 1)
        self.assertEqual(rl["locks"][0]["owner"], "PC-B")

        # 属主释放成功，列表清空
        os.environ["COLLAB_IDENTITY"] = "PC-B"
        r2 = _parse(await locks.release_project_lock("C:/shared-app"))
        self.assertTrue(r2["success"])
        rl2 = _parse(await locks.list_project_locks())
        self.assertEqual(rl2["count"], 0)

class TestWebResearchWigoloV36(unittest.IsolatedAsyncioTestCase):
    """v3.6.0：SSRF 硬化——web_research/web_agent 响应层 citations 校验（mock /v1/research、/v1/agent，hermetic）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
            "WIGOLO_API_TOKEN": os.environ.get("WIGOLO_API_TOKEN"),
            "WIGOLO_PROBE_TIMEOUT": os.environ.get("WIGOLO_PROBE_TIMEOUT"),
        }
        for k in self._saved:
            os.environ.pop(k, None)
        websearch._reset_wigolo_cache()

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        websearch._reset_wigolo_cache()

    def _set_wigolo(self, url="http://127.0.0.1:3333"):
        os.environ["WIGOLO_REST_URL"] = url
        os.environ["WIGOLO_PROBE_TIMEOUT"] = "1"

    def _body(self, citations, result=False):
        core = {"result" if result else "report": "## Brief\n\ncontent."}
        sources = []
        for c in citations:
            url = c.get("url", c) if isinstance(c, dict) else c
            sources.append({"url": url, "title": "S"})
        return json.dumps({**core, "citations": citations, "sources": sources}).encode("utf-8")

    # 1) research：内网 citation 剔除 + ssrf_removed=1
    async def test_research_removes_internal_citations(self):
        self._set_wigolo()
        citations = [
            {"index": 1, "url": "https://example.com/ok", "title": "OK", "snippet": "s"},
            {"index": 2, "url": "http://192.168.1.1/admin", "title": "内网", "snippet": "s"},
        ]
        body = self._body(citations)
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["ssrf_removed"], 1)
        self.assertEqual(len(r["citations"]), 1)
        self.assertEqual(r["citations"][0]["url"], "https://example.com/ok")

    # 2) research：混合多类内网/保留/非法 scheme → 只留 public，计数正确
    async def test_research_mixed_internal_kinds(self):
        self._set_wigolo()
        citations = [
            {"index": 1, "url": "https://example.com/ok", "title": "OK", "snippet": "s"},
            {"index": 2, "url": "http://10.0.0.5/x", "title": "私网", "snippet": "s"},
            {"index": 3, "url": "http://127.0.0.1/x", "title": "环回", "snippet": "s"},
            {"index": 4, "url": "http://169.254.169.254/latest/meta-data", "title": "metadata", "snippet": "s"},
            {"index": 5, "url": "javascript:alert(1)", "title": "非法 scheme", "snippet": "s"},
        ]
        body = self._body(citations)
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["ssrf_removed"], 4)
        self.assertEqual([c["url"] for c in r["citations"]], ["https://example.com/ok"])

    # 3) research：全 public → 原样保留，ssrf_removed=0
    async def test_research_all_public_kept(self):
        self._set_wigolo()
        citations = [
            {"index": 1, "url": "https://example.com/a", "title": "A", "snippet": "s"},
            {"index": 2, "url": "https://docs.python.org/3/", "title": "B", "snippet": "s"},
        ]
        body = self._body(citations)
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["ssrf_removed"], 0)
        self.assertEqual(len(r["citations"]), 2)

    # 4) research：空 citations → 不崩，ssrf_removed=0
    async def test_research_empty_citations(self):
        self._set_wigolo()
        body = self._body([])
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["ssrf_removed"], 0)
        self.assertEqual(r["citations"], [])

    # 5) agent：同样路径生效（result 字段 + 内网剔除）
    async def test_agent_removes_internal_citations(self):
        self._set_wigolo()
        citations = [
            {"index": 1, "url": "https://example.com/a", "title": "A", "snippet": "s"},
            {"index": 2, "url": "http://172.16.0.9/x", "title": "内网", "snippet": "s"},
        ]
        body = self._body(citations, result=True)
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_agent("p"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["mode"], "agent")
        self.assertEqual(r["ssrf_removed"], 1)
        self.assertEqual([c["url"] for c in r["citations"]], ["https://example.com/a"])

    # 6) 字符串元素形态兼容
    async def test_string_citation_elements_compatible(self):
        self._set_wigolo()
        citations = ["https://example.com/a", "http://10.0.0.5/"]
        body = self._body(citations)
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["ssrf_removed"], 1)
        self.assertEqual(r["citations"], ["https://example.com/a"])

    # 7) 输出结构齐全（含新增 ssrf_removed 字段，向后兼容）
    async def test_output_structure_has_ssrf_removed(self):
        self._set_wigolo()
        body = self._body([
            {"index": 1, "url": "https://example.com/1", "title": "T1", "snippet": "s1"},
        ])
        async def fake_fetch(url, timeout, **kw):
            return (url, body)
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._fetch_raw", new=fake_fetch):
            r = _parse(await websearch.web_research("q"))
        self.assertTrue(r["success"], r)
        for key in ("provider", "mode", "report", "truncated", "citations", "sources_count",
                    "heuristic", "chars", "estimated_tokens", "ssrf_removed"):
            self.assertIn(key, r)
        self.assertEqual(r["ssrf_removed"], 0)



class TestSummarizeV37(unittest.IsolatedAsyncioTestCase):
    """v3.7.0：summarize_text 摘要工具（提取式兜底 + LLM 可选；LLM mock hermetic）。"""

    ZH_TEXT = (
        "量子计算是一种利用量子力学原理进行计算的新型计算范式。"
        "它与传统计算机有本质区别，能够同时处理多个状态。"
        "量子比特是量子计算的基本单位。"
        "近年来量子计算取得了显著进展。"
        "多家公司发布了量子计算原型机。"
        "量子计算在密码学和材料科学中有广阔应用前景。"
    )
    EN_TEXT = (
        "Quantum computing is a new paradigm that leverages quantum mechanics. "
        "It differs fundamentally from classical computers. "
        "Qubits are the basic unit of quantum information. "
        "Recent years have seen remarkable progress in quantum hardware. "
        "Quantum computing promises breakthroughs in cryptography and materials science."
    )

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved = {}
        for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                  "OLLAMA_BASE_URL", "OLLAMA_MODEL"):
            self._saved[k] = os.environ.get(k)
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 0) v3.36.4：ollama 分支默认模型必须为本机已装的 qwen2.5:3b（曾误写 qwen2.5:7b）
    async def test_ollama_default_model_and_override(self):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "摘要"}}]}).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        with patch("urllib.request.urlopen", new=_fake_urlopen):
            r = _parse(await summarize.summarize_text(self.ZH_TEXT, backend="llm"))
            self.assertTrue(r["success"], r)
            self.assertEqual(captured["body"]["model"], "qwen2.5:3b")

            os.environ["OLLAMA_MODEL"] = "my-custom:1b"
            r2 = _parse(await summarize.summarize_text(self.ZH_TEXT, backend="llm"))
            self.assertTrue(r2["success"], r2)
            self.assertEqual(captured["body"]["model"], "my-custom:1b")

    # 1) 中文提取式：method=extractive、关键内容保留、句子来自原文、长度受控
    async def test_zh_extractive(self):
        r = _parse(await summarize.summarize_text(self.ZH_TEXT))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "extractive")
        self.assertIn("量子计算", r["summary"])
        self.assertLessEqual(r["chars"], 1500)
        # 摘要中的句子均为原文子串（提取式不回写）
        self.assertTrue(all(s in self.ZH_TEXT for s in r["summary"].split("。") if s))

    # 2) 英文提取式
    async def test_en_extractive(self):
        r = _parse(await summarize.summarize_text(self.EN_TEXT))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "extractive")
        self.assertIn("Quantum", r["summary"])
        self.assertLessEqual(r["chars"], 1500)

    # 3) auto 且无 LLM 配置 → 降级 extractive（setUp 已清 LLM env）
    async def test_auto_falls_back_to_extractive(self):
        r = _parse(await summarize.summarize_text(self.EN_TEXT))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "extractive")

    # 4) auto + LLM 配置 → llm 方法（mock 同步函数，to_thread 兼容）
    async def test_auto_uses_llm_when_configured(self):
        with patch("collab_mcp.summarize._llm_configured", return_value="ollama"), \
             patch("collab_mcp.summarize._llm_summarize", new=lambda t, l, m, to: ("LLM 摘要内容", "ollama")):
            r = _parse(await summarize.summarize_text(self.EN_TEXT))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "llm")
        self.assertEqual(r["summary"], "LLM 摘要内容")

    # 5) backend=llm 无配置 → 明确失败
    async def test_llm_backend_without_config_fails(self):
        r = _parse(await summarize.summarize_text(self.EN_TEXT, backend="llm"))
        self.assertFalse(r["success"], r)
        self.assertIn("未配置 LLM", r["error"])

    # 6) 空文本 → 明确失败
    async def test_empty_text_fails(self):
        r = _parse(await summarize.summarize_text("   "))
        self.assertFalse(r["success"], r)
        self.assertIn("非空 text", r["error"])

    # 7) 参数钳制：max_chars 钳到 [100, 8000]；非法值回默认
    async def test_clamps(self):
        r = _parse(await summarize.summarize_text(self.EN_TEXT, max_chars=99999))
        self.assertTrue(r["success"], r)
        self.assertLessEqual(r["chars"], 8000)
        r2 = _parse(await summarize.summarize_text(self.EN_TEXT, max_chars=5))
        self.assertTrue(r2["success"], r2)
        self.assertLessEqual(r2["chars"], 100)
        r3 = _parse(await summarize.summarize_text(self.EN_TEXT, max_chars="abc"))
        self.assertTrue(r3["success"], r3)
        self.assertLessEqual(r3["chars"], 1500)

    # 8) 身份闸门（REQUIRE_IDENTITY=1 无身份被拒）
    async def test_identity_guard(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await summarize.summarize_text(self.EN_TEXT))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved

    # 9) 超长文本 → LLM 路径截断标注 truncated
    async def test_long_text_truncated_flag(self):
        long_text = "量子计算" * 7000  # > 12000 字符
        with patch("collab_mcp.summarize._llm_configured", return_value="ollama"), \
             patch("collab_mcp.summarize._llm_summarize", new=lambda t, l, m, to: ("LLM 摘要内容", "ollama")):
            r = _parse(await summarize.summarize_text(long_text))
        self.assertTrue(r["success"], r)
        self.assertTrue(r["truncated"])



    # 低-1 回归（PC-C 闸门）：OPENAI_BASE_URL 带 /v1 时归一化，不拼 /v1/v1/
    async def test_openai_base_url_v1_normalized(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_BASE_URL"] = "http://myhost:8000/v1"
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

        def fake_urlopen(req, timeout=60):
            captured["url"] = req.full_url
            return FakeResp()

        with patch("urllib.request.urlopen", new=fake_urlopen):
            r = _parse(await summarize.summarize_text(self.EN_TEXT, backend="llm"))
        self.assertTrue(r["success"], r)
        self.assertEqual(captured["url"], "http://myhost:8000/v1/chat/completions")

    # 10) LLM HTTP 层 mock（低2 PC-C）：payload 结构 / temperature / max_tokens / 返回解析
    async def test_llm_http_layer(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "HTTP 摘要结果"}}]}).encode("utf-8")

        def fake_urlopen(req, timeout=60):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            captured["auth"] = req.get_header("Authorization")
            return FakeResp()

        with patch("urllib.request.urlopen", new=fake_urlopen):
            r = _parse(await summarize.summarize_text(self.EN_TEXT, backend="llm"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "llm")
        self.assertEqual(r["llm_backend"], "openai")
        self.assertEqual(r["summary"], "HTTP 摘要结果")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/chat/completions")
        self.assertNotIn("/v1/v1/", captured["url"])
        self.assertEqual(captured["auth"], "Bearer sk-test")
        self.assertEqual(captured["data"]["temperature"], 0.3)
        self.assertGreaterEqual(captured["data"]["max_tokens"], 256)
        self.assertEqual(captured["data"]["messages"][0]["role"], "system")
        self.assertEqual(captured["data"]["messages"][1]["role"], "user")

    # 11) LLM HTTP 层错误映射（低2 PC-C）：HTTPError → 明确失败
    async def test_llm_http_error(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        import urllib.error

        def fake_urlopen(req, timeout=60):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with patch("urllib.request.urlopen", new=fake_urlopen):
            r = _parse(await summarize.summarize_text(self.EN_TEXT, backend="llm"))
        self.assertFalse(r["success"], r)
        self.assertIn("HTTP 401", r["error"])


class TestHealthMonitorV38(unittest.IsolatedAsyncioTestCase):
    """v3.8.0：主动监控 health_monitor（mock 检查项，hermetic，零真实网络）。"""

    def _hm(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import health_monitor as hm
        return hm

    def _cfg(self, hm, tmp):
        return {
            "repo": str(tmp),
            "state": str(tmp / "state.json"),
            "log": str(tmp / "health-monitor.log"),
            "feed": str(tmp / "feed.jsonl"),
            "wigolo_url": "http://127.0.0.1:3333/health",
            "disk_min_gb": 5.0,
            "inbox_max": 20,
            "inbox_dir": str(tmp / "inbox"),
        }

    def _patchers(self, hm, overrides=None):
        defaults = {
            "check_wigolo": (True, "healthy"),
            "check_scrapling": (True, "import OK"),
            "check_git": (True, "sync"),
            "check_disk": (True, "100.0 GB"),
            "check_inbox": (True, "0 个待办"),
        }
        defaults.update(overrides or {})
        return [patch.object(hm, name, return_value=val) for name, val in defaults.items()]

    # 1) 全 OK：all_ok=True、无 feed 事件（首次 ok 静默基线）、state 全 ok
    async def test_all_ok(self):
        hm = self._hm()
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(hm, Path(td))
            import contextlib
            with contextlib.ExitStack() as st:
                for p in self._patchers(hm):
                    st.enter_context(p)
                r = hm.run_once(cfg)
                events = hm.emit(cfg, r)
            self.assertTrue(r["all_ok"], r)
            self.assertEqual(events, [])
            state = json.loads(Path(cfg["state"]).read_text(encoding="utf-8"))
            self.assertTrue(all(v == "ok" for v in state["last_status"].values()))
            self.assertFalse(Path(cfg["feed"]).exists())

    # 2) wigolo fail → health_alert 事件 + feed 落盘
    async def test_wigolo_fail_emits_alert(self):
        hm = self._hm()
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(hm, Path(td))
            import contextlib
            with contextlib.ExitStack() as st:
                for p in self._patchers(hm, {"check_wigolo": (False, "Connection refused")}):
                    st.enter_context(p)
                r = hm.run_once(cfg)
                events = hm.emit(cfg, r)
            self.assertFalse(r["all_ok"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "health_alert")
            self.assertEqual(events[0]["check"], "wigolo")
            self.assertEqual(events[0]["status"], "fail")
            feed = Path(cfg["feed"]).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(feed), 1)

    # 3) 去重：同 fail 连续两次仅一条事件
    async def test_dedup_same_fail(self):
        hm = self._hm()
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(hm, Path(td))
            import contextlib
            with contextlib.ExitStack() as st:
                for p in self._patchers(hm, {"check_wigolo": (False, "down")}):
                    st.enter_context(p)
                e1 = hm.emit(cfg, hm.run_once(cfg))
                e2 = hm.emit(cfg, hm.run_once(cfg))
            self.assertEqual(len(e1), 1)
            self.assertEqual(e2, [])

    # 4) 恢复：fail→ok 翻转写 status=ok 事件
    async def test_recovery_emits_ok(self):
        hm = self._hm()
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(hm, Path(td))
            import contextlib
            with contextlib.ExitStack() as st:
                for p in self._patchers(hm, {"check_wigolo": (False, "down")}):
                    st.enter_context(p)
                e1 = hm.emit(cfg, hm.run_once(cfg))
            with contextlib.ExitStack() as st:
                for p in self._patchers(hm, {"check_wigolo": (True, "healthy")}):
                    st.enter_context(p)
                e2 = hm.emit(cfg, hm.run_once(cfg))
            self.assertEqual(len(e1), 1)
            self.assertEqual(len(e2), 1)
            self.assertEqual(e2[0]["check"], "wigolo")
            self.assertEqual(e2[0]["status"], "ok")

    # 5) 各 fail 项（git/inbox/disk/scrapling）均产生对应事件
    async def test_other_fail_checks(self):
        hm = self._hm()
        cases = [
            ("git", "check_git", (False, "HEAD!=origin/master")),
            ("inbox", "check_inbox", (False, "25 个待办（阈值 20）")),
            ("disk", "check_disk", (False, "2.0 GB（阈值 5 GB）")),
            ("scrapling", "check_scrapling", (False, "未安装")),
        ]
        import contextlib
        for name, attr, ret in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                cfg = self._cfg(hm, Path(td))
                with contextlib.ExitStack() as st:
                    for p in self._patchers(hm, {attr: ret}):
                        st.enter_context(p)
                    r = hm.run_once(cfg)
                    events = hm.emit(cfg, r)
                self.assertFalse(r["all_ok"])
                self.assertEqual(len(events), 1, name)
                self.assertEqual(events[0]["check"], name)

    # 6) inbox 真实阈值：inbox_max 覆盖生效（临时目录 3 个文件，阈值 2）
    async def test_inbox_threshold(self):
        hm = self._hm()
        with tempfile.TemporaryDirectory() as td:
            inbox = Path(td) / "inbox"
            inbox.mkdir()
            for i in range(3):
                (inbox / f"task{i}.json").write_text("{}", encoding="utf-8")
            ok, detail = hm.check_inbox(str(inbox), 2)
            self.assertFalse(ok)
            self.assertIn("3 个待办", detail)
            ok2, _ = hm.check_inbox(str(inbox), 5)
            self.assertTrue(ok2)

    # 7) env 覆盖 default_cfg
    async def test_env_overrides(self):
        hm = self._hm()
        saved = {k: os.environ.get(k) for k in ("HM_DISK_MIN_GB", "HM_INBOX_MAX", "HM_WIGOLO_URL", "HM_REPO")}
        try:
            os.environ["HM_DISK_MIN_GB"] = "3"
            os.environ["HM_INBOX_MAX"] = "5"
            os.environ["HM_WIGOLO_URL"] = "http://127.0.0.1:9999/health"
            os.environ["HM_REPO"] = "C:/tmp"
            cfg = hm.default_cfg()
            self.assertEqual(cfg["disk_min_gb"], 3.0)
            self.assertEqual(cfg["inbox_max"], 5)
            self.assertEqual(cfg["wigolo_url"], "http://127.0.0.1:9999/health")
            self.assertEqual(cfg["repo"], "C:/tmp")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # 8) log_result：异常项写 ALERT 行
    async def test_log_result_alert(self):
        hm = self._hm()
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(hm, Path(td))
            import contextlib
            with contextlib.ExitStack() as st:
                for p in self._patchers(hm, {"check_wigolo": (False, "down")}):
                    st.enter_context(p)
                r = hm.run_once(cfg)
                events = hm.emit(cfg, r)
                hm.log_result(cfg, r, events)
            log = Path(cfg["log"]).read_text(encoding="utf-8")
            self.assertIn("ALERT", log)
            self.assertIn("wigolo", log)



    # 9) _find_chromium 对齐 v3.4 glob 语义（低1 PC-C）：chromium-<build>/chrome-win64 可探测
    async def test_find_chromium_glob(self):
        hm = self._hm()
        saved = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        with tempfile.TemporaryDirectory() as td:
            try:
                root = Path(td) / "ms-playwright"
                (root / "chromium-1199" / "chrome-win64").mkdir(parents=True)
                (root / "chromium-1199" / "chrome-win64" / "chrome.exe").write_text("", encoding="utf-8")
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
                self.assertIsNotNone(hm._find_chromium())
                # 空目录 → None
                empty = Path(td) / "empty"
                empty.mkdir()
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(empty)
                self.assertIsNone(hm._find_chromium())
            finally:
                if saved is None:
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
                else:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved


class TestSemanticSearchV39(unittest.IsolatedAsyncioTestCase):
    """v3.9.0：向量语义检索 semantic_search（mock ollama embedding，hermetic，零真实网络）。"""

    V_Q = [1.0, 0.0, 0.0]
    V_QUANTUM = [0.9, 0.1, 0.0]
    V_WEATHER = [0.0, 0.0, 1.0]

    async def asyncSetUp(self):
        _reset_collab_dir()
        semantic._close_index()
        semantic._STATE.clear()
        for d in (DOCS_DIR, semantic._SHARED_ROOT / "notes"):
            if d.is_dir():
                for f in list(d.rglob("*.md")):
                    f.unlink()
        self._saved = {k: os.environ.get(k) for k in ("OLLAMA_BASE_URL", "OLLAMA_EMBED_MODEL")}
        for k in self._saved:
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def _ingest(self, name, body, target_dir=""):
        src = COLLAB_DIR / name
        src.write_text(body, encoding="utf-8")
        r = _parse(await documents.add_document(str(src), target_dir=target_dir))
        self.assertTrue(r["success"], r)
        return r

    def _vec_for(self, text):
        """规则向量：query 精确匹配；文档块按内容包含匹配（块含标题前缀）。"""
        if text == "量子":
            return self.V_Q
        if "量子纠错" in text or "量子加速" in text:
            return self.V_QUANTUM
        if "天气" in text:
            return self.V_WEATHER
        return [0.0, 0.0, 0.0]

    def _mock_ready_and_embed(self):
        p1 = patch.object(semantic, "_ollama_embed_ready", return_value=True)
        def fake_embed(texts, timeout=30):
            return [self._vec_for(t) for t in texts]
        p2 = patch.object(semantic, "_embed", new=fake_embed)
        return p1, p2

    # 1) ollama embedding 不可用 → 明确失败
    async def test_not_ready_fails(self):
        with patch.object(semantic, "_ollama_embed_ready", return_value=False):
            r = _parse(await semantic.semantic_search("量子"))
        self.assertFalse(r["success"], r)
        self.assertIn("ollama", r["error"])

    # 2) 语义排序：量子文档高分在前 + 输出结构
    async def test_semantic_ranking(self):
        await self._ingest("quantum.md", "# 量子\n\n量子纠错和量子加速")
        await self._ingest("weather.md", "# 天气\n\n今天天气很好")
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "semantic")
        self.assertGreaterEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["path"], "documents/quantum.md")
        self.assertGreater(r["results"][0]["score"], 0.5)
        for key in ("method", "query", "scope", "results", "indexed_chunks", "count"):
            self.assertIn(key, r)

    # 3) min_score 过滤低分结果
    async def test_min_score_filter(self):
        await self._ingest("quantum.md", "# 量子\n\n量子纠错和量子加速")
        await self._ingest("weather.md", "# 天气\n\n今天天气很好")
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子", min_score=0.5))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["path"], "documents/quantum.md")

    # 4) v3.20.2：documents/ 下无头普通 md 也入语义索引（复用 _read_doc_file）
    async def test_plain_md_in_documents_indexed(self):
        (DOCS_DIR / "plain.md").write_text(
            "# 手写笔记\n\n量子纠错原理笔记\n", encoding="utf-8"
        )
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子"))
        self.assertTrue(r["success"], r)
        self.assertGreaterEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["path"], "documents/plain.md")

    # 5) v3.21：默认 min_score=0.3 过滤零分/低置信度块；显式 0 恢复全量
    async def test_default_min_score_filters_low(self):
        await self._ingest("quantum.md", "# 量子\n\n量子纠错和量子加速")
        await self._ingest("weather.md", "# 天气\n\n今天天气很好")
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["path"], "documents/quantum.md")
        with p1, p2:
            r0 = _parse(await semantic.semantic_search("量子", min_score=0.0))
        self.assertEqual(r0["count"], 2)

    # 6) v3.21：默认 documents scope 排除 media/ 噪音；显式 scope 可检索
    async def test_media_excluded_from_default_scope(self):
        await self._ingest("a.md", "# 量子\n\n量子纠错和量子加速")
        media_dir = DOCS_DIR / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "m.md").write_text(
            "# 量子\n\n量子纠错和量子加速\n", encoding="utf-8"
        )
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["results"][0]["path"], "documents/a.md")
        with p1, p2:
            r_m = _parse(
                await semantic.semantic_search("量子", target_dir="documents/media")
            )
        self.assertGreaterEqual(r_m["count"], 1)
        self.assertTrue(r_m["results"][0]["path"].startswith("documents/media/"))

    # 7) v3.21：embedding 模型选择——qwen3 首选、nomic 回退、env 强制
    def test_select_embed_model_prefers_qwen3(self):
        from collab_mcp.semantic import (
            _EMBED_MODEL_DEFAULT,
            _EMBED_MODEL_PREFERRED,
            _select_embed_model,
        )

        saved = os.environ.get("OLLAMA_EMBED_MODEL")
        os.environ.pop("OLLAMA_EMBED_MODEL", None)
        try:
            self.assertEqual(
                _select_embed_model(["nomic-embed-text:latest"]),
                _EMBED_MODEL_DEFAULT,
            )
            self.assertEqual(
                _select_embed_model(
                    ["qwen3-embedding:0.6b", "nomic-embed-text:latest"]
                ),
                _EMBED_MODEL_PREFERRED,
            )
            os.environ["OLLAMA_EMBED_MODEL"] = "bge-m3"
            self.assertEqual(
                _select_embed_model(["nomic-embed-text:latest"]), "bge-m3"
            )
        finally:
            if saved is None:
                os.environ.pop("OLLAMA_EMBED_MODEL", None)
            else:
                os.environ["OLLAMA_EMBED_MODEL"] = saved

    # 8) v3.21：scope 索引签名必须含模型名——换模型（维度变化）必须触发重建
    def test_scope_signature_includes_model(self):
        from collab_mcp.semantic import _scope_signature

        with patch.object(semantic, "_embed_model", return_value="qwen3-embedding:0.6b"):
            s1 = _scope_signature(Path("x"))
        with patch.object(semantic, "_embed_model", return_value="nomic-embed-text"):
            s2 = _scope_signature(Path("x"))
        self.assertNotEqual(s1, s2)

    # 4) target_dir 防穿越 / 不存在
    async def test_scope_guard_and_missing(self):
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r_bad = _parse(await semantic.semantic_search("量子", target_dir="../escape"))
            r_missing = _parse(await semantic.semantic_search("量子", target_dir="nope"))
        self.assertFalse(r_bad["success"])
        self.assertIn("超出共享目录", r_bad["error"])
        self.assertTrue(r_missing["success"])
        self.assertEqual(r_missing["count"], 0)

    # 5) 空目录 → count=0
    async def test_empty_scope(self):
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子", target_dir="documents"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["indexed_chunks"], 0)

    # 6) limit 截断
    async def test_limit(self):
        await self._ingest("a.md", "# A\n\n量子纠错和量子加速")
        await self._ingest("b.md", "# B\n\n量子纠错和量子加速")
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r = _parse(await semantic.semantic_search("量子", limit=1))
        self.assertEqual(r["count"], 1)

    # 7) 身份闸门
    async def test_identity_guard(self):
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await semantic.semantic_search("量子"))
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
        finally:
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved

    # 8) 签名缓存：新增文档 → 自动重建（indexed_chunks 增加）
    async def test_signature_rebuild(self):
        await self._ingest("a.md", "# A\n\n量子纠错和量子加速")
        p1, p2 = self._mock_ready_and_embed()
        with p1, p2:
            r1 = _parse(await semantic.semantic_search("量子"))
        self.assertEqual(r1["indexed_chunks"], 1)
        await self._ingest("b.md", "# B\n\n今天天气很好")
        p3, p4 = self._mock_ready_and_embed()
        with p3, p4:
            r2 = _parse(await semantic.semantic_search("量子"))
        self.assertEqual(r2["indexed_chunks"], 2)



    # 9) sqlite3 不可用（VM 定制 Python 缺 _sqlite3）→ 明确失败，server 启动不受影响
    async def test_sqlite_unavailable_fails(self):
        with patch.object(semantic, "_ollama_embed_ready", return_value=True), \
             patch.object(semantic, "_sqlite_module", return_value=None):
            r = _parse(await semantic.semantic_search("量子"))
        self.assertFalse(r["success"], r)
        self.assertIn("sqlite3", r["error"])


    # 10) 低2（PC-C）：ollama 返回非 JSON → 明确失败（不逃出框架内部错误）
    async def test_embed_nonjson_fails(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"<html>502 Bad Gateway</html>"

        def fake_urlopen(req, timeout=5):
            return FakeResp()

        with patch.object(semantic, "_ollama_embed_ready", return_value=True), \
             patch("urllib.request.urlopen", new=fake_urlopen):
            r = _parse(await semantic.semantic_search("量子"))
        self.assertFalse(r["success"], r)
        self.assertIn("非 JSON", r["error"])

    # 11) 低3（PC-C）：ready 失败态不缓存——ollama 晚启动后下次调用可恢复
    async def test_ready_recovers(self):
        def fake_urlopen(req, timeout=5):
            if not hasattr(fake_urlopen, "called"):
                fake_urlopen.called = True
                raise OSError("connection refused")
            import json as _json
            return _json.dumps({"models": [{"name": "nomic-embed-text:latest"}]}).encode("utf-8")

        # 注意：urlopen 返回 str/bytes，需适配 .read()——用对象包装
        class Resp:
            def __init__(self, payload):
                self._p = payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._p

        state = {"n": 0}

        def fake_urlopen2(req, timeout=5):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("connection refused")
            return Resp(b'{"models": [{"name": "nomic-embed-text:latest"}]}')

        with patch("urllib.request.urlopen", new=fake_urlopen2):
            self.assertFalse(semantic._ollama_embed_ready())
            # 失败态不缓存：第二次（ollama 已恢复）→ True
            self.assertTrue(semantic._ollama_embed_ready())


    # 12) per-scope 索引（v3.10 PC-C 建议 b）：scope A→B→A 切换不重建（仅 query embedding 开销）
    async def test_scope_switch_no_rebuild(self):
        await self._ingest("a.md", "# A\n\n量子纠错和量子加速")
        await self._ingest("n.md", "# N\n\n今天天气很好", target_dir="notes")

        def fake_embed(texts, timeout=30):
            return [self._vec_for(t) for t in texts]

        with patch.object(semantic, "_ollama_embed_ready", return_value=True), \
             patch.object(semantic, "_embed", side_effect=fake_embed) as m:
            r_docs = _parse(await semantic.semantic_search("量子", target_dir="documents"))
            self.assertEqual(r_docs["indexed_chunks"], 1)
            after_docs = len(m.call_args_list)
            r_notes = _parse(await semantic.semantic_search("量子", target_dir="notes"))
            self.assertEqual(r_notes["indexed_chunks"], 1)
            after_notes = len(m.call_args_list)
            r_docs2 = _parse(await semantic.semantic_search("量子", target_dir="documents"))
            self.assertEqual(r_docs2["indexed_chunks"], 1)
            after_docs2 = len(m.call_args_list)
        # 切回 documents 不重建：仅 +1 次 query embedding 调用（索引块不重复 embed）
        self.assertEqual(after_docs2 - after_notes, 1)


class TestGateChecklistV311(unittest.IsolatedAsyncioTestCase):
    """v3.11.0：验证闸门确定性层 gate_checklist（mock git，hermetic）。"""

    def _gc(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import gate_checklist as gc
        return gc

    DIFF = """diff --git a/collab/collab_mcp/semantic.py b/collab/collab_mcp/semantic.py
--- a/collab/collab_mcp/semantic.py
+++ b/collab/collab_mcp/semantic.py
@@ -190,0 +191,7 @@
+    if _sqlite_module() is None:
+        return fail("semantic_search 需要 sqlite3")
+    if not _ollama_embed_ready():
+        return fail("semantic_search 需要 ollama embedding")
+    try:
+        limit = max(1, min(int(limit), 50))
+    except (TypeError, ValueError):
+        limit = 10
diff --git a/collab/tests/test_server.py b/collab/tests/test_server.py
--- a/collab/tests/test_server.py
+++ b/collab/tests/test_server.py
@@ -5810,0 +5811,3 @@
+    async def test_sqlite_unavailable_fails(self):
+        with patch.object(semantic, "_ollama_embed_ready", return_value=True):
+            r = _parse(await semantic.semantic_search("量子"))
"""

    # 1) diff -U0 解析：新增行号集
    def test_parse_diff_changed_lines(self):
        gc = self._gc()
        result = gc.parse_diff_changed_lines(self.DIFF)
        self.assertIn("collab/collab_mcp/semantic.py", result)
        self.assertEqual(result["collab/collab_mcp/semantic.py"], set(range(191, 198)))
        self.assertEqual(result["collab/tests/test_server.py"], set(range(5811, 5814)))

    # 2) 提取意见位置
    def test_extract_positions(self):
        gc = self._gc()
        report = "问题在 collab/collab_mcp/semantic.py:192；另见 semantic.py:199 与 tests/test_server.py:5812"
        pos = gc.extract_positions(report)
        self.assertIn(("collab/collab_mcp/semantic.py", 192), pos)
        self.assertIn(("semantic.py", 199), pos)
        self.assertIn(("tests/test_server.py", 5812), pos)

    # 3) 覆盖核对：全覆盖 vs 漏文件
    def test_check_coverage(self):
        gc = self._gc()
        files = ["collab/collab_mcp/semantic.py", "collab/tests/test_server.py", "progress/x.md"]
        report_full = "已审查 semantic.py 和 test_server.py，progress/x.md 也已核对"
        covered, missing = gc.check_coverage(files, report_full)
        self.assertEqual(missing, [])
        self.assertEqual(len(covered), 3)
        report_partial = "只审查了 semantic.py"
        covered2, missing2 = gc.check_coverage(files, report_partial)
        self.assertEqual(missing2, ["collab/tests/test_server.py", "progress/x.md"])

    # 4) 位置校验：在变更行内 vs 漂移
    def test_check_positions(self):
        gc = self._gc()
        changed = gc.parse_diff_changed_lines(self.DIFF)
        ok_list, drifted = gc.check_positions(changed, [
            ("collab/collab_mcp/semantic.py", 192),
            ("semantic.py", 999),  # 漂移
            ("collab/tests/test_server.py", 5812),
        ])
        self.assertEqual(len(ok_list), 2)
        self.assertEqual(drifted, [("semantic.py", 999)])

    # 5) 分类
    def test_categorize(self):
        gc = self._gc()
        src, tests, docs = gc.categorize([
            "collab/collab_mcp/semantic.py", "collab/tests/test_server.py",
            "progress/collab-v3.11-gate-checklist-design.md", "README.md",
        ])
        self.assertEqual(src, ["collab/collab_mcp/semantic.py"])
        self.assertEqual(tests, ["collab/tests/test_server.py"])
        self.assertEqual(docs, ["progress/collab-v3.11-gate-checklist-design.md", "README.md"])

    # 6) 建议测试类：映射命中 + 兜底
    def test_suggest_tests(self):
        gc = self._gc()
        sug = gc.suggest_tests(["collab/collab_mcp/semantic.py"], str(REPO_DIR / "tests" / "test_server.py"))
        self.assertIn("TestSemanticSearchV39", sug)
        all_classes = gc.list_test_classes(str(REPO_DIR / "tests" / "test_server.py"))
        self.assertIn("TestGateChecklistV311", all_classes)
        # 未知模块 → 兜底全列表
        sug2 = gc.suggest_tests(["collab/collab_mcp/nope.py"], str(REPO_DIR / "tests" / "test_server.py"))
        self.assertEqual(sug2, all_classes)

    # 7) CLI --changes：mock git → 输出清单
    async def test_cli_changes(self):
        gc = self._gc()
        with patch.object(gc, "git_output", return_value="collab/collab_mcp/semantic.py\ncollab/tests/test_server.py\n"):
            out = gc.render_changes("C:/repo", "abc..def", str(REPO_DIR / "tests" / "test_server.py"))
        self.assertIn("变更文件总数: 2", out)
        self.assertIn("collab/collab_mcp/semantic.py", out)
        self.assertIn("TestSemanticSearchV39", out)

    # 8) CLI --verify：mock git + 临时报告 → 核对结果
    async def test_cli_verify(self):
        gc = self._gc()
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "report.md"
            report.write_text("已审查 collab/collab_mcp/semantic.py，见 semantic.py:192", encoding="utf-8")
            with patch.object(gc, "git_output", side_effect=[
                "collab/collab_mcp/semantic.py\n",
                self.DIFF,
            ]):
                out = gc.render_verify("C:/repo", "abc..def", str(report))
        self.assertIn("变更文件: 1", out)
        self.assertIn("漏审候选: 0", out)
        self.assertIn("漂移候选: 0", out)

    # 9) 报告不存在 → 明确错误
    async def test_verify_missing_report(self):
        gc = self._gc()
        with self.assertRaises(RuntimeError):
            gc.render_verify("C:/repo", "abc..def", "C:/nonexistent/report.md")



    # 10) 兼容性回归（PC-C v3.11 阻断项）：git_output 用 cwd=repo 而非 -C（VM git 1.8.3.1）
    async def test_git_output_uses_cwd(self):
        gc = self._gc()

        class FakeProc:
            returncode = 0
            stdout = b"collab/collab_mcp/semantic.py\n"
            stderr = b""

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["cwd"] = kw.get("cwd")
            return FakeProc()

        with patch.object(gc.subprocess, "run", side_effect=fake_run):
            out = gc.git_output("C:/repo", ["diff", "--name-only", "a..b"])
        self.assertEqual(out, "collab/collab_mcp/semantic.py\n")
        self.assertEqual(captured["cwd"], "C:/repo")
        self.assertNotIn("-C", captured["cmd"])







class TestVisionV312(unittest.IsolatedAsyncioTestCase):
    """v3.12.0：眼睛-大脑 Step1 —— analyze_observation（mock LLM，hermetic，无相机/无网络）。"""

    OBS = {
        "schema": "collab-vision-observation-v1",
        "observation_id": "obs-test-000001",
        "timestamp": "2026-08-07T10:00:00+08:00",
        "source": "demo",
        "image_path": "",
        "synthetic": True,
        "stats": {"width": 640, "height": 480, "brightness": 0.62, "motion": 0.0, "dominant_color": "#4080c0"},
        "detections": [],
        "capture_duration_ms": 0,
        "notes": [],
    }

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._tmp = Path(tempfile.mkdtemp(prefix="collab_vision_test_"))
        os.environ["COLLAB_VISION_DIR"] = str(self._tmp)
        self._saved = {k: os.environ.get(k) for k in (
            "COLLAB_VISION_DIR", "OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_VISION_MODEL",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
        )}
        for k in self._saved:
            if k != "COLLAB_VISION_DIR":
                os.environ.pop(k, None)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_obs(self, obs=None, name="latest.json"):
        data = obs if obs is not None else json.loads(json.dumps(self.OBS))
        p = self._tmp / name
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def _fake_urlopen(self, payload_text, captured=None):
        class Resp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        def fake(req, timeout=5):
            if captured is not None:
                captured["body"] = json.loads(req.data.decode("utf-8"))
            return Resp(json.dumps(
                {"choices": [{"message": {"content": payload_text}}]}
            ).encode("utf-8"))
        return fake

    # 1) 无 LLM 配置 → auto 走 heuristic（零 API 成本）
    async def test_heuristic_default(self):
        self._write_obs()
        r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")
        self.assertEqual(r["source"], "demo")
        self.assertIn("静态场景", r["interpretation"])
        self.assertIsInstance(r["suggested_actions"], list)
        self.assertTrue(r["suggested_actions"])
        self.assertIn("observation_id", r)
        self.assertIn("detection_count", r)

    # 2) 人脸检测 → heuristic 置信度 medium + 动作含人脸
    async def test_heuristic_face(self):
        obs = json.loads(json.dumps(self.OBS))
        obs["detections"] = [{"kind": "face", "confidence": 0.5, "bbox": [10, 20, 100, 100]}]
        self._write_obs(obs)
        r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")
        self.assertIn("人脸", r["interpretation"])
        self.assertIn("人脸", "".join(r["suggested_actions"]))
        self.assertEqual(r["confidence"], "medium")

    # 3) 光线偏暗 → 动作含补光建议
    async def test_heuristic_dark(self):
        obs = json.loads(json.dumps(self.OBS))
        obs["stats"]["brightness"] = 0.1
        self._write_obs(obs)
        r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertIn("光线偏暗", r["interpretation"])
        self.assertIn("补光", "".join(r["suggested_actions"]))

    # 4) mode=heuristic 时即使配了 LLM 也不发网络请求
    async def test_mode_heuristic_no_llm_call(self):
        self._write_obs()
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"

        def boom(req, timeout=5):
            raise AssertionError("heuristic 模式不应发起 LLM 请求")

        with patch("urllib.request.urlopen", new=boom):
            r = _parse(await vision.analyze_observation(mode="heuristic"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")

    # 5) mode=llm 无配置 → 明确失败
    async def test_mode_llm_no_config_fails(self):
        self._write_obs()
        r = _parse(await vision.analyze_observation(mode="llm"))
        self.assertFalse(r["success"])
        self.assertIn("未配置 LLM", r["error"])

    # 6) LLM 严格 JSON 解析
    async def test_llm_json_parse(self):
        self._write_obs()
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        captured = {}
        payload = '{"interpretation": "桌面有笔记本电脑和杯子", "suggested_actions": ["靠近观察", "记录"], "confidence": "high"}'
        with patch("urllib.request.urlopen", new=self._fake_urlopen(payload, captured)):
            r = _parse(await vision.analyze_observation(mode="llm"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "llm")
        self.assertEqual(r["llm_backend"], "ollama")
        self.assertEqual(r["interpretation"], "桌面有笔记本电脑和杯子")
        # v3.18：LLM 建议动作约束到 body 词表——"记录"非词表项被过滤
        self.assertEqual(r["suggested_actions"], ["靠近观察"])
        self.assertEqual(r["confidence"], "high")
        self.assertEqual(captured["body"]["model"], "qwen2.5:3b")

    # 7) LLM 非 JSON → 整段当解释 + heuristic 动作兜底（诚实标注 method=llm）
    async def test_llm_nonjson_fallback(self):
        self._write_obs()
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        with patch("urllib.request.urlopen", new=self._fake_urlopen("画面里没有特别的东西。")):
            r = _parse(await vision.analyze_observation(mode="llm"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "llm")
        self.assertEqual(r["interpretation"], "画面里没有特别的东西。")
        self.assertTrue(r["suggested_actions"])
        self.assertEqual(r["confidence"], "low")

    # 8) auto + LLM 网络失败 → 降级 heuristic
    async def test_auto_llm_failure_falls_back(self):
        self._write_obs()
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"

        def boom(req, timeout=5):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", new=boom):
            r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")
        self.assertIn("静态场景", r["interpretation"])

    # 9) source 防穿越 / 越界绝对路径 → 失败
    async def test_source_traversal_fails(self):
        self._write_obs()
        outside = self._tmp.parent / "outside-vision.json"
        outside.write_text(json.dumps(self.OBS), encoding="utf-8")
        for bad in ("../outside-vision.json", str(outside)):
            r = _parse(await vision.analyze_observation(source=bad))
            self.assertFalse(r["success"], bad)
            self.assertIn("越界", r["error"])
        outside.unlink()

    # 10) source 不存在 → 失败
    async def test_source_missing_fails(self):
        r = _parse(await vision.analyze_observation(source="nope.json"))
        self.assertFalse(r["success"])
        self.assertIn("不存在", r["error"])

    # 11) 观察文件 schema 不匹配 / 非 JSON → 失败
    async def test_invalid_observation_fails(self):
        (self._tmp / "latest.json").write_text("not-json", encoding="utf-8")
        r = _parse(await vision.analyze_observation())
        self.assertFalse(r["success"])
        self.assertIn("非法", r["error"])
        (self._tmp / "latest.json").write_text(
            json.dumps({"schema": "other-v99", "stats": {}}), encoding="utf-8")
        r2 = _parse(await vision.analyze_observation())
        self.assertFalse(r2["success"])

    # 12) 身份闸门：REQUIRE_IDENTITY=1 无身份 → 拒绝
    async def test_identity_gate(self):
        self._write_obs()
        saved = os.environ.get("REQUIRE_IDENTITY")
        saved_ident = os.environ.get("COLLAB_IDENTITY")
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            os.environ.pop("COLLAB_IDENTITY", None)
            r = _parse(await vision.analyze_observation())
            self.assertFalse(r["success"])
            self.assertIn("需要调用者身份", r["error"])
        finally:
            if saved is None:
                os.environ.pop("REQUIRE_IDENTITY", None)
            else:
                os.environ["REQUIRE_IDENTITY"] = saved
            if saved_ident is None:
                os.environ.pop("COLLAB_IDENTITY", None)
            else:
                os.environ["COLLAB_IDENTITY"] = saved_ident

    # 13) 视觉模型：with_image + OLLAMA_VISION_MODEL → 请求含 image_url data URI
    async def test_vision_model_sends_image(self):
        import base64 as _b64
        # 造一个最小 JPEG（1x1 像素），写入 vision 目录
        img_bytes = _b64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
        img_path = self._tmp / "frame-test.jpg"
        img_path.write_bytes(img_bytes)
        obs = json.loads(json.dumps(self.OBS))
        obs["image_path"] = str(img_path.relative_to(self._tmp.parent)).replace("\\", "/")
        self._write_obs(obs)
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        os.environ["OLLAMA_VISION_MODEL"] = "llava:latest"
        captured = {}
        payload = '{"interpretation": "画面内容", "suggested_actions": ["x"], "confidence": "medium"}'
        with patch("urllib.request.urlopen", new=self._fake_urlopen(payload, captured)):
            r = _parse(await vision.analyze_observation(mode="llm", with_image=True))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["llm_backend"], "ollama-vision")
        content = captured["body"]["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("data:image/jpeg;base64,", content[1]["image_url"]["url"])
        self.assertEqual(captured["body"]["model"], "llava:latest")

    # v3.25 语义统一（PC-C v3.23 低-1）：image_path 与观察 JSON 共置（纯文件名）→ 命中
    async def test_vision_model_sends_image_co_located(self):
        import base64 as _b64
        img_bytes = _b64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
        (self._tmp / "frame-test.jpg").write_bytes(img_bytes)
        obs = json.loads(json.dumps(self.OBS))
        obs["image_path"] = "frame-test.jpg"  # 新语义：共置纯文件名
        self._write_obs(obs)
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        os.environ["OLLAMA_VISION_MODEL"] = "llava:latest"
        captured = {}
        payload = '{"interpretation": "画面内容", "suggested_actions": ["x"], "confidence": "medium"}'
        with patch("urllib.request.urlopen", new=self._fake_urlopen(payload, captured)):
            r = _parse(await vision.analyze_observation(mode="llm", with_image=True))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["llm_backend"], "ollama-vision")
        content = captured["body"]["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("data:image/jpeg;base64,", content[1]["image_url"]["url"])

    # v3.25：观察 JSON 位于 root 子目录，帧共置 → 按观察文件目录解析命中
    async def test_vision_image_subdir_co_located(self):
        import base64 as _b64
        sub = self._tmp / "sub"
        sub.mkdir()
        (sub / "frame-x.jpg").write_bytes(_b64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="))
        obs = json.loads(json.dumps(self.OBS))
        obs["image_path"] = "frame-x.jpg"
        (sub / "obs.json").write_text(json.dumps(obs, ensure_ascii=False), encoding="utf-8")
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        os.environ["OLLAMA_VISION_MODEL"] = "llava:latest"
        captured = {}
        payload = '{"interpretation": "子目录画面", "suggested_actions": ["x"], "confidence": "medium"}'
        with patch("urllib.request.urlopen", new=self._fake_urlopen(payload, captured)):
            r = _parse(await vision.analyze_observation(
                source="sub/obs.json", mode="llm", with_image=True))
        self.assertTrue(r["success"], r)
        content = captured["body"]["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("data:image/jpeg;base64,", content[1]["image_url"]["url"])

    # v3.25：image_path 指向 vision 根外 → 拒绝并降级纯文本（不透出越界图片）
    async def test_vision_image_outside_root_rejected(self):
        import base64 as _b64
        outside = Path(tempfile.mkdtemp(prefix="collab_vision_outside_"))
        (outside / "frame-evil.jpg").write_bytes(_b64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="))
        obs = json.loads(json.dumps(self.OBS))
        obs["image_path"] = str(outside / "frame-evil.jpg")
        self._write_obs(obs)
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        os.environ["OLLAMA_VISION_MODEL"] = "llava:latest"
        captured = {}
        payload = '{"interpretation": "无图降级", "suggested_actions": ["x"], "confidence": "medium"}'
        with patch("urllib.request.urlopen", new=self._fake_urlopen(payload, captured)):
            r = _parse(await vision.analyze_observation(mode="llm", with_image=True))
        self.assertTrue(r["success"], r)
        # 越界图片不得透出：content 应为纯文本（非 list），且不出现 image_url
        content = captured["body"]["messages"][1]["content"]
        self.assertNotIsInstance(content, list)
        self.assertNotIn("image_url", str(content))

    # 低-1 回归（PC-C 闸门 bc6e482b155a）：OPENAI 默认 base 不应拼出 /v1/v1/ 404
    async def test_openai_default_url_no_double_v1(self):
        self._write_obs()
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ.pop("OPENAI_BASE_URL", None)
        seen = {}

        def fake(req, timeout=5):
            seen["url"] = req.full_url
            return self._fake_urlopen(
                '{"interpretation": "桌面场景", "suggested_actions": ["记录"], "confidence": "medium"}'
            )(req, timeout)

        with patch("urllib.request.urlopen", new=fake):
            r = _parse(await vision.analyze_observation(mode="llm"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["llm_backend"], "openai")
        self.assertEqual(seen["url"], "https://api.openai.com/v1/chat/completions")
        self.assertNotIn("/v1/v1/", seen["url"])

    # 低-1 回归：OPENAI_BASE_URL 显式带 /v1 时也归一化，不拼 /v1/v1/
    async def test_openai_base_url_v1_normalized(self):
        self._write_obs()
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_BASE_URL"] = "http://myhost:8000/v1"
        seen = {}

        def fake(req, timeout=5):
            seen["url"] = req.full_url
            return self._fake_urlopen(
                '{"interpretation": "x", "suggested_actions": ["y"], "confidence": "low"}'
            )(req, timeout)

        with patch("urllib.request.urlopen", new=fake):
            r = _parse(await vision.analyze_observation(mode="llm"))
        self.assertTrue(r["success"], r)
        self.assertEqual(seen["url"], "http://myhost:8000/v1/chat/completions")

    # 14) 真实冒烟（本机有 OpenCV 时）：--image 静态图 → 采集 → analyze heuristic
    @unittest.skipUnless(importlib.util.find_spec("cv2"), "本机无 OpenCV，跳过真实采集冒烟")
    async def test_real_image_smoke(self):
        import cv2 as _cv2
        import numpy as _np
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import vision_capture as vc
        frame = _np.full((240, 320, 3), 200, dtype=_np.uint8)
        img_path = self._tmp / "fixture.jpg"
        vc._imwrite_utf8(_cv2, img_path, frame)
        rc = vc.main(["--image", str(img_path), "--out-dir", str(self._tmp)])
        self.assertEqual(rc, 0)
        obs_files = list(self._tmp.glob("observation-*.json"))
        self.assertEqual(len(obs_files), 1)
        obs = json.loads(obs_files[0].read_text(encoding="utf-8"))
        self.assertEqual(obs["stats"]["width"], 320)
        self.assertEqual(obs["stats"]["height"], 240)
        self.assertGreater(obs["stats"]["brightness"], 0.7)

        r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")
        self.assertEqual(r["source"], "image:%s" % img_path)


class TestVisionCaptureV312(unittest.TestCase):
    """v3.12.0：眼睛-大脑 Step1 —— vision_capture.py 脚本（demo hermetic + 缺 cv2 报错路径）。"""

    def _vc(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import vision_capture as vc
        return vc

    def test_demo_writes_observation_and_latest(self):
        vc = self._vc()
        with tempfile.TemporaryDirectory() as td:
            rc = vc.main(["--demo", "--out-dir", td])
            self.assertEqual(rc, 0)
            obs_files = list(Path(td).glob("observation-*.json"))
            self.assertEqual(len(obs_files), 1)
            obs = json.loads(obs_files[0].read_text(encoding="utf-8"))
            self.assertEqual(obs["schema"], "collab-vision-observation-v1")
            self.assertTrue(obs["synthetic"])
            self.assertEqual(obs["source"], "demo")
            self.assertEqual(obs["stats"]["width"], 0)
            latest = json.loads((Path(td) / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["observation_id"], obs["observation_id"])

    def test_missing_cv2_non_demo_errors(self):
        vc = self._vc()
        with patch.object(vc, "_load_cv2", return_value=None):
            rc = vc.main(["--camera", "0", "--out-dir", tempfile.gettempdir()])
        self.assertEqual(rc, 2)

    def test_demo_cli_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run(
                [sys.executable, str(REPO_DIR / "scripts" / "vision_capture.py"),
                 "--demo", "--out-dir", td, "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            obs = json.loads(p.stdout)
            self.assertEqual(obs["schema"], "collab-vision-observation-v1")
            self.assertTrue(obs["synthetic"])
            self.assertEqual(obs["source"], "demo")

    # v3.25 语义统一（PC-C v3.23 低-1）：image_path 写纯文件名（与观察 JSON 共置），
    # 不依赖 cv2（patch 分析/写帧），hermetic
    def test_image_path_relative_to_observation_dir(self):
        vc = self._vc()
        stats = {"width": 4, "height": 4, "brightness": 0.5, "motion": 0.0,
                 "dominant_color": "#000000", "sat_mean": 0.0, "val_mean": 0.0,
                 "edge_density": 0.0, "gray_var": 0.0, "dominant_colors": []}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(vc, "_analyze", return_value=(stats, [], [])), \
                patch.object(vc, "_imwrite_utf8",
                             side_effect=lambda cv2_, path, frame: path.write_bytes(b"fake")):
            obs = vc._build_observation(None, object(), "demo", None, False, Path(td))
            self.assertEqual(obs["image_path"], f"frame-{obs['observation_id']}.jpg")
            self.assertTrue((Path(td) / obs["image_path"]).is_file())


class TestVisionCaptureWatchV314(unittest.TestCase):
    """v3.14.0：Step 3 —— vision_capture.py --watch 连续观察（demo hermetic + 钳制 + 缺 cv2）。"""

    def _vc(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import vision_capture as vc
        return vc

    def test_watch_demo_rounds_writes_all(self):
        # obs_id 的 uuid 是随机的，文件名字典序 != 写入顺序；以 --json 输出顺序为准
        import contextlib
        import io
        vc = self._vc()
        buf = io.StringIO()
        with patch.object(vc.time, "sleep", return_value=None):
            with tempfile.TemporaryDirectory() as td:
                with contextlib.redirect_stdout(buf):
                    rc = vc.main(["--watch", "--demo", "--rounds", "2", "--interval", "1",
                                  "--out-dir", td, "--json"])
                self.assertEqual(rc, 0)
                obs_files = list(Path(td).glob("observation-*.json"))
                self.assertEqual(len(obs_files), 2)
                ids_written = []
                for line in buf.getvalue().splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        ids_written.append(json.loads(line)["observation_id"])
                self.assertEqual(len(ids_written), 2)
                # latest.json 必须指向最后写入的一轮，且该 observation 文件存在
                latest = json.loads((Path(td) / "latest.json").read_text(encoding="utf-8"))
                self.assertEqual(latest["observation_id"], ids_written[-1])
                last_file = Path(td) / f"observation-{ids_written[-1]}.json"
                self.assertTrue(last_file.exists())
                last_obs = json.loads(last_file.read_text(encoding="utf-8"))
                self.assertEqual(last_obs["observation_id"], ids_written[-1])
                self.assertTrue(all(o["synthetic"] for o in
                                    (json.loads(f.read_text(encoding="utf-8")) for f in obs_files)))

    def test_watch_interval_clamped(self):
        vc = self._vc()
        with tempfile.TemporaryDirectory() as td:
            # interval 99999 钳制到 3600；rounds=1 不触发 sleep，直接返回
            rc = vc.main(["--watch", "--demo", "--rounds", "1", "--interval", "99999",
                          "--out-dir", td])
        self.assertEqual(rc, 0)

    def test_watch_missing_cv2_non_demo_fails(self):
        vc = self._vc()
        with patch.object(vc, "_load_cv2", return_value=None):
            rc = vc.main(["--watch", "--camera", "0", "--rounds", "1",
                          "--out-dir", tempfile.gettempdir()])
        self.assertEqual(rc, 2)


class TestBodyContextV314(unittest.IsolatedAsyncioTestCase):
    """v3.14.0：Step 3 —— analyze_observation body_context 大脑-身体双向反馈（mock LLM，hermetic）。"""

    OBS = {
        "schema": "collab-vision-observation-v1",
        "observation_id": "obs-bc-000001",
        "timestamp": "2026-08-07T12:00:00+08:00",
        "source": "demo",
        "image_path": "",
        "synthetic": True,
        "stats": {"width": 640, "height": 480, "brightness": 0.1, "motion": 0.0,
                  "dominant_color": "#101010"},
        "detections": [],
        "capture_duration_ms": 0,
        "notes": [],
    }

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._tmp = Path(tempfile.mkdtemp(prefix="collab_bodyctx_test_"))
        os.environ["COLLAB_VISION_DIR"] = str(self._tmp)
        self._saved = {k: os.environ.get(k) for k in (
            "COLLAB_VISION_DIR", "OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_VISION_MODEL",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
        )}
        for k in self._saved:
            if k != "COLLAB_VISION_DIR":
                os.environ.pop(k, None)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_obs(self, brightness=0.1, motion=0.0):
        obs = json.loads(json.dumps(self.OBS))
        obs["stats"]["brightness"] = brightness
        obs["stats"]["motion"] = motion
        (self._tmp / "latest.json").write_text(json.dumps(obs), encoding="utf-8")

    async def test_dark_light_off_suggests_light(self):
        self._write_obs(brightness=0.1)
        r = _parse(await vision.analyze_observation(
            body_context=json.dumps({"light": False, "heading_deg": 0})))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")
        self.assertIn("光线偏暗", r["interpretation"])
        self.assertIn("补光", "".join(r["suggested_actions"]))

    async def test_dark_light_on_suggests_camera(self):
        self._write_obs(brightness=0.1)
        r = _parse(await vision.analyze_observation(
            body_context=json.dumps({"light": True, "heading_deg": 90})))
        self.assertTrue(r["success"], r)
        self.assertIn("补光已开启", r["interpretation"])
        joined = "".join(r["suggested_actions"])
        self.assertIn("摄像头", joined)
        self.assertNotIn("开启补光", joined)

    async def test_static_heading_suggests_turn(self):
        self._write_obs(brightness=0.6, motion=0.0)
        r = _parse(await vision.analyze_observation(
            body_context=json.dumps({"heading_deg": 180})))
        self.assertTrue(r["success"], r)
        self.assertIn("转向扫描", r["interpretation"])
        self.assertIn("扫描", "".join(r["suggested_actions"]))

    async def test_invalid_body_context_ignored(self):
        self._write_obs()
        r = _parse(await vision.analyze_observation(body_context="not-json{{"))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "heuristic")
        self.assertEqual(r["body_context"], "not-json{{")

    async def test_no_body_context_preserves_v312(self):
        self._write_obs(brightness=0.6, motion=0.0)
        r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertIn("静态场景", r["interpretation"])
        self.assertEqual(r["body_context"], "")

    async def test_llm_prompt_includes_body_context(self):
        self._write_obs(brightness=0.1)
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        captured = {}

        class Resp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        def fake(req, timeout=5):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return Resp(json.dumps({"choices": [{"message": {"content": json.dumps({
                "interpretation": "偏暗，需补光",
                "suggested_actions": ["开启补光"],
                "confidence": "high",
            })}}]}).encode("utf-8"))

        body_ctx = json.dumps({"light": False, "heading_deg": 0}, ensure_ascii=False)
        with patch("urllib.request.urlopen", new=fake):
            r = _parse(await vision.analyze_observation(mode="llm", body_context=body_ctx))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "llm")
        user_text = captured["body"]["messages"][1]["content"]
        self.assertIn("当前身体状态", user_text)
        self.assertIn("heading_deg", user_text)
        self.assertEqual(r["body_context"], body_ctx)

    async def test_dark_no_body_preserves_v312_text(self):
        # PC-C M2：无 body_context 暗场景动作文本与 v3.12 完全一致（回归面）
        self._write_obs(brightness=0.1)
        r = _parse(await vision.analyze_observation())
        self.assertTrue(r["success"], r)
        self.assertIn("提示调整补光或摄像头朝向", r["suggested_actions"])

    async def test_dark_light_on_action_avoids_light_substring(self):
        # PC-C M1：暗+灯已开 → 建议调摄像头，动作文本避开「补光」子串——
        # body.execute_action 子串匹配下「补光」会被 light_on 别名先命中，camera 永不转动
        self._write_obs(brightness=0.1)
        r = _parse(await vision.analyze_observation(
            body_context=json.dumps({"light": True, "heading_deg": 0})))
        self.assertTrue(r["success"], r)
        joined = "".join(r["suggested_actions"])
        self.assertIn("摄像头", joined)
        self.assertNotIn("补光", joined)

    async def test_light_string_true_handled(self):
        # PC-C L3：light 为字符串 "true" 也按开启处理（宽松判断）
        self._write_obs(brightness=0.1)
        r = _parse(await vision.analyze_observation(
            body_context=json.dumps({"light": "true"})))
        self.assertTrue(r["success"], r)
        self.assertIn("摄像头", "".join(r["suggested_actions"]))

    async def test_llm_prompt_body_context_trimmed(self):
        # PC-C L2：超长 body_context 拼 LLM 提示词时裁剪到上限，防 token 放大
        self._write_obs(brightness=0.1)
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        captured = {}

        class Resp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        def fake(req, timeout=5):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return Resp(json.dumps({"choices": [{"message": {"content": json.dumps({
                "interpretation": "偏暗",
                "suggested_actions": ["开启补光"],
                "confidence": "low",
            })}}]}).encode("utf-8"))

        long_ctx = json.dumps({"history": [{"x": "y" * 3000}]}, ensure_ascii=False)
        with patch("urllib.request.urlopen", new=fake):
            r = _parse(await vision.analyze_observation(mode="llm", body_context=long_ctx))
        self.assertTrue(r["success"], r)
        user_text = captured["body"]["messages"][1]["content"]
        self.assertIn("已截断", user_text)
        self.assertLess(len(user_text), 1200)


class TestRobotLoopV314(unittest.IsolatedAsyncioTestCase):
    """v3.14.0：Step 3 —— robot_loop.py 连续闭环（demo hermetic + 无 capture 复用 latest）。"""

    def _rl(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import robot_loop as rl
        return rl

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._vdir = Path(tempfile.mkdtemp(prefix="collab_rl_vision_"))
        self._bdir = Path(tempfile.mkdtemp(prefix="collab_rl_body_"))
        self._saved = {}
        for k in ("COLLAB_VISION_DIR", "COLLAB_BODY_DIR"):
            self._saved[k] = os.environ.get(k)
        os.environ["COLLAB_VISION_DIR"] = str(self._vdir)
        os.environ["COLLAB_BODY_DIR"] = str(self._bdir)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def test_demo_loop_two_rounds(self):
        rl = self._rl()
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            summary = await rl.run_loop(rounds=2, interval=1, capture=True,
                                        demo=True, verbose=False)
        self.assertEqual(summary["rounds_run"], 2)
        self.assertGreaterEqual(summary["actions_executed"], 1)
        self.assertTrue((self._bdir / "state.json").exists(),
                        "身体状态文件应被 execute_action 创建")
        self.assertTrue(list(self._vdir.glob("observation-*.json")),
                        "capture demo 应刷新观察文件")

    async def test_loop_uses_existing_latest_without_capture(self):
        rl = self._rl()
        obs = {
            "schema": "collab-vision-observation-v1",
            "observation_id": "obs-rl-000001",
            "timestamp": "2026-08-07T12:00:00+08:00",
            "source": "demo",
            "image_path": "",
            "synthetic": True,
            "stats": {"width": 640, "height": 480, "brightness": 0.05, "motion": 0.0,
                      "dominant_color": "#101010"},
            "detections": [],
            "capture_duration_ms": 0,
            "notes": [],
        }
        (self._vdir / "latest.json").write_text(json.dumps(obs), encoding="utf-8")
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            summary = await rl.run_loop(rounds=1, interval=1, capture=False, verbose=False)
        self.assertEqual(summary["rounds_run"], 1)
        self.assertEqual(summary["actions_executed"], 1)
        self.assertTrue(summary["rounds"][0]["action_ok"])
        # 双向反馈：执行后身体状态文件生成，下一轮可被大脑感知
        state = json.loads((self._bdir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["light"], "暗场景+灯未开 → 大脑应建议开灯")

    async def test_closed_loop_light_on_then_camera(self):
        # PC-C M1 集成：预置身体 light=true + 暗场景 → 闭环第 1 轮应执行 camera
        # （而非被「补光」子串误归一化为 light_on 重复开灯）；camera 必须真的转动
        rl = self._rl()
        obs = {
            "schema": "collab-vision-observation-v1",
            "observation_id": "obs-rl-cam-000001",
            "timestamp": "2026-08-07T12:00:00+08:00",
            "source": "demo",
            "image_path": "",
            "synthetic": True,
            "stats": {"width": 640, "height": 480, "brightness": 0.05, "motion": 0.0,
                      "dominant_color": "#101010"},
            "detections": [],
            "capture_duration_ms": 0,
            "notes": [],
        }
        (self._vdir / "latest.json").write_text(json.dumps(obs), encoding="utf-8")
        (self._bdir / "state.json").write_text(json.dumps({
            "schema": "collab-body-state-v1",
            "robot_id": "sim-1",
            "position": {"x": 0, "y": 0},
            "heading_deg": 0,
            "status": "idle",
            "light": True,
            "camera": {"pan_deg": 0, "tilt_deg": 0},
            "last_action": None,
            "last_action_at": None,
            "history": [],
        }), encoding="utf-8")
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            summary = await rl.run_loop(rounds=1, interval=1, capture=False, verbose=False)
        self.assertEqual(summary["rounds_run"], 1)
        self.assertTrue(summary["rounds"][0]["action_ok"], summary["rounds"][0])
        state = json.loads((self._bdir / "state.json").read_text(encoding="utf-8"))
        self.assertNotEqual(state["camera"]["pan_deg"], 0,
                            "暗+灯已开 → 大脑应建议调摄像头且身体执行 camera")
        self.assertTrue(state["light"], "不应误关灯")


class TestPerceptionEnhanceV315(unittest.TestCase):
    """v3.15.0：感知增强 —— vision_capture stats 扩展（HSV 饱和度/亮度、边缘密度、灰度方差、top-3 颜色桶）。"""

    def _vc(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import vision_capture as vc
        return vc

    def test_enhanced_stats_on_real_image(self):
        try:
            import cv2 as _cv2
            import numpy as _np
        except ImportError:
            self.skipTest("需 OpenCV（uv311/无 cv2 环境跳过，CI ubuntu 覆盖）")
        # 黑白棋盘（提供强边缘，Canny 高阈值 200 可检）+ 中央亮红块（提供高饱和度）
        frame = _np.zeros((240, 320, 3), dtype=_np.uint8)
        for y in range(0, 240, 30):
            for x in range(0, 320, 40):
                if (x // 40 + y // 30) % 2 == 0:
                    frame[y:y + 30, x:x + 40] = 255
        frame[90:150, 120:200] = (0, 0, 255)  # BGR 红色块
        vc = self._vc()
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "fixture.jpg"
            vc._imwrite_utf8(_cv2, img, frame)
            rc = vc.main(["--image", str(img), "--out-dir", td])
            self.assertEqual(rc, 0)
            obs_files = list(Path(td).glob("observation-*.json"))
            self.assertEqual(len(obs_files), 1)
            stats = json.loads(obs_files[0].read_text(encoding="utf-8"))["stats"]
        for key in ("sat_mean", "val_mean", "edge_density", "gray_var", "dominant_colors"):
            self.assertIn(key, stats, f"stats 缺少感知增强字段 {key}")
        self.assertGreater(stats["sat_mean"], 10, "含高饱和色块时 sat_mean 应明显 > 0")
        self.assertGreater(stats["edge_density"], 0.0, "色块边界应产生边缘")
        self.assertGreater(stats["gray_var"], 0.0, "灰底+色块应有灰度方差")
        colors = stats["dominant_colors"]
        self.assertGreaterEqual(len(colors), 2, "应有至少 2 个颜色桶")
        self.assertLessEqual(sum(c["share"] for c in colors), 1.001)
        self.assertTrue(any(c["color"].upper() != "#606060" for c in colors))

    def test_demo_defaults(self):
        vc = self._vc()
        with tempfile.TemporaryDirectory() as td:
            rc = vc.main(["--demo", "--out-dir", td])
            self.assertEqual(rc, 0)
            obs = json.loads((Path(td) / "latest.json").read_text(encoding="utf-8"))
        stats = obs["stats"]
        self.assertEqual(stats["sat_mean"], 0.0)
        self.assertEqual(stats["val_mean"], 0.0)
        self.assertEqual(stats["edge_density"], 0.0)
        self.assertEqual(stats["gray_var"], 0.0)
        self.assertEqual(stats["dominant_colors"], [])

    def test_heuristic_consumes_sat_for_color_target(self):
        # v3.15/v3.19：heuristic 识别彩色目标——集中高饱和色块 + 边缘信号联合判定
        from collab_mcp import vision
        obs = {
            "schema": "collab-vision-observation-v1", "observation_id": "t",
            "source": "x", "stats": {"brightness": 0.28, "motion": 0.0,
                                     "sat_mean": 62.9, "val_mean": 83.8,
                                     "edge_density": 0.054, "gray_var": 912.3,
                                     "dominant_colors": [{"color": "#e0e060", "share": 0.10}]},
            "detections": [], "notes": [],
        }
        interp, actions, conf = vision._heuristic_interpret(obs)
        self.assertIn("彩色目标", interp)
        self.assertTrue(any("观察" in a for a in actions), actions)

    def test_heuristic_diffuse_sat_not_target(self):
        # v3.19：开灯整体提饱和（sat 高但边缘低）→ 不误报「彩色目标」，只报「饱和度较高」
        from collab_mcp import vision
        obs = {
            "schema": "collab-vision-observation-v1", "observation_id": "t",
            "source": "x", "stats": {"brightness": 0.77, "motion": 0.0,
                                     "sat_mean": 101.2, "val_mean": 211.8,
                                     "edge_density": 0.045, "gray_var": 1951.1,
                                     "dominant_colors": [{"color": "#e0e020", "share": 0.18}]},
            "detections": [], "notes": [],
        }
        interp, actions, _ = vision._heuristic_interpret(obs)
        self.assertNotIn("彩色目标", interp)
        self.assertIn("饱和度较高", interp)

    def test_heuristic_sat_threshold_not_triggered(self):
        from collab_mcp import vision
        obs = {
            "schema": "collab-vision-observation-v1", "observation_id": "t",
            "source": "x", "stats": {"brightness": 0.32, "motion": 0.0,
                                     "sat_mean": 45.0, "val_mean": 90.0,
                                     "edge_density": 0.01, "gray_var": 800},
            "detections": [], "notes": [],
        }
        interp, actions, _ = vision._heuristic_interpret(obs)
        self.assertNotIn("彩色目标", interp)
        self.assertNotIn("高饱和度", interp)

    def test_imread_imwrite_non_ascii_path(self):
        # v3.15.1：cv2 imread/imwrite 在 Windows 非 ASCII 路径失败（PC-C 中危）→ imdecode/imencode 兜底
        try:
            import cv2 as _cv2
            import numpy as _np
        except ImportError:
            self.skipTest("需 OpenCV（uv311/无 cv2 环境跳过）")
        vc = self._vc()
        frame = _np.full((60, 80, 3), 90, dtype=_np.uint8)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "中文路径"
            base.mkdir()
            img = base / "帧测试.jpg"
            self.assertTrue(vc._imwrite_utf8(_cv2, img, frame), "非 ASCII 路径 imwrite 应成功")
            self.assertTrue(img.exists(), "文件应已落盘")
            back = vc._imread_utf8(_cv2, img)
            self.assertIsNotNone(back, "非 ASCII 路径 imread 应能解码")
            self.assertEqual(back.shape, frame.shape, "解码帧形状应一致")

    def test_heuristic_edge_and_contrast(self):
        from collab_mcp import vision
        obs = {
            "schema": "collab-vision-observation-v1", "observation_id": "t",
            "source": "x", "stats": {"brightness": 0.5, "motion": 0.0,
                                     "sat_mean": 40.0, "val_mean": 130.0,
                                     "edge_density": 0.08, "gray_var": 4200.0},
            "detections": [], "notes": [],
        }
        interp, actions, _ = vision._heuristic_interpret(obs)
        self.assertIn("细节", interp)
        self.assertIn("对比度", interp)
        self.assertTrue(any("观察" in a for a in actions), actions)

    def test_heuristic_still_works_with_enhanced_stats(self):
        # 增强字段不破坏大脑 heuristic（回归面）
        import json as _json
        vc = self._vc()
        with tempfile.TemporaryDirectory() as td:
            rc = vc.main(["--demo", "--out-dir", td])
            self.assertEqual(rc, 0)
            obs = _json.loads((Path(td) / "latest.json").read_text(encoding="utf-8"))
        # 直接调 heuristic（隔离 vision dir 由 env 控制——此处仅验证不抛异常）
        from collab_mcp import vision
        saved = os.environ.get("COLLAB_VISION_DIR")
        os.environ["COLLAB_VISION_DIR"] = td
        try:
            interp, actions, conf = vision._heuristic_interpret(obs)
            self.assertIsInstance(interp, str)
            self.assertTrue(actions)
            self.assertIn(conf, ("high", "medium", "low"))
        finally:
            if saved is None:
                os.environ.pop("COLLAB_VISION_DIR", None)
            else:
                os.environ["COLLAB_VISION_DIR"] = saved


if __name__ == "__main__":
    unittest.main()


class TestAnyDocIntakeV316(unittest.TestCase):
    """v3.16.0：anydoc 摄入升级 —— office 全家桶格式检测 + 可选转换 + 明确降级报错。"""

    def _add(self, src, target="anydoc_test"):
        from collab_mcp import documents
        return json.loads(asyncio.run(documents.add_document(str(src), target_dir=target)))

    def test_office_ext_detection(self):
        from collab_mcp.documents import _detect_format
        for name, want in {
            "a.pptx": "pptx", "a.xlsx": "xlsx", "a.xls": "xls", "a.doc": "doc",
            "a.ppt": "ppt", "a.odt": "odt", "a.ods": "ods", "a.odp": "odp",
            "a.csv": "csv", "a.docx": "docx", "a.pdf": "pdf",
        }.items():
            p = Path(name)
            self.assertEqual(_detect_format(p, p.suffix.lower(), ""), want, name)

    def test_new_format_without_anydoc_fails_clearly(self):
        from collab_mcp import documents
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "t.xlsx"
            src.write_bytes(b"not really xlsx")
            saved = documents._ANYDOC_AVAILABLE
            documents._ANYDOC_AVAILABLE = False
            try:
                r = self._add(src)
            finally:
                documents._ANYDOC_AVAILABLE = saved
        self.assertFalse(r.get("success"), r)
        self.assertIn("firecrawl-anydoc", json.dumps(r, ensure_ascii=False))

    def test_anydoc_empty_output_raises_for_office(self):
        # PC-C 闸门 LOW1：anydoc 可用但返回空文本（非异常）→ office 格式显式报错，
        # 不回退 passthrough 把二进制当文本读
        from collab_mcp import documents
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "t.pptx"
            src.write_bytes(b"fake pptx")
            saved = documents._ANYDOC_AVAILABLE
            documents._ANYDOC_AVAILABLE = True
            try:
                with patch("collab_mcp.documents._extract_anydoc", return_value=""):
                    r = self._add(src)
            finally:
                documents._ANYDOC_AVAILABLE = saved
        self.assertFalse(r.get("success"), r)
        self.assertIn("anydoc", json.dumps(r, ensure_ascii=False))

    @unittest.skipUnless(
        importlib.util.find_spec("anydoc")
        and importlib.util.find_spec("openpyxl")
        and importlib.util.find_spec("docx")
        and importlib.util.find_spec("pptx"),
        "需 firecrawl-anydoc + openpyxl + python-docx + python-pptx",
    )
    def test_real_office_conversion_uses_anydoc(self):
        import openpyxl
        import docx as _docx
        import pptx as _pptx
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            xlsx = td / "table.xlsx"
            wb = openpyxl.Workbook(); ws = wb.active
            ws["A1"] = "名称"; ws["B1"] = "数量"; ws["A2"] = "苹果"; ws["B2"] = 3
            wb.save(xlsx)
            docx_p = td / "note.docx"
            d = _docx.Document(); d.add_heading("测试文档", 0); d.add_paragraph("这是正文。"); d.save(docx_p)
            pptx_p = td / "slides.pptx"
            prs = _pptx.Presentation(); sl = prs.slides.add_slide(prs.slide_layouts[1])
            sl.shapes.title.text = "演示标题"; sl.placeholders[1].text = "要点内容"
            prs.save(pptx_p)
            csv_p = td / "data.csv"
            csv_p.write_text("名称,数量\n苹果,3\n", encoding="utf-8")
            for src, fmt in ((xlsx, "xlsx"), (docx_p, "docx"), (pptx_p, "pptx"), (csv_p, "csv")):
                r = self._add(src)
                self.assertTrue(r.get("success"), r)
                self.assertEqual(r["format"], fmt)
                self.assertEqual(r["method"], "anydoc")
                self.assertGreater(r["chars"], 0)


class TestFirecrawlAgentV317(unittest.IsolatedAsyncioTestCase):
    """v3.17.0：web_research/web_agent 的 Firecrawl v2 /agent 可选后端（hermetic，mock HTTP）。"""

    async def asyncSetUp(self):
        self._saved = {
            "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
            "WEB_AGENT_PROVIDER": os.environ.get("WEB_AGENT_PROVIDER"),
            "WIGOLO_REST_URL": os.environ.get("WIGOLO_REST_URL"),
        }
        for k in ("FIRECRAWL_API_KEY", "WEB_AGENT_PROVIDER", "WIGOLO_REST_URL"):
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def _research(self, **kw):
        return _parse(await websearch.web_research(**kw))

    async def _agent(self, **kw):
        return _parse(await websearch.web_agent(**kw))

    async def test_forced_firecrawl_without_key_fails(self):
        os.environ["WEB_AGENT_PROVIDER"] = "firecrawl"
        r = await self._research(question="测试问题")
        self.assertFalse(r["success"], r)
        self.assertIn("FIRECRAWL_API_KEY", r["error"])

    async def test_forced_firecrawl_research(self):
        os.environ["WEB_AGENT_PROVIDER"] = "firecrawl"
        os.environ["FIRECRAWL_API_KEY"] = "fc-test"
        with patch("collab_mcp.websearch._firecrawl_agent", new=AsyncMock(return_value={
            "result": "研究结论：vllm 是主流推理框架。",
            "sources": [
                {"url": "https://example.com/a", "title": "A"},
                {"url": "http://192.168.1.5/x", "title": "内网"},
                "https://example.com/b",
            ],
        })):
            r = await self._research(question="vllm 是什么")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "firecrawl")
        self.assertEqual(r["mode"], "research")
        self.assertIn("vllm", r["report"])
        self.assertEqual(r["ssrf_removed"], 1)  # 内网 source 被剔除
        self.assertEqual(r["sources_count"], 3)
        self.assertEqual(len(r["citations"]), 2)

    async def test_forced_firecrawl_web_agent(self):
        os.environ["WEB_AGENT_PROVIDER"] = "firecrawl"
        os.environ["FIRECRAWL_API_KEY"] = "fc-test"
        with patch("collab_mcp.websearch._firecrawl_agent", new=AsyncMock(return_value={
            "result": "收集结果：Notion 定价。",
            "sources": [{"url": "https://www.notion.so/pricing"}],
        })):
            r = await self._agent(prompt="查 Notion 定价", urls="https://www.notion.so/pricing")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "firecrawl")
        self.assertEqual(r["mode"], "agent")
        self.assertEqual(r["sources_count"], 1)

    async def test_auto_defaults_to_wigolo(self):
        # 默认 auto（未设 WEB_AGENT_PROVIDER）→ 保持 wigolo 行为不变
        with patch("collab_mcp.websearch._wigolo_available", new=AsyncMock(return_value=True)), \
             patch("collab_mcp.websearch._wigolo_research", new=AsyncMock(return_value={
                 "report": "简报", "citations": [], "sources": [],
             })):
            r = await self._research(question="x")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["provider"], "wigolo")

    async def test_sanitize_sources(self):
        from collab_mcp.websearch import _firecrawl_sanitize_sources
        clean, removed = _firecrawl_sanitize_sources([
            {"url": "https://example.com/x"}, {"url": "http://10.0.0.1/y"}, "https://8.8.8.8/b",
            None, {"title": "无url"}, 123,
        ])
        self.assertEqual(removed, 4)  # 内网 + None + 无url dict + 非str
        self.assertEqual(len(clean), 2)


class TestVisionLLMVocabV318(unittest.TestCase):
    """v3.18.0：LLM 建议动作约束到 body 词表（可执行性；编造动作丢弃，全丢回退 heuristic）。"""

    def _parse(self, content, obs=None):
        from collab_mcp import vision
        return vision._parse_llm_response(content, obs or {"stats": {"brightness": 0.5}}, "")

    def test_actions_constrained_to_vocab(self):
        interp, actions, conf = self._parse(
            '{"interpretation": "场景", "suggested_actions": ["前进", "打开舱门", "turn right"], "confidence": "high"}'
        )
        self.assertEqual(interp, "场景")
        self.assertEqual(actions, ["前进", "turn right"])  # "打开舱门" 非词表项被过滤
        self.assertEqual(conf, "high")

    def test_all_invalid_falls_back_to_heuristic(self):
        from collab_mcp import body, vision
        interp, actions, conf = self._parse(
            '{"interpretation": "场景", "suggested_actions": ["打开舱门", "发射导弹"], "confidence": "low"}'
        )
        self.assertEqual(interp, "场景")
        self.assertGreaterEqual(len(actions), 1)
        for a in actions:
            self.assertNotEqual(body._normalize_action(a), "", f"回退动作应可执行: {a}")

    def test_valid_kept_and_deduped(self):
        interp, actions, conf = self._parse(
            '{"interpretation": "场景", "suggested_actions": ["前进", "move forward", "观察"], "confidence": "medium"}'
        )
        self.assertEqual(actions, ["前进", "观察"])  # "move forward" 与 "前进" 同归一化 → 去重


class TestSearchHardeningV319(unittest.TestCase):
    """v3.19.0：检索加固 —— sqlite 锁重试 + 失效 scope 索引表清理。"""

    def test_retry_locked_recovers(self):
        from collab_mcp.semantic import _retry_locked
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("database is locked")
            return "ok"

        self.assertEqual(_retry_locked(fn, tries=3, delay=0), "ok")
        self.assertEqual(calls["n"], 3)

    def test_retry_locked_non_lock_error_propagates(self):
        from collab_mcp.semantic import _retry_locked

        def bad():
            raise RuntimeError("other failure")

        with self.assertRaises(RuntimeError):
            _retry_locked(bad, tries=3, delay=0)

    def test_cleanup_orphan_scopes(self):
        from collab_mcp import semantic
        m = semantic._sqlite_module()
        if m is None:
            self.skipTest("无 sqlite3")
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = m.connect(str(db))
            semantic._ensure_registry(conn)
            alive = Path(td) / "alive"
            alive.mkdir()
            conn.execute(
                f"INSERT OR REPLACE INTO {semantic._META_REGISTRY} VALUES (?, ?, ?)",
                ("k_dead", "chunks_dead", str(Path(td) / "gone")),
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {semantic._META_REGISTRY} VALUES (?, ?, ?)",
                ("k_alive", "chunks_alive", str(alive)),
            )
            conn.commit()
            removed = semantic._cleanup_orphan_scopes(conn)
            self.assertEqual(removed, 1)
            left = conn.execute(
                f"SELECT scope_key FROM {semantic._META_REGISTRY}"
            ).fetchall()
            self.assertEqual([r[0] for r in left], ["k_alive"])
            conn.close()


class TestBodyV313(unittest.IsolatedAsyncioTestCase):
    """v3.13.0：身体执行模拟器 execute_action（hermetic，COLLAB_BODY_DIR 隔离）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._tmp = Path(tempfile.mkdtemp(prefix="collab_body_test_"))
        os.environ["COLLAB_BODY_DIR"] = str(self._tmp)
        self._saved = {k: os.environ.get(k) for k in
                       ("COLLAB_BODY_DIR", "REQUIRE_IDENTITY", "COLLAB_IDENTITY")}

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def _act(self, **kw):
        return _parse(await body.execute_action(**kw))

    # 1) 前进沿 heading 0° → +y
    async def test_forward_along_heading(self):
        r = await self._act(action="前进")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["normalized"], "forward")
        self.assertEqual(r["state"]["position"], {"x": 0, "y": 1})
        self.assertEqual(r["state"]["status"], "moving")

    # 2) 右转后再前进 → 沿 90° 走 +x
    async def test_turn_right_then_forward(self):
        r1 = await self._act(action="右转")
        self.assertTrue(r1["success"], r1)
        self.assertEqual(r1["state"]["heading_deg"], 90)
        r2 = await self._act(action="前进")
        self.assertEqual(r2["state"]["position"], {"x": 1, "y": 0})

    # 3) 左转使 heading 递减（0-90 → 270）
    async def test_turn_left_decreases_heading(self):
        r = await self._act(action="左转")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["state"]["heading_deg"], 270)

    # 4) 转 180° 后前进 → -y
    async def test_heading_180_backward_direction(self):
        await self._act(action="right", amount=2)  # heading 180
        r = await self._act(action="forward")
        self.assertEqual(r["state"]["position"], {"x": 0, "y": -1})

    # 5) 后退 → 反向移动
    async def test_backward_reverses(self):
        r = await self._act(action="后退")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["normalized"], "backward")
        self.assertEqual(r["state"]["position"], {"x": 0, "y": -1})

    # 6) 停止 → idle
    async def test_stop(self):
        await self._act(action="前进")
        r = await self._act(action="停止")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["state"]["status"], "idle")

    # 7) 补光 → light=true
    async def test_light_on(self):
        r = await self._act(action="补光")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["normalized"], "light_on")
        self.assertTrue(r["state"]["light"])

    # 8) 摄像头 detail 解析 pan/tilt
    async def test_camera_detail(self):
        r = await self._act(action="调整摄像头角度", detail="pan=30 tilt=-20")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["normalized"], "camera")
        self.assertEqual(r["state"]["camera"], {"pan_deg": 30, "tilt_deg": -20})

    # 9) 未知动作 → 明确失败 + 可用列表
    async def test_unknown_action_fails(self):
        r = await self._act(action="跳舞")
        self.assertFalse(r["success"])
        self.assertIn("可用动作", r["error"])

    # 10) 空 action → 失败
    async def test_empty_action_fails(self):
        r = await self._act(action="   ")
        self.assertFalse(r["success"])
        self.assertIn("非空 action", r["error"])

    # 11) 身份闸门
    async def test_identity_gate(self):
        os.environ["REQUIRE_IDENTITY"] = "1"
        os.environ.pop("COLLAB_IDENTITY", None)
        r = await self._act(action="前进")
        self.assertFalse(r["success"])
        self.assertIn("需要调用者身份", r["error"])

    # 12) 状态跨调用持久化 + 历史增长
    async def test_state_persists_and_history_grows(self):
        await self._act(action="前进")
        r2 = await self._act(action="前进")
        self.assertEqual(r2["state"]["position"], {"x": 0, "y": 2})
        self.assertEqual(r2["history_count"], 2)
        self.assertEqual(r2["state"]["last_action"], "forward")

    # 13) 历史上限 20
    async def test_history_capped(self):
        for _ in range(25):
            await self._act(action="前进")
        r = await self._act(action="停止")
        self.assertEqual(r["history_count"], 20)
        self.assertEqual(len(r["state"]["history"]), 20)

    # 14) amount 钳制 [1,10] + 非法回默认
    async def test_amount_clamp(self):
        r = await self._act(action="前进", amount=999)
        self.assertEqual(r["state"]["position"], {"x": 0, "y": 10})
        r2 = await self._act(action="前进", amount="abc")
        self.assertEqual(r2["state"]["position"], {"x": 0, "y": 11})

    # 15) 自然语言建议（LLM suggested_actions 样式）→ 归一化
    async def test_natural_language_suggestion(self):
        r = await self._act(action="调整摄像头角度以获取更多光线进入镜头")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["normalized"], "camera")

    # 16) 损坏状态文件 → 明确失败（不静默覆盖）
    async def test_corrupt_state_fails(self):
        (self._tmp / "state.json").write_text("not-json", encoding="utf-8")
        r = await self._act(action="前进")
        self.assertFalse(r["success"])
        self.assertIn("损坏", r["error"])

    # 17) 观察动作返回状态摘要
    async def test_observe_summary(self):
        r = await self._act(action="观察")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["normalized"], "observe")
        self.assertIn("位置", r["summary"])

    # 18) 眼睛→大脑→身体 联动：heuristic 建议动作 → execute_action 成功
    async def test_brain_action_loop(self):
        vdir = Path(tempfile.mkdtemp(prefix="collab_vision_loop_"))
        obs = {
            "schema": "collab-vision-observation-v1",
            "observation_id": "obs-loop-000001",
            "timestamp": "2026-08-07T10:00:00+08:00",
            "source": "demo",
            "image_path": "",
            "synthetic": True,
            "stats": {"width": 640, "height": 480, "brightness": 0.05, "motion": 0.0,
                      "dominant_color": "#101010"},
            "detections": [],
            "capture_duration_ms": 0,
            "notes": [],
        }
        (vdir / "latest.json").write_text(json.dumps(obs, ensure_ascii=False), encoding="utf-8")
        saved_vision = os.environ.get("COLLAB_VISION_DIR")
        os.environ["COLLAB_VISION_DIR"] = str(vdir)
        try:
            vr = _parse(await vision.analyze_observation())
            self.assertTrue(vr["success"], vr)
            self.assertTrue(vr["suggested_actions"])
            suggestion = vr["suggested_actions"][0]
        finally:
            if saved_vision is None:
                os.environ.pop("COLLAB_VISION_DIR", None)
            else:
                os.environ["COLLAB_VISION_DIR"] = saved_vision
        # 身体执行大脑建议（闭环演示）
        br = await self._act(action=suggestion)
        self.assertTrue(br["success"], br)
        self.assertIn(br["normalized"],
                      ("light_on", "camera", "observe", "forward", "backward",
                       "turn_left", "turn_right", "stop"))

    # 19) PC-C LOW-2 吸收：取反语义「关闭补光/关灯」→ light_off（不反向开灯）
    async def test_light_off_negation(self):
        r1 = await self._act(action="关闭补光")
        self.assertTrue(r1["success"], r1)
        self.assertEqual(r1["normalized"], "light_off")
        self.assertFalse(r1["state"]["light"])
        r2 = await self._act(action="关灯")
        self.assertTrue(r2["success"], r2)
        self.assertEqual(r2["normalized"], "light_off")
        self.assertFalse(r2["state"]["light"])

    # 20) PC-C LOW-1 吸收：detail 溢出值不击穿崩溃（跳过 + amount 兜底）
    async def test_camera_detail_overflow_no_crash(self):
        r = await self._act(action="调整摄像头角度", detail="pan=1e999 tilt=inf")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["state"]["camera"]["pan_deg"], 1)  # amount 兜底
        self.assertEqual(r["state"]["camera"]["tilt_deg"], 0)

    # 21) PC-C LOW-4 吸收：schema 正确但深字段缺失/类型错 → 干净失败
    async def test_deep_invalid_state_fails(self):
        bad1 = {"schema": "collab-body-state-v1", "position": {"x": 1}, "heading_deg": 0,
                "camera": {"pan_deg": 0, "tilt_deg": 0}, "light": False}
        (self._tmp / "state.json").write_text(json.dumps(bad1), encoding="utf-8")
        r1 = await self._act(action="前进")
        self.assertFalse(r1["success"])
        self.assertIn("position", r1["error"])
        bad2 = {"schema": "collab-body-state-v1", "position": {"x": 0, "y": 0},
                "heading_deg": "zero", "camera": {"pan_deg": 0, "tilt_deg": 0}, "light": False}
        (self._tmp / "state.json").write_text(json.dumps(bad2), encoding="utf-8")
        r2 = await self._act(action="前进")
        self.assertFalse(r2["success"])
        self.assertIn("heading_deg", r2["error"])


class TestExplainWorkV322(unittest.IsolatedAsyncioTestCase):
    """v3.22.0：AI 老师 explain_work（hermetic：无真实 ollama/git 依赖）。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._tmp = Path(tempfile.mkdtemp(prefix="collab_ai_teacher_test_"))
        self._old_base = os.environ.get("OLLAMA_BASE_URL")
        # 指向必不可达端口：验证 LLM 失败自动降级提取式（hermetic，零真实网络）
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        def _restore():
            if self._old_base is not None:
                os.environ["OLLAMA_BASE_URL"] = self._old_base
            else:
                os.environ.pop("OLLAMA_BASE_URL", None)

        self.addCleanup(_restore)

    async def test_explain_work_registered(self):
        tools = await server.list_tools()
        self.assertIn("explain_work", [t.name for t in tools])

    async def test_explain_path_four_sections_and_file(self):
        src = self._tmp / "sample.txt"
        src.write_text(
            "第一课样本：完成了检索质量优化。\n验证：458 个测试全绿。\n"
            "影响：中文查询更快。\n",
            encoding="utf-8",
        )
        out = self._tmp / "lesson.md"
        r = _parse(
            await ai_teacher.explain_work(
                path=str(src),
                title="样本",
                out_path=str(out),
                use_llm=False,
            )
        )
        self.assertTrue(r["success"], r)
        self.assertTrue(out.exists())
        text = out.read_text(encoding="utf-8")
        for sec in (
            "## 一、一句话总结",
            "## 二、分块人话讲解",
            "## 三、证据引用",
            "## 四、你应该亲自检查的点",
        ):
            self.assertIn(sec, text)
        self.assertIn(str(src), text)
        self.assertEqual(r["method"], "extractive")
        self.assertTrue(r["read_only"])

    async def test_explain_llm_failure_falls_back_extractive(self):
        src = self._tmp / "sample2.txt"
        src.write_text(
            "后台日志：插件市场检查完成。\n设备同步握手失败（404）。\n",
            encoding="utf-8",
        )
        out = self._tmp / "lesson2.md"
        r = _parse(
            await ai_teacher.explain_work(
                path=str(src),
                out_path=str(out),
                use_llm=True,
                llm_timeout=2,
            )
        )
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "extractive")
        text = out.read_text(encoding="utf-8")
        self.assertIn("提取式", text)
        self.assertIn("异常", text)

    async def test_explain_dir_without_previews_does_not_guess(self):
        d = self._tmp / "empty_dir"
        (d / "sub").mkdir(parents=True)
        out = self._tmp / "lesson3.md"
        r = _parse(
            await ai_teacher.explain_work(
                path=str(d),
                out_path=str(out),
                use_llm=True,
                llm_timeout=2,
            )
        )
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "extractive")
        text = out.read_text(encoding="utf-8")
        self.assertIn("这里没证据", text)

    async def test_explain_requires_exactly_one_input(self):
        r = _parse(await ai_teacher.explain_work())
        self.assertFalse(r["success"])
        self.assertIn("需要且仅需要", r["error"])
        r2 = _parse(
            await ai_teacher.explain_work(
                milestone="v1.0.0",
                path=str(self._tmp),
            )
        )
        self.assertFalse(r2["success"])


class TestRobotLoopPolishV323(unittest.IsolatedAsyncioTestCase):
    """v3.23.0：robot_loop 打磨——stats 聚合 / capture_ok / elapsed_ms / stream-json / 预检。"""

    def _rl(self):
        import sys as _sys
        _sys.path.insert(0, str(REPO_DIR / "scripts"))
        import robot_loop as rl
        return rl

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._vdir = Path(tempfile.mkdtemp(prefix="collab_rl23_vision_"))
        self._bdir = Path(tempfile.mkdtemp(prefix="collab_rl23_body_"))
        self._saved = {}
        for k in ("COLLAB_VISION_DIR", "COLLAB_BODY_DIR"):
            self._saved[k] = os.environ.get(k)
        os.environ["COLLAB_VISION_DIR"] = str(self._vdir)
        os.environ["COLLAB_BODY_DIR"] = str(self._bdir)

    async def asyncTearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def test_stats_aggregation_and_round_fields(self):
        rl = self._rl()
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            summary = await rl.run_loop(
                rounds=2, interval=1, capture=True, demo=True, verbose=False
            )
        self.assertEqual(summary["rounds_run"], 2)
        stats = summary["stats"]
        self.assertEqual(stats["analysis_ok"], 2)
        self.assertEqual(stats["actions_executed"], summary["actions_executed"])
        self.assertGreaterEqual(stats["actions_executed"], 1)
        self.assertGreaterEqual(stats["total_ms"], 0)
        # 动作聚合：按名计数之和 == 总执行数
        self.assertEqual(sum(stats["actions_by_name"].values()), stats["actions_executed"])
        for r in summary["rounds"]:
            self.assertIs(r["capture_ok"], True)
            self.assertIsInstance(r["elapsed_ms"], int)
            self.assertGreaterEqual(r["elapsed_ms"], 0)

    async def test_capture_failure_recorded_and_loop_continues(self):
        rl = self._rl()
        with patch.object(rl, "_refresh_observation", return_value=False):
            with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
                summary = await rl.run_loop(
                    rounds=2, interval=1, capture=True, demo=True, verbose=False
                )
        self.assertEqual(summary["rounds_run"], 2)
        self.assertEqual(summary["stats"]["capture_failures"], 2)
        for r in summary["rounds"]:
            self.assertIs(r["capture_ok"], False)

    def test_stream_json_one_line_per_round_plus_summary(self):
        # 同步测试：main() 内部用 asyncio.run，不能在异步用例里调用
        import io
        from contextlib import redirect_stdout

        rl = self._rl()
        buf = io.StringIO()
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            with redirect_stdout(buf):
                rc = rl.main(
                    [
                        "--rounds", "2", "--interval", "1", "--demo",
                        "--stream-json",
                        "--vision-dir", str(self._vdir),
                        "--body-dir", str(self._bdir),
                    ]
                )
        self.assertEqual(rc, 0)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)  # 2 轮 + 1 汇总
        parsed = [json.loads(ln) for ln in lines]
        self.assertEqual(parsed[0]["round"], 1)
        self.assertEqual(parsed[-1]["rounds_run"], 2)
        self.assertIn("stats", parsed[-1])

    async def test_precheck_warns_without_latest_and_no_capture(self):
        import io
        from contextlib import redirect_stderr

        rl = self._rl()
        err = io.StringIO()
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            with redirect_stderr(err):
                summary = await rl.run_loop(
                    rounds=1, interval=1, capture=False, verbose=False
                )
        self.assertFalse(summary["observation_ready_before_start"])
        self.assertFalse(summary["rounds"][0]["analysis_ok"])
        self.assertIn("预检", err.getvalue())
        self.assertIn("latest.json", err.getvalue())

    async def test_vision_dir_propagates_to_analysis(self):
        # 回归：--vision-dir 必须同时作用于采集与分析（否则分析会读 env 目录/旧观察）
        rl = self._rl()
        param_dir = Path(tempfile.mkdtemp(prefix="collab_rl23_param_"))
        self.addCleanup(shutil.rmtree, param_dir, ignore_errors=True)
        with patch.object(rl, "_sleep", new=AsyncMock(return_value=None)):
            summary = await rl.run_loop(
                rounds=1, interval=1, capture=True, demo=True,
                vision_dir=str(param_dir), verbose=False,
            )
        self.assertEqual(summary["rounds_run"], 1)
        self.assertTrue(
            summary["rounds"][0]["analysis_ok"],
            "分析应读取 --vision-dir 指定目录（采集与分析同源）",
        )
        self.assertTrue(list(param_dir.glob("observation-*.json")))
        self.assertFalse(
            list(self._vdir.glob("observation-*.json")),
            "env 目录（COLLAB_VISION_DIR）不应被写入",
        )


class RecurringScheduleTest(unittest.IsolatedAsyncioTestCase):
    """v3.35.0：周期/重复任务 schedule_recurring_task / list_schedules / set_schedule_enabled。"""

    async def asyncSetUp(self):
        _reset_collab_dir()
        self._saved_identity = {
            k: os.environ.get(k)
            for k in ("COLLAB_IDENTITY", "REQUIRE_IDENTITY", "COLLAB_ROLE")
        }
        for k in self._saved_identity:
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        for k, v in self._saved_identity.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def _past_start(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    async def test_create_and_list_schedule(self):
        r = _parse(await schedules.schedule_recurring_task(
            "每日整理", "清理终态垃圾", 1440,
            assignee="hub", priority="high", start_at=await self._past_start()))
        self.assertTrue(r["success"], r)
        schedule_id = r["schedule_id"]

        listed = _parse(await schedules.list_schedules())
        self.assertTrue(listed["success"])
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["schedules"][0]["schedule_id"], schedule_id)
        self.assertEqual(listed["schedules"][0]["interval_minutes"], 1440)
        self.assertEqual(listed["schedules"][0]["run_count"], 0)
        self.assertTrue(listed["schedules"][0]["enabled"])

    async def test_due_schedule_materializes_once(self):
        r = _parse(await schedules.schedule_recurring_task(
            "小时提醒", "x", 60, start_at=await self._past_start()))
        self.assertTrue(r["success"])
        schedule_id = r["schedule_id"]

        fired1 = schedules.fire_due_schedules()
        self.assertEqual(len(fired1), 1)
        self.assertEqual(fired1[0]["schedule_id"], schedule_id)
        task_id = fired1[0]["task_id"]
        task = safe_read_json(INBOX_DIR / f"{task_id}.json")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["recurring_schedule_id"], schedule_id)
        self.assertEqual(task["occurrence"], 1)

        fired2 = schedules.fire_due_schedules()
        self.assertEqual(fired2, [], "同一期不应重复生成")
        schedule = safe_read_json(SCHEDULES_DIR / f"{schedule_id}.json")
        self.assertEqual(schedule["run_count"], 1)
        self.assertEqual(schedule["last_task_id"], task_id)

    async def test_disabled_schedule_does_not_fire(self):
        r = _parse(await schedules.schedule_recurring_task(
            "停用调度", "x", 60, start_at=await self._past_start()))
        schedule_id = r["schedule_id"]
        stopped = _parse(await schedules.set_schedule_enabled(schedule_id, False))
        self.assertTrue(stopped["success"])

        self.assertEqual(schedules.fire_due_schedules(), [])
        schedule = safe_read_json(SCHEDULES_DIR / f"{schedule_id}.json")
        self.assertIs(schedule["enabled"], False)
        self.assertEqual(schedule["run_count"], 0)

    async def test_max_runs_stops_after_limit(self):
        r = _parse(await schedules.schedule_recurring_task(
            "只跑一次", "x", 60, start_at=await self._past_start(), max_runs=1))
        self.assertTrue(r["success"])
        schedule_id = r["schedule_id"]

        self.assertEqual(len(schedules.fire_due_schedules()), 1)
        self.assertEqual(schedules.fire_due_schedules(), [])
        schedule = safe_read_json(SCHEDULES_DIR / f"{schedule_id}.json")
        self.assertEqual(schedule["run_count"], 1)
        self.assertEqual(schedule["max_runs"], 1)

    def test_notify_daemon_fires_schedule_then_new_task_once(self):
        # 首次扫描只建基线，不展开调度；后续扫描应产生 schedule_fired + new_task，
        # 且同一期不重复。
        notifications_dir = COLLAB_DIR / "notifications"
        shutil.rmtree(notifications_dir, ignore_errors=True)
        state_file = notifications_dir / "state.json"
        notify_daemon.scan_once(COLLAB_DIR, state_file)

        result = asyncio.run(schedules.schedule_recurring_task(
            "通知到期", "x", 60, start_at=(
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(),
        ))
        schedule_id = _parse(result)["schedule_id"]

        events = notify_daemon.scan_once(COLLAB_DIR, state_file)
        event_types = [event["type"] for event in events]
        self.assertIn("schedule_fired", event_types)
        self.assertIn("new_task", event_types)
        self.assertEqual(events[0]["schedule_id"], schedule_id)

        second_events = notify_daemon.scan_once(COLLAB_DIR, state_file)
        self.assertNotIn(
            "schedule_fired",
            [event["type"] for event in second_events],
        )

    def test_notify_daemon_creates_missing_chat_dir_for_schedule(self):
        # 回归：全新 collab 目录可能还没有 chat/。周期任务展开后要能自己补建
        # chat/，不能因为 _system_chat_message 找不到父目录而崩。
        notifications_dir = COLLAB_DIR / "notifications"
        shutil.rmtree(notifications_dir, ignore_errors=True)
        shutil.rmtree(CHAT_DIR, ignore_errors=True)
        state_file = notifications_dir / "state.json"
        notify_daemon.scan_once(COLLAB_DIR, state_file)

        result = asyncio.run(schedules.schedule_recurring_task(
            "无 chat 目录", "x", 60, start_at=(
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(),
        ))
        self.assertTrue(_parse(result)["success"])

        events = notify_daemon.scan_once(COLLAB_DIR, state_file)
        self.assertIn("schedule_fired", [event["type"] for event in events])
        self.assertIn("new_task", [event["type"] for event in events])
        self.assertTrue(CHAT_DIR.is_dir())
        self.assertTrue(next(CHAT_DIR.glob("*.json"), None))

    async def test_invalid_inputs_rejected(self):
        bad_interval = _parse(await schedules.schedule_recurring_task(
            "非法周期", "x", 30))
        self.assertFalse(bad_interval["success"])

        bad_start = _parse(await schedules.schedule_recurring_task(
            "非法时间", "x", 60, start_at="not-a-time"))
        self.assertFalse(bad_start["success"])

    async def test_identity_required_rejected(self):
        os.environ["REQUIRE_IDENTITY"] = "1"
        try:
            r = _parse(await schedules.schedule_recurring_task("无身份", "x", 60))
            self.assertFalse(r["success"])
        finally:
            os.environ.pop("REQUIRE_IDENTITY", None)

