# 验证闸门清单 — PC-C 执行（2026-08-06 优化版）

> **2026-09-08 临时变更**：所有队友暂停（用户决议），T1/T2 闸门暂由**中枢以 PC-A 真实身份**认领执行
> （流程不变：五轴+全量+冒烟，evidence 照实）；报告命名 `results/hub-<版本>-verify-<日期>.md`
> （沿用 pc-c- 前缀会冒充队友身份）。队友恢复在线后切回 PC-C 执行并删除本段。

> 目标：消灭每次闸门重复的机械操作（VM git pull 必失败、CRLF diff 噪声、全量重跑），
> 把 PC-C 精力集中在**高价值的五轴审查 + 冒烟复核**上。
> 依据：v2.9 / v3.0 / v3.1 / v3.2 四次闸门报告取证（git pull 4/4 失败、diff -w 4/4 需容忍 CRLF、
> 全量套件 4 环境重复跑：本机 3.13 / uv 3.11 / CI Ubuntu 3.11 / VM CentOS 3.11）。

## 一、分级闸门

| 级别 | 适用场景 | 必做 | 示例 |
|---|---|---|---|
| **T1 全量** | 网络面/新后端/安全敏感/大改动 | 全量测试 + 五轴审查 + 冒烟复核 | v2.9 抓取后端、v3.0 媒体转写 |
| **T2 轻量** | 可选 provider/增量/小功能 | 新测试类 + 变更行审查 + 冒烟/证据复核（全量套件由 CI + 本机双环境覆盖） | v3.1/v3.2 本可适用 |
| **T3 免闸门** | 纯文档/版本号/README/ops 脚本 | CI 绿 + 派单方自查 | release notes、版本 bump、看门狗脚本 |

> 原则：闸门价值 = 独立代码审查（每次都能抓到真问题：v2.9 M1/M2、v3.2 中1 title 兜底）。
> 分级只裁剪**机械重复**，不裁剪**审查深度**——T2 仍必须对改动行逐行审查 + 给出分级清单。

## 二、标准步骤（共享 repo，无需 git pull）

VM 与宿主共享同一 repo（/mnt/hgfs/myshare = 宿主目录，**同一 .git**）：
VM 上**不要 git pull**（必因 known_hosts 报错），只需核对引用。

1. 认领 + 状态：`claim_task` → `get_collab_status`（identity=PC-C）
2. 一键核对 + 测试：`bash collab/scripts/verify_gate.sh`（无参=全量；带参只跑指定测试类）
3. 五轴审查：只**深读改动文件/新增函数**；未变更的轴直接注明「未变更，跳过深读」；
   派单方（PC-A）任务里的「审查重点」已圈定要深读的轴
4. 冒烟/证据复核：读 `results/` 冒烟记录 + 必要工具调用（如 search_documents 验证入库检索）
5. 报告写 `results/pc-c-<版本>-verify-<YYYYMMDD>.md`（UTF-8，模板见下）
6. `complete_task`：evidence（≤8000 字符）= 测试统计 + **明确结论**（approved / 需修改+纠正）+ 分级摘要；`result_path` 指向报告

## 三、报告模板（沿用既有五轴格式）

```
# PC-C <版本> 验证闸门报告
- 任务 / 验证人 / 执行时间 / 验证对象(commit) / 环境
## 一、执行记录（命令 + 结果）
## 二、测试统计（Ran N tests ... OK；新测试类用例数；hermetic 说明）
## 三、五轴审查（功能正确性 / 安全 / 并发状态 / 错误处理 / 可读性文档测试；未变更轴注明跳过）
## 四、分级问题清单（高/中/低，均给文件:行 + 具体修复建议）
## 五、结论（approved / 需修改）
```

## 四、一次性基建（2026-08-06 已上线，后续无需重复）

- **废除 VM git pull**：共享 repo 用 `git rev-parse HEAD origin/master` 核对（verify_gate.sh 已内置）
- **.gitattributes 换行归一化**：后续改动入库为 LF，CRLF diff 噪声逐渐消失；存量 CRLF 仍用 `git diff -w` 容忍
- **verify_gate.sh**：一键 repo 核对 + 测试，PC-C 不必再手拼命令
- 已知未做：ML reranker 补下载、历史搜索测试 mock 化（v2.9 L5，影响 flaky 不影响闸门结论）