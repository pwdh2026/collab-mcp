"""视觉观察分析工具：analyze_observation（v3.12 眼睛-大脑 Step 1；v3.14 大脑-身体双向反馈）。

设计来源：progress/collab-v3.12-vision-eyes-brain-design.md
- 眼睛侧（vision_capture.py，PC-C 用 OpenCV 采集）产出结构化观察到共享目录
  collab/vision/（observation-<id>.json + frame-<id>.jpg + latest.json）。
- 大脑侧本工具读取观察 → LLM（ollama/OpenAI-compatible，urllib 零新依赖）综合推理，
  产出「场景解释 + 建议动作 + 置信度」；无 LLM 配置时 heuristic 兜底（零 API 成本）。
- 可选视觉模型：设 OLLAMA_VISION_MODEL（如 llava）后，with_image=true 且帧存在时，
  以 data URI 把 JPEG base64 作为 image_url 内容发送（ollama OpenAI 兼容端点支持）。

v3.25：image_path 语义统一——采集侧写「帧文件相对观察 JSON 所在目录」的路径（默认共置
为纯文件名），消费侧按观察文件目录解析；存量旧格式（相对 vision 根父目录）自动回退。

v3.14 Step 3：新增 body_context 参数——调用方传入身体状态 JSON（collab/body/state.json，
如 robot_loop 读取），大脑据此做双向反馈：heuristic 区分「补光未开→开灯 / 已开→调朝向」、
静态时给出带当前朝向的转向扫描建议；LLM 分支把身体状态追加进提示词。

安全约定：身份闸门复用；source 解析后必须位于 vision 根目录内（防穿越）；
日志只记 observation_id 不记帧内容；图片 base64 上限 2MB。
"""

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .identity import assert_identity_allowed
from .logging_setup import logger
from .utils import fail, ok

_OBS_SCHEMA_PREFIX = "collab-vision-observation-v"
_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 图片 base64 上限 2MB
_MAX_BODY_CONTEXT_CHARS = 500  # body_context 拼 LLM 提示词上限（PC-C L2，防 token 放大）


def _vision_dir() -> Path:
    """观察共享目录：可用 COLLAB_VISION_DIR 覆盖（测试隔离用），默认 collab/vision/。"""
    return Path(os.environ.get("COLLAB_VISION_DIR") or
                (Path(__file__).resolve().parent.parent / "vision"))


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _llm_base_url(env_key: str, default: str) -> str:
    """归一化 OpenAI-compatible 服务根地址：去掉尾部 /v1，URL 拼装时统一补 /v1/chat/completions。

    避免默认值/用户值同时含 /v1 时拼出 /v1/v1/ 404（PC-C 闸门低-1，2026-08-07 吸收）。
    """
    base = (os.environ.get(env_key) or default).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _llm_configured() -> str:
    """返回可用 LLM 后端标识（ollama/openai/''）；auto 探测用。"""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_MODEL"):
        return "ollama"
    return ""


def _vision_model() -> str:
    """返回配置的视觉模型名（未配置返回 ''）。"""
    return (os.environ.get("OLLAMA_VISION_MODEL") or "").strip()


def _resolve_observation(source: str, root=None):
    """把 source 解析为观察文件绝对路径；越界/不存在返回 None。

    source 取值：latest（默认）→ vision/latest.json；文件名 → vision/<name>；
    绝对路径 → 必须是 vision 根目录内的路径（防穿越）。
    """
    root = (root or _vision_dir()).resolve()
    if not source or source.strip().lower() in ("latest", ""):
        p = root / "latest.json"
    else:
        cand = Path(source)
        if not cand.is_absolute():
            cand = root / cand
        try:
            r = cand.resolve()
        except OSError:
            return None
        # 必须位于 vision 根目录内
        try:
            r.relative_to(root)
        except ValueError:
            return None
        p = r
    return p if p.is_file() else None


def _read_observation(path: Path):
    """读取并校验观察 JSON；非法返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError, OSError):
        return None
    schema = data.get("schema") or ""
    if not schema.startswith(_OBS_SCHEMA_PREFIX):
        return None
    return data


def _parse_body_context(body_context: str) -> dict:
    """解析 body_context（身体状态 JSON 字符串）；非法/空返回 {}（仅降级，不失败）。"""
    if not body_context:
        return {}
    try:
        data = json.loads(body_context)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_light_on(light) -> bool:
    """宽松判断补光开启：容忍 bool/数字/字符串（PC-C L3）。"""
    return light in (True, 1, "true", "True")


def _is_light_off(light) -> bool:
    """宽松判断补光关闭：容忍 bool/数字/字符串。"""
    return light in (False, 0, "false", "False")


def _trim_body_context(body_context: str) -> str:
    """裁剪 body_context 到上限（PC-C L2），避免超长状态放大 LLM token。"""
    if len(body_context) <= _MAX_BODY_CONTEXT_CHARS:
        return body_context
    return body_context[:_MAX_BODY_CONTEXT_CHARS] + "…(已截断)"


def _saturated_share(colors: list) -> float:
    """dominant_colors 中高彩度桶的占比和（RGB 通道极差 ≥100 视为饱和色）。

    v3.19：配合 sat_mean/edge 联合判定「彩色目标」——集中高饱和色块才是目标，
    整体亮度/饱和度抬升（如开灯）不构成目标。
    """
    total = 0.0
    for c in colors or []:
        if not isinstance(c, dict):
            continue
        share = c.get("share")
        color = str(c.get("color") or "")
        if not isinstance(share, (int, float)) or not color.startswith("#"):
            continue
        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
        except ValueError:
            continue
        if max(r, g, b) - min(r, g, b) >= 100:
            total += float(share)
    return total


def _heuristic_interpret(obs: dict, body_context: str = "") -> tuple:
    """零 API 成本规则解释：(interpretation, suggested_actions, confidence)。

    v3.14：body_context（身体状态 JSON）参与推理——暗场景区分「补光未开→开灯 /
    已开→调朝向」；静态无目标时给出带当前朝向的转向扫描建议。无 body_context 时
    行为与 v3.12 完全一致。
    """
    stats = obs.get("stats") or {}
    dets = obs.get("detections") or []
    body = _parse_body_context(body_context)
    light = body.get("light")
    heading = body.get("heading_deg")
    brightness = stats.get("brightness")
    motion = stats.get("motion")
    faces = [d for d in dets if d.get("kind") == "face"]
    parts: list[str] = []
    actions: list[str] = []
    if brightness is None:
        parts.append("无亮度信息")
    elif brightness < 0.25:
        if _is_light_on(light):
            # 避开「补光」子串：该动作会交给 body.execute_action 子串匹配，含「补光」会被
            # light_on 别名先命中（PC-C M1：camera 永不转动）。用「灯光」替代。
            parts.append("画面光线偏暗（亮度 %.0f%%），但身体补光已开启" % (brightness * 100))
            actions.append("调整摄像头角度/朝向（灯光已开，需改变视角）")
        elif _is_light_off(light):
            parts.append("画面光线偏暗（亮度 %.0f%%），可能影响识别质量" % (brightness * 100))
            actions.append("开启补光（当前未开启）")
        else:
            # light 未知（无 body_context 或 body 无 light 字段）：保持 v3.12 原文（PC-C M2 回归面）
            parts.append("画面光线偏暗（亮度 %.0f%%），可能影响识别质量" % (brightness * 100))
            actions.append("提示调整补光或摄像头朝向")
    elif brightness > 0.9:
        parts.append("画面光线过曝（亮度 %.0f%%）" % (brightness * 100))
    if motion is not None and motion > 0.15:
        parts.append("画面存在明显运动/变化（motion %.0f%%）" % (motion * 100))
        actions.append("保持观察并复核动态变化来源")
    if faces:
        parts.append("检测到 %d 张人脸" % len(faces))
        actions.append("靠近观察目标人脸（可请 PC-C 复核画面）")
    # v3.15 感知增强：heuristic 消费增强特征（sat/edge/gray_var），规则识别彩色目标/细节/对比
    sat = stats.get("sat_mean")
    edge = stats.get("edge_density")
    gvar = stats.get("gray_var")
    sat_share = _saturated_share(stats.get("dominant_colors"))
    # v3.19：彩色目标 = 集中高饱和色块（饱和桶占比） + 边缘信号（物体边界）；
    # 避免「开灯整体提饱和」误报（场景B sat=101 但 edge=0.045 不触发，场景C sat=62.9/edge=0.054 触发）
    if (
        isinstance(sat, (int, float)) and 40 <= sat <= 130
        and isinstance(edge, (int, float)) and edge > 0.05
        and sat_share >= 0.05
    ):
        parts.append("画面存在集中高饱和色块（饱和度 %.0f，饱和桶占比 %.0f%%），疑似彩色目标/物体"
                     % (sat, sat_share * 100))
        actions.append("靠近观察彩色目标（可请 PC-C 复核画面）")
    elif isinstance(sat, (int, float)) and sat > 60:
        parts.append("画面饱和度较高（%.0f），可能存在彩色内容" % sat)
    if isinstance(edge, (int, float)) and edge > 0.05:
        parts.append("画面细节/边缘较丰富（edge_density %.3f）" % edge)
        if not any("观察" in a for a in actions):
            actions.append("关注画面细节区域并观察")
    if isinstance(gvar, (int, float)) and gvar > 3000:
        parts.append("画面对比度较强（gray_var %.0f）" % gvar)
    if not parts:
        if isinstance(heading, (int, float)):
            parts.append("静态场景，未检出显著目标（朝向 %d°，建议转向扫描）" % int(heading))
            actions.append("左转或右转扫描（当前朝向 %d°）" % int(heading))
        else:
            parts.append("静态场景，未检出显著目标（亮度 %.0f%%，无运动）" %
                         ((brightness or 0) * 100))
            actions.append("保持观察，或调整摄像头角度覆盖目标区域")
    if not actions:
        actions.append("保持观察，等待变化")
    confidence = "medium" if faces or (motion or 0) > 0.15 else "low"
    return "；".join(parts), actions, confidence



def _image_data_uri(obs: dict, root=None, obs_dir=None) -> str:
    """把观察帧编码为 data URI；不存在/超限/越界返回 ''。

    v3.25 语义统一（PC-C v3.23 低-1）：image_path 相对**观察 JSON 所在目录**（采集侧与
    帧共置，通常为纯文件名）；obs_dir 传观察文件父目录。兼容旧格式——相对 vision 根
    父目录（如 vision/frame-<id>.jpg）或相对根目录——按候选顺序回退。
    绝对路径与所有候选都必须位于 vision 根目录内（防穿越）。
    """
    rel = obs.get("image_path") or ""
    if not rel:
        return ""
    root = (root or _vision_dir()).resolve()
    cand = Path(rel)
    if cand.is_absolute():
        candidates = [cand]
    else:
        base = Path(obs_dir).resolve() if obs_dir else root
        candidates = [base / rel, root.parent / rel, root / rel]
    for c in candidates:
        try:
            r = c.resolve()
            r.relative_to(root)
        except (OSError, ValueError):
            continue
        if r.is_file() and r.stat().st_size <= _MAX_IMAGE_BYTES:
            b64 = base64.b64encode(r.read_bytes()).decode("ascii")
            return "data:image/jpeg;base64," + b64
    return ""


def _constrain_actions(actions: list, fallback: list) -> "tuple[list, int]":
    """把建议动作约束到 body 词表：仅保留可归一化的动作（保序、按归一化去重、上限 3）。

    v3.18：LLM 常编造词表外动作（如「打开舱门」）→ execute_action 失败；
    此处过滤掉不可执行项，全部无效时回退 heuristic 动作（保证闭环可执行）。
    返回 (有效动作原文本列表, 被过滤数)。
    """
    from .body import _normalize_action

    out: list = []
    seen: set = set()
    dropped = 0
    for a in actions or []:
        text = str(a).strip()
        if not text:
            dropped += 1
            continue
        norm = _normalize_action(text)
        if not norm:
            dropped += 1
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(text)
        if len(out) >= 3:
            break
    if not out:
        return list(fallback or []), dropped
    return out, dropped


def _parse_llm_response(content: str, obs: dict, body_context: str = "") -> tuple:
    """尽力解析 LLM JSON；失败则整段当 interpretation、heuristic 动作兜底（透传 body_context，PC-C L1）。"""
    text = content.strip()
    # 去 markdown 代码围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
        else:
            raise ValueError("no json object")
        interp = str(data.get("interpretation") or "").strip()
        acts_raw = data.get("suggested_actions") or []
        acts = [str(a).strip() for a in acts_raw if str(a).strip()] if isinstance(acts_raw, list) else []
        conf = str(data.get("confidence") or "low").strip().lower()
        if conf not in ("high", "medium", "low"):
            conf = "low"
        if not interp:
            raise ValueError("empty interpretation")
        # v3.18：LLM 建议动作约束到 body 词表（可执行性；编造动作丢弃，全丢回退 heuristic）
        _, fallback_actions, _ = _heuristic_interpret(obs or {}, body_context)
        acts, dropped = _constrain_actions(acts, fallback_actions)
        if dropped:
            logger.info(f"🧠 LLM 建议动作过滤 {dropped} 条非词表项")
        return interp, acts or ["保持观察，等待进一步指令"], conf
    except (ValueError, json.JSONDecodeError):
        # 非 JSON 兜底：整段当解释，动作用 heuristic（基于真实观察）
        _, fallback_actions, fallback_conf = _heuristic_interpret(obs or {}, body_context)
        return text[:500], fallback_actions, fallback_conf


def _llm_analyze(obs: dict, focus: str, timeout: int, with_image: bool,
                 body_context: str = "", root=None, obs_dir=None) -> tuple:
    """LLM 综合推理（OpenAI-compatible chat completions）。

    返回 (interpretation, suggested_actions, confidence, backend)。
    v3.14：body_context（身体状态 JSON）追加进提示词，让 LLM 结合身体状态推理。
    """
    if os.environ.get("OPENAI_API_KEY"):
        base = _llm_base_url("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
        backend = "openai"
    else:
        base = _llm_base_url("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        model = os.environ.get("OLLAMA_MODEL") or "qwen2.5:3b"
        headers = {"Content-Type": "application/json"}
        backend = "ollama"

    obs_digest = {
        "source": obs.get("source"),
        "stats": obs.get("stats"),
        "detections": obs.get("detections"),
    }
    focus_text = focus.strip() or "无（请概括当前场景）"
    system_prompt = (
        "你是机器人「大脑」的视觉感知推理模块。基于结构化观察（来自摄像头/OpenCV 检测）"
        "与当前身体状态，给出场景解释与建议动作。严格输出 JSON："
        '{"interpretation": "≤150字中文场景解释", "suggested_actions": ["动作1", "动作2", "动作3"], '
        '"confidence": "high|medium|low"}。不要输出 JSON 之外的任何内容。'
        "建议动作必须是可执行动作，从以下词表选择（中文）：前进、后退、左转、右转、停止、"
        "补光、关灯、调整摄像头角度、观察。每个动作 ≤12 字，最多 3 个。"
    )
    user_text = (
        "结构化观察（JSON）：%s\n关注点：%s\n请分析。"
        % (json.dumps(obs_digest, ensure_ascii=False), focus_text)
    )
    if body_context:
        user_text += "\n当前身体状态（JSON）：%s" % _trim_body_context(body_context)

    vision_model = _vision_model()
    use_vision = bool(with_image and vision_model)
    if use_vision:
        uri = _image_data_uri(obs, root=root, obs_dir=obs_dir)
        if uri:
            model = vision_model
            backend = backend + "-vision"
            content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": uri}},
            ]
        else:
            use_vision = False  # 无可用帧 → 退回纯文本
    if not use_vision:
        content = user_text

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM 分析失败（HTTP {e.code}）: {e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"LLM 分析失败（网络）: {e}") from e
    content = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "")
    content = (content or "").strip()
    if not content:
        raise RuntimeError("LLM 分析返回空内容")
    interp, actions, confidence = _parse_llm_response(content, obs, body_context)
    return interp, actions, confidence, backend


async def analyze_observation(
    source: str = "latest",
    focus: str = "",
    mode: str = "auto",
    with_image: bool = False,
    timeout: int = 60,
    body_context: str = "",
    vision_dir: str = "",
) -> str:
    """眼睛观察 → 大脑分析（场景解释 + 建议动作 + 置信度）。

    Args:
        source: 观察来源——latest（默认，vision/latest.json）/ 文件名 / vision 目录内路径
        focus: 关注点（可选问题，如「画面里有什么异常」）
        mode: auto（默认；有 LLM 配置则 LLM 否则 heuristic）/ heuristic / llm
        with_image: 配置了 OLLAMA_VISION_MODEL 时把帧 base64 喂视觉模型（可选增强）
        timeout: LLM 调用超时秒数（10~300，默认 60）
        body_context: 身体状态 JSON 字符串（v3.14，双向反馈；如 robot_loop 从
            collab/body/state.json 读取传入；空=无身体上下文，行为与 v3.12 一致）
        vision_dir: 可选，观察目录覆盖（v3.23.0；robot_loop --vision-dir 同时作用于
            采集与分析；空=沿用 COLLAB_VISION_DIR / 默认 collab/vision）
    """
    denied = assert_identity_allowed("analyze_observation")
    if denied:
        return fail(denied)
    timeout = _clamp_int(timeout, 10, 300, 60)
    m = (mode or "auto").strip().lower()
    if m not in ("auto", "heuristic", "llm"):
        return fail(f"未知 mode: {mode}（可选 auto/heuristic/llm）")

    root = Path(vision_dir).resolve() if vision_dir else None
    path = _resolve_observation(source, root=root)
    if path is None:
        return fail(f"观察不存在或路径越界: {source}（仅允许 vision 目录内）")
    obs = _read_observation(path)
    if obs is None:
        return fail(f"观察文件非法或 schema 不匹配: {path.name}")

    obs_id = obs.get("observation_id") or path.stem
    logger.info(f"\U0001f441\ufe0f analyze_observation: id={obs_id} source={obs.get('source')} mode={m} body_context={'有' if body_context else '无'}")

    method = "heuristic"
    llm_backend = ""
    interp, actions, confidence = _heuristic_interpret(obs, body_context)
    configured = _llm_configured()

    if m == "llm":
        if not configured:
            return fail("mode=llm 但未配置 LLM（设 OPENAI_API_KEY 或 OLLAMA_BASE_URL+OLLAMA_MODEL）")
        try:
            interp, actions, confidence, llm_backend = await asyncio.to_thread(
                _llm_analyze, obs, focus, timeout, with_image, body_context, root,
                Path(path).parent)
            method = "llm"
        except RuntimeError as e:
            return fail(str(e))
    elif m == "auto" and configured:
        try:
            interp, actions, confidence, llm_backend = await asyncio.to_thread(
                _llm_analyze, obs, focus, timeout, with_image, body_context, root,
                Path(path).parent)
            method = "llm"
        except RuntimeError as e:
            logger.warning(f"\u26a0\ufe0f LLM 分析失败，降级 heuristic: {e}")
            interp, actions, confidence = _heuristic_interpret(obs, body_context)
            method = "heuristic"

    dets = obs.get("detections") or []
    return ok({
        "observation_id": obs_id,
        "source": obs.get("source"),
        "timestamp": obs.get("timestamp"),
        "stats": obs.get("stats"),
        "detections": dets,
        "detection_count": len(dets),
        "method": method,
        "llm_backend": llm_backend if method == "llm" else "",
        "focus": focus.strip(),
        "interpretation": interp,
        "suggested_actions": actions,
        "confidence": confidence,
        "body_context": body_context,
    })
