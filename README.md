# Claude 协作 MCP Server

![CI](https://github.com/pwdh2026/collab-mcp/actions/workflows/test.yml/badge.svg)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

让多台电脑上的多个 Claude 实例通过一个共享文件夹协作：任务分配、消息传递、队友注册、代码知识图谱查询，全部由文件驱动，无需中央数据库。

- 版本：v3.36.3（兼容 MCP 2.0 协议）
- 依赖：Python >= 3.10，`mcp >= 2.0.0`（`pip install -r collab/requirements.txt`）
- 部署形态：默认任一机器（推荐 VM）运行 `server.py`，各 Claude 客户端通过 SSH stdio 调用它；可选开启 Streamable HTTP

## 架构

```
PC-A 的 Claude ─┐                         ┌─ PC-B 的 Claude
PC-B 的 Claude ─┼─ SSH stdio → server.py ─┼─ PC-C 的 Claude
                │  (collab/ 共享文件夹)    │
                │   inbox/ 任务队列        │
                │   done/  已完成任务      │
                │   chat/  聊天消息        │
                │   teammates.json 注册表  │
                │   .codegraph/ 代码索引   │
                └──────────────────────────┘
```

任务和消息都是 JSON 文件：创建任务 = 往 `inbox/` 写一个文件；完成任务 = 从 `inbox/` 移到 `done/`；发消息 = 往 `chat/` 写文件。任何实例只需"看一眼共享目录"即可感知全局状态。

## 目录结构

```
collab-mcp/
├── README.md                ← 本文件
├── LICENSE                  ← GPL-3.0
├── .github/workflows/       ← CI（push/PR 自动跑全量回归）
├── .githooks/ + security/   ← 提交前密钥扫描
└── collab/                  ← 协作平台本体
    ├── server.py            ← 入口（CLI + stdio，可选 Streamable HTTP）
    ├── requirements.txt     ← Python 依赖（mcp>=2.0.0）
    ├── notify_daemon.py     ← L3 事件通知（新任务/消息 → feed + 系统消息）
    ├── dashboard.py         ← 静态看板生成
    ├── schedule_core.py     ← 周期任务调度核心
    ├── collab_mcp/          ← 代码包（56 个 MCP 工具注册于 app.py；
    │                          config/tasks/chat/teammates/status/health/
    │                          notifications/memory/artifacts/schedules/rag/
    │                          fixit/websearch/vision/body/dashboard 等模块）
    ├── tests/               ← 自动化回归测试（531 个用例）
    ├── skills/              ← 技能注册表种子（*.md，capabilities 标签路由）
    ├── templates/           ← 任务模板 / Playbook
    ├── scripts/             ← 验证闸门、巡检等辅助脚本
    └── docs/                ← 队友 Tailscale 接入指南
```

运行时状态目录（`collab/inbox/ done/ chat/ locks/ notifications/ teammates.json` 等）
由平台自身按文件读写生成，不入库（见 `.gitignore`）。

## 快速开始

### 1. 启动服务器（推荐在 VM 上）

```bash
python3 /mnt/hgfs/myshare/collab/server.py
```

本机开发/测试可直接在 Windows 运行（自动回退到仓库内目录）：

```powershell
python C:\myshare\collab\server.py
```

环境自检（非 MCP 模式，无需客户端）：

```bash
python3 .../server.py --version
python3 .../server.py --health
```

### 2. 可选：开启 Streamable HTTP

默认不开放 HTTP，仍走 SSH stdio。需要 HTTP 入口时，显式设置一个静态 Bearer Token，
服务会把该 token 映射到 `COLLAB_HTTP_IDENTITY` 指定的协作身份（默认 `PC-A`）：

```powershell
$env:COLLAB_HTTP = "1"
$env:COLLAB_HTTP_BEARER_TOKEN = "replace-with-a-long-random-token"
$env:COLLAB_HTTP_IDENTITY = "PC-A"
python C:\myshare\collab\server.py --http
```

默认监听 `127.0.0.1:8000/mcp`，可用 `COLLAB_HTTP_HOST` / `COLLAB_HTTP_PORT` /
`COLLAB_HTTP_PATH` 覆盖。未配置 token 时拒绝开放 HTTP 端口；这个静态 token 只作为
可信内网/本机入口的轻量门禁，不替代 SSH `authorized_keys` 的逐队友身份体系。
可选加固：设置 `COLLAB_HTTP_ALLOWED_IDENTITIES`（逗号分隔身份白名单）后，启动时校验
映射身份必须在白名单内，否则拒绝启动（v3.36.1）；不设置则行为不变。

### 3. 给 Claude 客户端配置 MCP

复制 [collab/mcp-config-template.json](collab/mcp-config-template.json) 中的 `collab` 段到 Claude 的 MCP 配置（`~/.claude.json` 或项目 `.claude/settings.json`）：

```json
"collab": {
  "type": "stdio",
  "command": "ssh",
  "args": ["centos-vm", "/usr/local/bin/python3.11", "/mnt/hgfs/myshare/collab/server.py"],
  "env": { "MSYS_NO_PATHCONV": "1" }
}
```

> **SSH 别名说明：** `centos-vm` 是中枢机本地 `~/.ssh/config` 里的别名示例（指向你的 VM）；
> 队友机可经中枢端口转发（如 8022 → VM 22）配置自己的别名，其余部分相同。
> 队友 Tailscale 组网接入见 [collab/docs/](collab/docs/)（setup-tailscale-vm.sh / setup-tailscale-teammate.bat）。

### 4. 新队友接入

1. 把本仓库共享给队友（git clone 或共享文件夹），让队友的 Claude 先阅读本 README
2. 配置好上述 MCP 后重启 Claude Code（确认 `collab` 工具出现在工具列表，应为 56 个）
3. 调用 `register_teammate` 注册（系统自动创建欢迎自检任务）
4. 按欢迎任务完成 `health_check` → `get_collab_status` → `send_message` → `complete_task`
5. 之后用 `/loop 3m <轮询内容>` 或手动轮询 `get_pending_tasks` 接任务

## 工具清单（56 个）

| 工具 | 作用 |
|------|------|
| `create_task` | 创建任务到 inbox，可带 project_path / related_symbols 代码上下文、execution_env 执行环境与 priority 优先级 |
| `get_pending_tasks` | 查待办，可按 assignee 筛选（`any` 任务所有人可见） |
| `claim_next_task` | 自动按 priority → deadline → created_at 认领下一个可执行任务，60s 队列锁防重复认领，支持队友 `max_concurrency` 并发上限 |
| `schedule_recurring_task` | 创建周期任务调度项，到期由 notify_daemon 在 inbox 生成普通任务（首版仅支持 >=60 分钟且 60 整数倍的 `interval_minutes`） |
| `list_schedules` | 列出 schedules/ 下的调度项，可按 `enabled_only` 过滤 |
| `set_schedule_enabled` | 启用或停用调度项，保留历史运行记录 |
| `complete_task` | 完成任务，移到 done/，可上报 total/input/output tokens、cost 与 model |
| `get_task_context` | 任务详情 + 自动查询 CodeGraph 代码洞察 |
| `register_artifact` | 登记任务的大文件交付物：先核对 artifacts/<task_id>/ 下真实文件 SHA256 与字节数，再写 manifest.json，杜绝无实物空登记 |
| `verify_artifact` | 按 manifest 重算交付物 SHA256/大小，返回 PASS/FAIL；文件缺失、大小或哈希不一致均判 FAIL |
| `force_assign` | 强制转移任务给其他队友（仅中枢；死信/超时任务回收，含转移审计） |
| `should_delegate` | 任务协作评分（0-100），按复杂度/机械性信号辅助拆解决策 |
| `acquire_project_lock` / `release_project_lock` | 项目级中央锁（改共享代码前取锁，防 Git 冲突，2 小时 TTL） |
| `list_project_locks` | 查看当前所有有效项目锁 |
| `send_message` / `get_chat_history` | 异步消息/聊天记录 |
| `get_collab_status` | 各目录文件数量概览 |
| `health_check` | 目录读写、磁盘、内存、依赖版本等自检 |
| `register_teammate` / `list_teammates` | 队友注册/列表 |
| `query_codegraph` | 原生 SQLite 查询代码知识图谱（FTS5 + LIKE 回退） |
| `list_shared_dir` | 列出共享文件夹目录内容（队友机可通过 MCP 查看） |
| `read_shared_file` | 读取共享文件夹中的文本文件（≤256KB，队友机可读指南） |
| `get_notifications` | 读取通知事件流（新任务/新消息，notify_daemon 写入） |
| `remember_fact` | 记录团队记忆事实到 collab/memory/*.md（标题/正文/标签/来源，身份可溯） |
| `search_memory` | 全文（多词 AND）+ 标签检索团队记忆，按时间倒序返回 |
| `register_skill` / `list_skills` / `find_skill` | 技能注册表：登记/浏览/检索 collab/skills/*.md（capabilities 标签路由，entry 可直接派单） |
| `add_document` / `list_documents` | 文档摄入：PDF/DOCX/HTML/EPUB/RTF/MD → 共享 documents/ 目录（转换器自动降级，sha256 去重） |
| `search_documents` | 全文检索已摄入文档：SQLite FTS5（trigram，中文子串友好），多词 AND + 命中摘要 + bm25 相关度 |
| `rag_query` | 本地 RAG：关键词 + 向量混合召回（RRF 去重排序）→ 原文片段 → 本地 Ollama 引用式回答；Ollama 不可用时自动降级 retrieval-only |
| `search_troubleshooting` | 检索本地 fixit 排障索引：症状 → 带 `source_path` 的修法卡片，源语料变化自动重建 |
| `web_search` | 外部互联网搜索（默认 Bing 免费解析，DuckDuckGo/Firecrawl 自动降级；可选 wigolo：设置 WIGOLO_REST_URL 指向本地 wigolo serve 即自动成为首选，18 引擎聚合 + ML 重排；可用 WEB_SEARCH_PROVIDER 覆盖）：query/max_results/timeout，返回 title/url/snippet/source/rank 规范化摘要 |
| `web_fetch` | 抓取网页转文本（Firecrawl 优先、标准库兜底），默认入库 documents/web/，含 SSRF 防护与 sha256 去重 |
| `transcribe_media` | 媒体转写入库（v3.0）：本地音频/视频 → 转录文本 → documents/media/（faster-whisper/openai-whisper/OpenAI API 优雅降级后端，无后端明确报错），内容指纹去重 + search_documents 闭环 |

> 🔧 transcribe_media 本地后端启用：`pip install faster-whisper`（CPU int8 小模型，首次转写自动下载模型）；
> 或设置 `OPENAI_API_KEY` 用云端 whisper-1；两者都没有时工具返回明确报错。

## 测试

```powershell
cd C:\myshare\collab
python -m unittest discover -s tests -v
```

531 个用例覆盖：任务生命周期（含幂等完成、done 残留清理）、**显式认领状态机**（v1.7.1）、**任务级 journal 事件溯源**（v1.7.2）、**capabilities 标签派单匹配**（v1.7.3）、**人工复核闸门**（v1.8.0）、**认领超时自动回收**（v1.8.1）、**消息关联任务**（v1.8.2）、**生命周期指标**（v1.8.3）、**任务模板/Playbook**（v1.8.4）、**静态只读看板**（v1.8.5）、**多步骤流水线**（v1.9.0，依赖解锁 + 每步超时）、**失败分支/跳过级联 + pipeline_status 聚合**（v1.9.1）、**看板流水线分组**（v1.9.2）、**可视化 DAG 看板**（v2.3.x，SVG 节点图：depends_on 为边、状态着色、data-dep/data-status 可测试；缺 id/循环依赖容错）、**并行扇出/汇聚**（v2.0.0）、**条件分支**（v2.0.1）、**失败自动重试 Retry Loop**（v2.0.2）、**种子流水线**（v2.4.x，pipeline-research 研究→草稿→复核闸门→定稿 / pipeline-security-review lint→安全审查闸门；缺参回滚含 journal 清理）、**ocr 委托审查流水线**（v2.4.3，pipeline-ocr-delegate-review：中枢生成 delegate 规格 → host agent 行级审查，复核闸门；repo/spec_assignee 可参数化，适用于无 git 队友机，方案 B）、**技能注册表**（v2.5.x，list_skills/find_skill/register_skill：collab/skills/*.md + capabilities 标签路由，20 个种子技能；v2.5.1 吸收 PC-B 反馈：CJK 检索措辞回归 + 归一化语义明确）、**文档摄入**（v2.6.x，add_document/list_documents：PDF/DOCX/HTML/EPUB/RTF/MD → 共享 documents/ 目录，转换器自动降级零硬依赖，元数据头 + 安全 slug + 路径防护；v2.6.1 吸收验证反馈：%PDF- 魔数校验 + HTML 有序列表/代码块修复 + sha256 去重）、**文档全文检索**（v2.7.0，search_documents：SQLite FTS5 trigram 全文索引（中文子串友好），多词 AND + 命中摘要 + bm25 相关度，target_dir 可限定任意共享子目录；索引为每进程派生缓存自动重建；含 trigram+UNINDEXED 列 LIKE 失效回归；v2.7.2 中文语料实弹：纯中文子串/中英混排 AND/全角标点 NFKC/中文 BM25 排序，8 条 CjkSearchTest）、**本地排障检索**（v3.29.0，search_troubleshooting：复用 fixit 独立 FTS5 排障索引，症状 → 修法卡片 + source_path，HTTP/索引损坏时明确报错不崩服务）、**本地 RAG 一体机**（v3.30.2，rag_query：关键词 + 向量混合召回，RRF 去重排序，本地 Ollama 依据资料引用式回答，并有 prompt/source grounding 防“资料未说明”；Ollama 不可用时自动 retrieval_only）、**产品化 MVP 三增强**（v3.32.0，claim_next_task 原子优先级认领 + heartbeat 队友 last_seen + complete_task token/成本上报，create_task 新增 priority）、**大文件交付登记**（v3.33.0，register_artifact/verify_artifact：登记前核对真实 SHA256/size，接收方校验返回 PASS/FAIL，覆盖缺失/篡改/路径穿越）、**按模型成本汇总**（v3.34.0，get_task_metrics 新增 by_model：按 token_usage.model 聚合 token/成本/请求数与均值，缺失 model 的任务不混入 by_model）、**周期/重复任务**（v3.35.0，schedule_recurring_task/list_schedules/set_schedule_enabled：文件驱动的 hours 粒度调度项，notify_daemon 到期展开为 inbox 普通任务，同一期不重复生成，全新目录缺 chat/ 时自动补建）、**可选 Streamable HTTP 静态 Bearer 鉴权**（v3.36.0，`--http`/`COLLAB_HTTP` 显式开启，`COLLAB_HTTP_BEARER_TOKEN` 必填，constant-time 校验后映射单一 `COLLAB_HTTP_IDENTITY`；默认 stdio 与未配置 token 时旧行为完全不变）、**HTTP 身份白名单启动校验**（v3.36.1，`COLLAB_HTTP_ALLOWED_IDENTITIES` 逗号白名单：HTTP 映射身份不在名单内或白名单配置为空则拒绝启动，未设置时行为不变，5 条白名单用例）、**JSON 读取健壮性**（v3.36.2，safe_read_json 对缺失文件静默、对损坏仍告警）、**启动顺序加固**（v3.36.3，token/白名单校验前置于 app 导入，verifier 身份与被校验身份同源；3 条子进程接线用例）、**外部检索**（v2.8.0，web_search/web_fetch：Bing 免费默认 + DuckDuckGo/Firecrawl 自动降级链 + WEB_SEARCH_PROVIDER 覆盖、SSRF 防护、超时/限流/网络错误映射、网页抓取默认入库 documents/web/ 与 add_document 闭环，23 条 WebSearch/SSR 用例）、**团队记忆**（v2.2.x，remember_fact/search_memory 全文+标签检索，含身份校验、原子写入与元数据异常容错）、assignee 筛选与**身份隔离**（v1.5.0，含 REQUIRE_IDENTITY 强制开关）、**force_assign / deadline / evidence / 结果截断**（v1.6.0-v1.7.0）、**should_delegate 协作评分**与**项目中央锁**（v1.7.0）、聊天往返、队友注册（含欢迎任务与身份校验）、状态统计、健康检查、CodeGraph 查询（含缓存重建恢复）、56 个工具注册、共享文件夹读写（含路径穿越防护）、通知 daemon（基线/防重复/崩溃恢复/feed 裁剪/系统消息去重/**stale_task 超时告警**/**schedule_fired 周期任务通知**）、CLI 输出（Windows UTF-8）、真实 MCP stdio 协议端到端（含中文传输）。测试使用独立临时目录且**隔离身份环境变量**（SSH 场景 hermetic，v2.2.1），不污染真实数据。

## 身份隔离（v1.5.0）

平台按 SSH 公钥识别调用者身份（`COLLAB_IDENTITY` 环境变量，由 VM 的
`authorized_keys` 为每个队友公钥绑定），实现任务级权限隔离：

- 队友只能看到/完成指派给自己或 `assignee=any` 的任务；`complete_task`、
  `get_task_context`、`get_pending_tasks` 均有校验
- `register_teammate` 的注册名必须与 SSH 身份一致，防止冒名注册
- 中枢（PC-A 或 `COLLAB_ROLE=hub`）保留全量权限
- 未配置身份时保持旧的开放行为（工具响应带 `identity: null` 提示）；
  生产环境设 `REQUIRE_IDENTITY=1` 可强制要求身份——未绑定公钥的会话
  直接拒绝访问任务/注册类工具，消除"空身份=全权限"风险

VM 侧配置：`/etc/ssh/sshd_config` 开 `PermitUserEnvironment yes`，然后
在队友公钥行前加 `environment="COLLAB_IDENTITY=<名字>",no-port-forwarding `。
队友组网接入脚本见 [collab/docs/](collab/docs/)。

## 运维

- **连通性自检**：`collab/check_ports.sh`（VM 侧 6 步检查）
- **主机防火墙**：`collab/setup_host_firewall.ps1` 一键放行 SSH
- **日志**：`server.log`，超过 10MB 自动归档，保留最近 3 份
- **健康检查**：`health_check` 工具或 `python server.py --health`
- **事件通知（L3）**：`notify_daemon.py` 监控新任务/新消息，写入 `notifications/feed.jsonl` 并自动在聊天发系统消息。VM 上安装每分钟扫描：`bash collab/setup_notify_vm.sh`
- **GitHub 巡检**：`GH_STATUS_REPOS="you/repo1,you/repo2" python collab/scripts/gh_status.py [YYYYMMDD]` 聚合指定仓库的 issue/PR/release/提交/CI 状态，生成 `results/gh-status-YYYYMMDD.md`，可配合 `add_document` 摄入知识库（需 gh 已登录）

## 路线图

- [x] 拉一个真实队友做端到端闭环验证（PC-B 已于 2026-08-03 完成注册 → 接任务 → 完成 → 中枢确认）
- [x] 队友可通过 MCP 读取共享文件夹（list_shared_dir / read_shared_file）
- [x] L3 事件通知：notify_daemon + get_notifications + VM cron（v1.4.0）
- [x] 身份隔离：SSH 公钥 → COLLAB_IDENTITY → assignee 权限校验（v1.5.0）
- [x] CodeGraph 缓存加固：每进程副本 + busy_timeout + 损坏自动重建（v1.5.0）
- [x] 死信与超时处理：notify_daemon stale_task 告警 + force_assign 转移（v1.6.0）
- [x] 验证闭环：complete_task 支持 evidence、get_task_context 可读 done/（v1.6.0）
- [x] 上下文摘要协议：evidence 8000 字符截断 + result_path 引用 + 代码上下文截断（v1.7.0）
- [x] should_delegate 协作评分（v1.7.0）
- [x] 项目级中央锁：acquire/release/list + 2 小时 TTL（v1.7.0）
- [x] 任务状态机：claim_task 显式认领 + 隐式 self-claim + 队友视角过滤（v1.7.1）
- [x] 任务级事件日志 journal：时间旅行回放 + get_task_context history（v1.7.2）
- [x] capabilities 结构化（skills 标签 + max_concurrency）+ should_delegate 标签匹配（v1.7.3）
- [x] 高风险任务人工复核闸门：review_required + approve_task / request_changes（v1.8.0）
- [x] 认领超时自动回收：claim_stale + timeout_released（v1.8.1）
- [x] 消息关联任务：send_message task_id + get_chat_history filter_task_id（v1.8.2）
- [x] 任务生命周期指标：get_task_metrics（v1.8.3）
- [x] 任务模板/Playbook：list_templates + create_task(template=...)（v1.8.4）
- [x] 静态只读看板：generate_dashboard + dashboard.py CLI（v1.8.5）
- [x] 多步骤流水线：模板 pipeline 展开 + 依赖解锁 + 每步认领超时（v1.9.0）
- [x] 流水线失败分支：fail_task + 级联跳过 + pipeline_status 聚合 + URGENT 告警（v1.9.1）
- [x] 看板流水线分组：聚合状态 + 步骤 chips（v1.9.2）
- [x] 并行扇出/汇聚：pipeline-parallel-review（v2.0.0）
- [x] 条件分支：步骤 if 条件 + 创建期级联跳过（v2.0.1）
- [x] Retry Loop：max_retries 失败自动重试（v2.0.2）
- [x] 可选 Streamable HTTP + 静态 Bearer Token 鉴权（v3.36.0）
- [x] HTTP 身份白名单启动校验：`COLLAB_HTTP_ALLOWED_IDENTITIES`（v3.36.1）
- [x] safe_read_json 缺失文件静默（未持有锁时不再误报"损坏文件"WARNING，v3.36.2）
- [x] 校验先于构建：`app` 推迟到 token/白名单校验后导入，verifier 身份与被校验身份同源（v3.36.3）
- [x] GitHub Actions CI：push/PR 自动跑全量回归

## 许可证

本项目以 **GPL-3.0-only** 授权（见 [LICENSE](LICENSE)）：你可以自由使用、研究、修改和分发本软件；
任何基于本代码的衍生作品（含修改版与二次分发）**必须以同等的 GPL-3.0 开源条件发布源码**——
不允许将其闭源商用。
