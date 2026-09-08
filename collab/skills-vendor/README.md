# skills-vendor — 第三方技能包共享（2026-08-05 中枢安装）

供 PC-A（Codex）/ PC-C（Claude Code）共用。来源均为 MIT 开源，完整内容放共享
文件夹，任何队友可直接读取，无需先 clone。

## 内容

| 目录 | 来源 | 技能数 | 说明 |
|------|------|--------|------|
| `agent-skills/` | github.com/addyosmani/agent-skills | 24 | 全生命周期技能包（Define→Plan→Build→Verify→Review→Ship），**主 router** |
| `superpowers/` | github.com/obra/superpowers | 14（原仓） | 方法论技能；本平台**只精选 5 个**（见下） |

### 本平台已启用的 superpowers 精选（5 个）
`systematic-debugging`、`verification-before-completion`、`receiving-code-review`、
`using-git-worktrees`、`writing-skills`。
⚠️ **不要安装 superpowers 的 `using-superpowers`（router）与 `test-driven-development`**
——与 agent-skills 同名/同 router 冲突（两套 router 叠加会互相打架）。

## 技能内容位置

- 完整技能：`skills-vendor/agent-skills/skills/<名>/SKILL.md`（superpowers 同理）
- 检查清单：`skills-vendor/agent-skills/references/*.md`
- 平台注册表：`collab/skills/<名>.md`（find_skill / list_skills 可检索，entry 指向上述路径）

## PC-C（Claude Code）安装方式（任选其一）

1. **平台内使用（零安装）**：MCP `find_skill <名>` 检索 → `read_shared_file`
   `collab/skills-vendor/<pack>/skills/<名>/SKILL.md` 读全文照着执行。
2. **原生插件（推荐，体验最好）**：
   - agent-skills：`/plugin marketplace add addyosmani/agent-skills`
     → `/plugin install agent-skills@addy-agent-skills`
   - superpowers（可选）：`/plugin marketplace add obra/superpowers-marketplace`
     → `/plugin install superpowers@superpowers-marketplace`
   - 若 SSH clone 报 publickey，把 GitHub 域名改为 HTTPS 直连（见 agent-skills README）。
3. **手动拷贝**：把共享目录里 `SKILL.md` 拷到 PC-C 的 `~/.claude/skills/<名>/`。

## 提醒

- 技能在**新会话**才被加载（Codex/Claude 都在会话开始时读技能目录）。
- 更新：`git -C C:\myshare pull` 后重新拷共享目录即可（或重跑 `/plugin install`）。
