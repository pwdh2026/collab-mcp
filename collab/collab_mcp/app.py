"""MCP Server 实例与工具注册。"""

import os

from mcp.server import MCPServer

from . import __version__
from . import (
    ai_teacher,
    artifacts,
    body,
    bootstrap,
    chat,
    codegraph,
    dashboard,
    delegation,
    documents,
    files,
    fixit,
    health,
    http_auth,
    locks,
    media,
    memory,
    notifications,
    rag,
    schedules,
    search,
    semantic,
    skills,
    status,
    summarize,
    tasks,
    teammates,
    vision,
    websearch,
)

_server_kwargs = {
    "name": "claude-collab-server",
    "title": "Claude 协作 MCP Server",
    "description": "多 Claude 实例协作消息中枢 — 任务分配、消息传递、状态管理、代码智能、队友注册",
    "version": __version__,
}

# 只有显式配置静态 Bearer Token 时，HTTP 入口才启用鉴权；否则 stdio
# 路径以及 server 实例保持与旧版完全一致。
_http_token = os.environ.get(http_auth.ENV_HTTP_TOKEN, "").strip()
if _http_token:
    _server_kwargs["token_verifier"] = http_auth.build_token_verifier()
    _server_kwargs["auth"] = http_auth.build_auth_settings()

server = MCPServer(**_server_kwargs)

# 注册全部 MCP 工具（顺序即 MCP list_tools 的展示顺序）
_TOOLS = [
    bootstrap.bootstrap,
    tasks.create_task,
    artifacts.register_artifact,
    artifacts.verify_artifact,
    tasks.get_pending_tasks,
    tasks.complete_task,
    tasks.claim_task,
    tasks.claim_next_task,
    schedules.schedule_recurring_task,
    schedules.list_schedules,
    schedules.set_schedule_enabled,
    tasks.heartbeat,
    tasks.mark_research_done,
    tasks.get_task_context,
    tasks.force_assign,
    tasks.approve_task,
    tasks.request_changes,
    tasks.fail_task,
    tasks.list_templates,
    skills.register_skill,
    skills.list_skills,
    skills.find_skill,
    documents.add_document,
    documents.list_documents,
    search.search_documents,
    fixit.search_troubleshooting,
    semantic.semantic_search,
    rag.rag_query,
    websearch.web_search,
    websearch.web_fetch,
    websearch.web_research,
    websearch.web_agent,
    summarize.summarize_text,
    ai_teacher.explain_work,
    media.transcribe_media,
    vision.analyze_observation,
    body.execute_action,
    delegation.should_delegate,
    locks.acquire_project_lock,
    locks.release_project_lock,
    locks.list_project_locks,
    chat.send_message,
    chat.get_chat_history,
    status.get_collab_status,
    status.get_task_metrics,
    dashboard.generate_dashboard,
    dashboard.dashboard_data,
    health.health_check,
    teammates.register_teammate,
    teammates.list_teammates,
    codegraph.query_codegraph,
    files.list_shared_dir,
    files.read_shared_file,
    notifications.get_notifications,
    memory.remember_fact,
    memory.search_memory,
]

for _tool in _TOOLS:
    server.tool()(_tool)

TOOL_COUNT = len(_TOOLS)
