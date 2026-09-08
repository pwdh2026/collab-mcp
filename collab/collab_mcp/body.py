"""身体执行模拟器：execute_action（v3.13 眼睛→大脑→身体 闭环 Step 2）。

设计来源：progress/collab-v3.13-body-actuator-design.md
- v3.12 完成眼睛↔大脑（vision_capture 采集 + analyze_observation 分析），真机闭环已达成；
- 本模块把大脑给出的 suggested_actions 接到「身体」执行（纯软件模拟器/演示接口，无真实硬件），
  补上 眼睛→大脑→身体 闭环缺的最后一块。
- 状态持久化到共享 collab/body/state.json（文件驱动、跨机器可见；COLLAB_BODY_DIR 可覆盖，测试隔离）。
- 动作别名归一化（子串匹配，兼容 LLM 自然语言建议）；位置/朝向模拟（heading 决定位移方向）。

安全约定：身份闸门复用；action 必填校验；未知动作明确失败（带可用列表）；原子写；
状态文件损坏/schema/深字段不兼容 → 明确失败（不静默覆盖，状态以文件为准）。

动作匹配语义（PC-C LOW-3 文档化）：_normalize_action 按 _ACTION_ALIASES 字典插入顺序做
子串匹配（含英文小写），固有代价=误命中（如「左右」→turn_left、bright→turn_right、
scanner→observe）；未知动作由 fail+可用列表兜底。取反语义由显式动作处理：如
「关闭补光/关掉补光/关灯」→ light_off（在 light_on 之前匹配），不会反向开灯。
"""

import math
import os
from pathlib import Path

from .identity import assert_identity_allowed
from .logging_setup import logger
from .utils import fail, ok, now_iso, safe_read_json, safe_write_json

_STATE_SCHEMA = "collab-body-state-v1"
_HISTORY_LIMIT = 20
_AMOUNT_MIN = 1
_AMOUNT_MAX = 10

# 动作别名：归一化动作 -> 子串关键词（小写匹配；顺序即优先级）
_ACTION_ALIASES = {
    "forward": ["forward", "move_forward", "move forward", "前进", "向前"],
    "backward": ["backward", "move_back", "move back", "后退", "向后", "倒车"],
    "turn_left": ["turn_left", "turn left", "left", "左转", "向左", "左"],
    "turn_right": ["turn_right", "turn right", "right", "右转", "向右", "右"],
    "stop": ["stop", "halt", "停止", "停下"],
    "light_off": ["light_off", "light off", "关闭补光", "关掉补光", "关灯"],
    "light_on": ["light_on", "illuminate", "light on", "补光", "开灯", "照明"],
    "camera": ["camera", "摄像头", "调整摄像头", "调整角度", "角度", "pan", "tilt"],
    "observe": ["observe", "scan", "观察", "扫描", "查看"],
}


def _body_dir() -> Path:
    """身体状态目录：可用 COLLAB_BODY_DIR 覆盖（测试隔离用），默认 collab/body/。"""
    return Path(os.environ.get("COLLAB_BODY_DIR") or
                (Path(__file__).resolve().parent.parent / "body"))


def _state_path() -> Path:
    return _body_dir() / "state.json"


def _default_state() -> dict:
    return {
        "schema": _STATE_SCHEMA,
        "robot_id": "sim-1",
        "position": {"x": 0, "y": 0},
        "heading_deg": 0,
        "status": "idle",
        "light": False,
        "camera": {"pan_deg": 0, "tilt_deg": 0},
        "last_action": None,
        "last_action_at": None,
        "history": [],
    }


def _load_state() -> "tuple[dict | None, str]":
    """读取身体状态；文件不存在返回默认，损坏/不兼容返回 (None, 错误信息)。"""
    p = _state_path()
    if not p.exists():
        return _default_state(), ""
    data = safe_read_json(p)
    if data is None:
        return None, f"身体状态文件损坏: {p}（可删除该文件重置）"
    if data.get("schema") != _STATE_SCHEMA:
        return None, (f"身体状态 schema 不兼容: {data.get('schema')}（需 {_STATE_SCHEMA}，"
                      "可删除文件重置）")
    err = _validate_state_shape(data)
    if err:
        return None, err
    data.setdefault("history", [])
    return data, ""


def _validate_state_shape(data: dict) -> str:
    """深字段校验（PC-C LOW-4）：顶层 schema 正确但深字段缺失/类型错时干净失败，不崩溃。"""
    pos = data.get("position")
    if (not isinstance(pos, dict)
            or not isinstance(pos.get("x"), (int, float))
            or not isinstance(pos.get("y"), (int, float))):
        return "身体状态 position 字段非法（需 {x: 数字, y: 数字}，可删除文件重置）"
    if not isinstance(data.get("heading_deg"), (int, float)):
        return "身体状态 heading_deg 字段非法（需数字，可删除文件重置）"
    cam = data.get("camera")
    if (not isinstance(cam, dict)
            or not isinstance(cam.get("pan_deg"), (int, float))
            or not isinstance(cam.get("tilt_deg"), (int, float))):
        return "身体状态 camera 字段非法（需 {pan_deg: 数字, tilt_deg: 数字}，可删除文件重置）"
    if not isinstance(data.get("light"), bool):
        return "身体状态 light 字段非法（需布尔，可删除文件重置）"
    return ""


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, v))


def _normalize_action(action: str) -> str:
    """把动作文本归一化为标准动作名；未命中返回 ''。"""
    low = (action or "").strip().lower()
    if not low:
        return ""
    for norm, keys in _ACTION_ALIASES.items():
        for k in keys:
            if k in low:
                return norm
    return ""


def _heading_delta(heading: int) -> "tuple[int, int]":
    """heading（度）→ (dx, dy)；0°=+y，90°=+x，180°=-y，270°=-x。"""
    rad = math.radians(heading % 360)
    return round(math.sin(rad)), round(math.cos(rad))


def _parse_camera_detail(detail: str, amount: int) -> "tuple[int | None, int | None]":
    """从 detail 解析 pan/tilt（如 'pan=30 tilt=-20'）；无 detail 时按 amount 兜底为 pan。"""
    pan = None
    tilt = None
    for token in (detail or "").replace("，", " ").replace(",", " ").split():
        token = token.strip().lower()
        if not token:
            continue
        for name in ("pan", "tilt"):
            if token.startswith(name):
                parts = token.split("=")
                if len(parts) == 2:
                    try:
                        val = int(float(parts[1]))
                    except (ValueError, OverflowError):
                        continue  # 非法/溢出值跳过（PC-C LOW-1：不击穿崩溃）
                    if name == "pan":
                        pan = val
                    else:
                        tilt = val
    if pan is None and tilt is None:
        pan = amount  # 无 detail 时按 amount 转动 pan（简单兜底）
    return pan, tilt


def _record_history(state: dict, action: str, norm: str, summary: str) -> None:
    state["history"].append({
        "action": action,
        "normalized": norm,
        "summary": summary,
        "ts": now_iso(),
        "position": dict(state["position"]),
        "heading_deg": state["heading_deg"],
    })
    if len(state["history"]) > _HISTORY_LIMIT:
        state["history"] = state["history"][-_HISTORY_LIMIT:]


async def execute_action(action: str, amount: int = 1, detail: str = "") -> str:
    """执行一个身体动作（纯软件模拟器/演示接口）。

    接收大脑 analyze_observation 给出的 suggested_actions（或任意动作文本），
    归一化后更新模拟机器人状态（位置/朝向/灯光/摄像头），并记录历史。

    Args:
        action: 动作文本（中文/英文均可；如 '前进'、'turn right'、'调整摄像头角度'）
        amount: 移动/转动步数（钳制 1..10，默认 1）
        detail: 可选补充（摄像头动作可带 'pan=30 tilt=-20'）
    """
    denied = assert_identity_allowed("execute_action")
    if denied:
        return fail(denied)

    action = (action or "").strip()
    if not action:
        return fail("execute_action 需要非空 action")

    amount = _clamp_int(amount, _AMOUNT_MIN, _AMOUNT_MAX, 1)

    norm = _normalize_action(action)
    if not norm:
        available = "、".join(sorted(_ACTION_ALIASES))
        return fail(f"无法识别动作「{action}」（可用动作：{available}）")

    state, err = _load_state()
    if state is None:
        return fail(err)

    summary = ""
    if norm in ("forward", "backward"):
        dx, dy = _heading_delta(state["heading_deg"])
        if norm == "backward":
            dx, dy = -dx, -dy
        state["position"]["x"] += dx * amount
        state["position"]["y"] += dy * amount
        state["status"] = "moving"
        label = "前进" if norm == "forward" else "后退"
        summary = (f"{label} {amount} 步 → 位置 ({state['position']['x']}, "
                   f"{state['position']['y']})，朝向 {state['heading_deg']}°")
    elif norm == "turn_left":
        state["heading_deg"] = (state["heading_deg"] - 90 * amount) % 360
        state["status"] = "turning"
        summary = f"左转 {90 * amount}° → 朝向 {state['heading_deg']}°"
    elif norm == "turn_right":
        state["heading_deg"] = (state["heading_deg"] + 90 * amount) % 360
        state["status"] = "turning"
        summary = f"右转 {90 * amount}° → 朝向 {state['heading_deg']}°"
    elif norm == "stop":
        state["status"] = "idle"
        summary = "停止（status=idle）"
    elif norm == "light_off":
        state["light"] = False
        summary = "补光关闭（light=false）"
    elif norm == "light_on":
        state["light"] = True
        summary = "补光开启（light=true）"
    elif norm == "camera":
        pan, tilt = _parse_camera_detail(detail, amount)
        if pan is not None:
            state["camera"]["pan_deg"] = (state["camera"]["pan_deg"] + pan) % 360
        if tilt is not None:
            state["camera"]["tilt_deg"] = max(-90, min(90, state["camera"]["tilt_deg"] + tilt))
        state["status"] = "scanning"
        parsed = ""
        if pan is not None or tilt is not None:
            parsed = f"（detail 解析: pan={pan}, tilt={tilt}）"
        summary = (f"摄像头调整 → pan {state['camera']['pan_deg']}°，"
                   f"tilt {state['camera']['tilt_deg']}°{parsed}")
    elif norm == "observe":
        state["status"] = "observing"
        summary = (f"观察当前状态：位置 ({state['position']['x']}, {state['position']['y']})，"
                   f"朝向 {state['heading_deg']}°，灯光 {'开' if state['light'] else '关'}，"
                   f"摄像头 pan {state['camera']['pan_deg']}°/tilt {state['camera']['tilt_deg']}°")

    state["last_action"] = norm
    state["last_action_at"] = now_iso()
    _record_history(state, action, norm, summary)

    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not safe_write_json(p, state):
        return fail(f"无法写入身体状态: {p}")

    logger.info(f"🦾 execute_action: {norm}（{action[:40]}）→ {summary[:80]}")
    return ok({
        "message": f"动作已执行: {summary}",
        "action": action,
        "normalized": norm,
        "summary": summary,
        "state": state,
        "history_count": len(state["history"]),
    })
