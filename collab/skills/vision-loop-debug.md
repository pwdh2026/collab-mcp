---
{
  "name": "vision-loop-debug",
  "description": "视觉闭环联调排障：采集帧 analyze_observation 出动作建议 → execute_action 执行并回传状态（body_context）→ 再分析，形成眼睛-大脑-身体闭环；定位链路问题",
  "capability_tags": [
    "vision:observe",
    "loop:closed",
    "debug:integration"
  ],
  "entry": "tools:analyze_observation,execute_action",
  "scope": "task",
  "status": "ready",
  "created_by": "local",
  "created_at": "2026-08-08T10:28:46.526909+00:00",
  "updated_by": "",
  "updated_at": ""
}
---

# vision-loop-debug（视觉闭环联调排障）

> 一句话：眼睛（采集帧）→ 大脑（analyze_observation 给动作建议）→ 身体（execute_action 执行）→ 状态回传（body_context 喂回大脑），四步构成闭环；本技能用于跑通和排查这条链。

## 触发
- 需要分析摄像头采集帧/图像，给出可执行动作建议时
- 闭环联调：动作执行后要确认状态变化、或链路某一步不工作（采集失败/建议不可执行/执行无效果）
- 复现与排查 v3.12-v3.15 眼睛-大脑-身体链路问题

## 完整闭环调用链（照此执行，不要跳步）
1. 采集：vision_capture.py --camera/--image/--demo → 观察 JSON 落共享 collab/vision/（或直接引用已有帧/观察）
2. 分析：analyze_observation(source=..., method="auto|heuristic|llm", body_context=<当前身体状态 JSON>)
   - body_context 必须来自上一轮 execute_action 返回的 state 字段，或 read_shared_file("body/state.json")；没有身体状态就不要传（行为与 v3.12 一致）
3. 执行：execute_action(action=<suggested_actions 中第一个可执行动作>, amount=1, detail=...) → 返回完整 state
4. 回传：把步骤 3 返回的 state 作为下一轮 analyze_observation 的 body_context（闭环关键，别漏）
- 无身体/纯分析场景：只做 1→2，suggested_actions 只作建议，不执行

## 边界与失败语义
- execute_action 是同步模拟器：无超时/重试概念；执行即落盘 collab/body/state.json（原子写）
- 动作无法识别 → fail 消息带可用动作词表（前进/后退/左转/右转/停止/补光/关灯/调整摄像头角度/观察）→ 按词表重发，绝不编造词表外动作
- analyze_observation 输出已受 v3.18 词表约束；suggested_actions 全被过滤掉时回退 heuristic
- 读身体状态两条途径：透传上一轮 execute_action 返回的 state（推荐）；或 read_shared_file("body/state.json")（MCP 侧无独立 get_body_state 工具）
- 无 cv2 环境：采集 stats 受限（无 Haar 人脸检测/增强特征），LLM 分析需 ollama（OLLAMA_VISION_MODEL）或 OpenAI 兼容端点；未配置 → 自动用 heuristic
- body_context 裁剪上限 500 字符（超长截断，防 token 放大）
