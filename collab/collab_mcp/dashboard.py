r"""静态只读看板生成器 — P2-2（MAF DevUI 的降级版）。

纯 stdlib 生成单文件 HTML（无 CDN、离线可用），读取 inbox/done/chat/feed/teammates，
不依赖后端服务。既可被 MCP 工具 generate_dashboard 调用，也可用 CLI：
    python collab/dashboard.py [--out C:\myshare\dashboard.html]
"""

import html
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .config import CHAT_DIR, COLLAB_DIR, DONE_DIR, INBOX_DIR
from .logging_setup import logger
from .utils import fail, now_iso, ok, safe_read_json

_CSS = """
body { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; margin: 24px; color: #1f2937; background: #f9fafb; }
h1 { font-size: 20px; margin-bottom: 4px; }
h2 { font-size: 16px; margin-top: 28px; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 16px; min-width: 130px; }
.card b { display: block; font-size: 22px; }
.card span { color: #6b7280; font-size: 12px; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; font-size: 13px; vertical-align: top; }
th { background: #f3f4f6; }
.status-in_progress { color: #b45309; font-weight: 600; }
.status-needs_review { color: #7c3aed; font-weight: 600; }
.status-done { color: #047857; }
.status-running { color: #1d4ed8; font-weight: 600; }
.status-completed { color: #047857; font-weight: 600; }
.status-failed { color: #b91c1c; font-weight: 600; }
.status-skipped { color: #6b7280; }
.status-blocked { color: #9ca3af; }
.status-pending { color: #1d4ed8; }
.pipes { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.pipe-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 14px; max-width: 560px; }
.chip { display: inline-block; background: #f3f4f6; border-radius: 10px; padding: 2px 8px; font-size: 12px; margin: 3px 4px 0 0; }
.muted { color: #6b7280; font-size: 12px; }
.dag-wrap { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; margin: 12px 0; overflow-x: auto; }
.dag-legend { font-size: 12px; color: #4b5563; margin-bottom: 6px; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin: 0 4px 0 12px; vertical-align: -1px; border: 1px solid #d1d5db; }
.loop-wrap { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; margin: 12px 0; }
.loop-item { margin: 8px 0; padding: 8px 10px; background: #f9fafb; border-radius: 6px; }
.loop-hist { margin: 6px 0 0 18px; padding: 0; font-size: 13px; }
.trend-wrap { display: flex; gap: 16px; flex-wrap: wrap; margin: 12px 0; }
.trend-chart { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; flex: 1 1 340px; max-width: 780px; }
.trend-chart b { font-size: 13px; display: block; margin-bottom: 6px; }
"""


def _parse_iso(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


def _count_dir(d: Path) -> int:
    try:
        return len(list(d.glob("*.json")))
    except OSError:
        return 0


def _load_teammates() -> list[dict]:
    registry_file = COLLAB_DIR / "teammates.json"
    if not registry_file.exists():
        return []
    registry = safe_read_json(registry_file) or {}
    return list(registry.values())


def _tail_feed(limit: int = 20) -> list[dict]:
    feed = COLLAB_DIR / "notifications" / "feed.jsonl"
    if not feed.exists():
        return []
    import json

    events = []
    try:
        with open(feed, encoding="utf-8") as f:
            for line in f.readlines()[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


_STATUS_FILL = {
    "pending": "#dbeafe",
    "blocked": "#e5e7eb",
    "in_progress": "#fde68a",
    "needs_review": "#ede9fe",
    "done": "#d1fae5",
    "completed": "#d1fae5",
    "failed": "#fee2e2",
    "skipped": "#f3f4f6",
}
_STATUS_STROKE = {
    "failed": "#b91c1c",
    "needs_review": "#7c3aed",
    "in_progress": "#b45309",
}
_DEFAULT_STROKE = "#9ca3af"
_NODE_W, _NODE_H, _GAP_X, _GAP_Y = 150, 44, 70, 30
_MAX_LABEL = 14


def _dag_status_colors(status: str) -> tuple[str, str]:
    fill = _STATUS_FILL.get(status or "", _STATUS_FILL["pending"])
    stroke = _STATUS_STROKE.get(status or "", _DEFAULT_STROKE)
    return fill, stroke


def _dag_legend() -> str:
    """状态图例（颜色 swatch + 状态名）。"""
    order = ["pending", "blocked", "in_progress", "needs_review", "done", "failed", "skipped"]
    parts = ["<b>图例：</b>"]
    for st in order:
        fill, _ = _dag_status_colors(st)
        parts.append(
            f'<span class="swatch" style="background:{fill}"></span>{_esc(st)}'
        )
    return "".join(parts)


def _render_pipeline_dag(steps: list[dict], pipeline_id: str) -> str:
    """把流水线步骤渲染为内联 SVG 节点图（v2.3.0）。

    - 节点按依赖层（rank）排布：无依赖为第 0 层，rank = max(依赖 rank) + 1；
      同层并行步骤横向排列，层数纵向推进（依赖方向 → 右）
    - 边为 depends_on 关系，带箭头曲线，附 data-dep="from->to" 便于测试/脚本读取
    - 节点按状态着色，悬停 title 显示 步骤名/状态/模板/任务 id
    - 纯读生成，不依赖任何前端库
    """
    # M1（PC-B 验证）：过滤缺 id 的坏步骤，避免 pos 索引 KeyError 拖垮整板
    steps = [s for s in steps if s.get("id")]
    if not steps:
        return ""
    by_id = {s.get("id"): s for s in steps}

    # 依赖分层（L1：visiting 集合做真正的循环检测，深度上限仅兜底）
    rank: dict[str, int] = {}
    visiting: set[str] = set()

    def _rank(s: dict, _depth: int = 0) -> int:
        sid = s.get("id")
        if sid in rank:
            return rank[sid]
        if _depth > 200 or sid in visiting:
            rank[sid] = 0
            return 0
        visiting.add(sid)
        deps = [d for d in (s.get("depends_on") or []) if d in by_id]
        if not deps:
            r = 0
        else:
            r = max(_rank(by_id[d], _depth + 1) for d in deps) + 1
        rank[sid] = r
        visiting.discard(sid)
        return r

    for s in steps:
        _rank(s)

    layers: dict[int, list[dict]] = {}
    for s in steps:
        layers.setdefault(rank.get(s.get("id"), 0), []).append(s)
    max_per_layer = max((len(v) for v in layers.values()), default=1)

    pos: dict[str, tuple[int, int]] = {}
    for layer, items in layers.items():
        offset = (max_per_layer - len(items)) / 2
        for i, s in enumerate(items):
            pos[s.get("id")] = (
                layer * (_NODE_W + _GAP_X),
                int((i + offset) * (_NODE_H + _GAP_Y)),
            )

    width = (max(layers) + 1) * (_NODE_W + _GAP_X) + 30
    height = max_per_layer * (_NODE_H + _GAP_Y) + 30
    marker_id = f"arrow_{pipeline_id}"

    edges = []
    for s in steps:
        sid = s.get("id")
        for dep in (s.get("depends_on") or []):
            if dep not in pos:
                continue
            fx, fy = pos[dep][0] + _NODE_W, pos[dep][1] + _NODE_H // 2
            tx, ty = pos[sid][0], pos[sid][1] + _NODE_H // 2
            mid = (fx + tx) / 2
            edges.append(
                f'<path class="dag-edge" data-dep="{_esc(dep)}->{_esc(sid)}" '
                f'd="M {fx},{fy} C {mid},{fy} {mid},{ty} {tx},{ty}" '
                f'fill="none" stroke="#94a3b8" stroke-width="1.5" '
                f'marker-end="url(#{marker_id})"/>'
            )

    nodes = []
    for s in steps:
        sid = s.get("id")
        x, y = pos[sid]
        status = s.get("status", "pending") or "pending"
        fill, stroke = _dag_status_colors(status)
        raw_label = str(s.get("step_name") or s.get("title") or sid)
        # L2（PC-B 验证）：先按原始文本截断，再转义，避免截断点落在实体中间
        short = raw_label if len(raw_label) <= _MAX_LABEL else raw_label[:_MAX_LABEL - 1] + "…"
        label = _esc(short)
        tip = _esc(
            f"{raw_label} | {status} | {s.get('template') or '-'} | {sid}"
        )
        nodes.append(
            f'<g class="dag-node" data-task="{_esc(sid)}" data-status="{_esc(status)}">'
            f"<title>{tip}</title>"
            f'<rect x="{x}" y="{y}" width="{_NODE_W}" height="{_NODE_H}" rx="8" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + _NODE_W / 2}" y="{y + 19}" text-anchor="middle" '
            f'font-size="12" font-weight="600" fill="#1f2937">{short}</text>'
            f'<text x="{x + _NODE_W / 2}" y="{y + 35}" text-anchor="middle" '
            f'font-size="10" fill="#6b7280">{_esc(status)}</text>'
            "</g>"
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="流水线 DAG {_esc(pipeline_id)}">'
        f'<defs><marker id="{marker_id}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"/></marker></defs>'
        + "".join(edges)
        + "".join(nodes)
        + "</svg>"
    )


def _loop_state_html() -> str:
    """眼睛→大脑→身体 闭环状态（v3.19 看板可视化）：latest 观察 + 身体状态 + 动作史。"""
    blocks: list[str] = []
    vision_json = Path(COLLAB_DIR) / "vision" / "latest.json"
    obs = safe_read_json(vision_json) if vision_json.exists() else None
    if obs:
        st = obs.get("stats") or {}
        dc = st.get("dominant_colors") or []

        def _pct(share):
            try:
                return round(float(share) * 100)
            except (TypeError, ValueError):
                return 0

        dc_str = ", ".join(
            f"{c.get('color', '')}({_pct(c.get('share', 0))}%)"
            for c in dc[:2] if isinstance(c, dict)
        )
        blocks.append(
            f'<div class="loop-item"><b>👁️ 最新观察</b> '
            f'<span class="muted">{_esc(obs.get("observation_id", ""))} ｜ '
            f'{_esc(obs.get("timestamp", ""))}</span><br>'
            f'亮度 {st.get("brightness", "-")} ｜ 饱和度 {st.get("sat_mean", "-")} ｜ '
            f'边缘 {st.get("edge_density", "-")} ｜ 主色 {st.get("dominant_color", "-")}'
            f'<br><span class="muted">色桶: {_esc(dc_str) or "-"}</span></div>'
        )
    body_json = Path(COLLAB_DIR) / "body" / "state.json"
    state = safe_read_json(body_json) if body_json.exists() else None
    if state:
        pos = state.get("position") or {}
        cam = state.get("camera") or {}
        light = "开" if state.get("light") else "关"
        blocks.append(
            f'<div class="loop-item"><b>🦾 身体状态</b> '
            f'<span class="muted">位置 ({pos.get("x", 0)}, {pos.get("y", 0)}) ｜ '
            f'朝向 {state.get("heading_deg", 0)}° ｜ 灯光 {light} ｜ '
            f'摄像头 pan {cam.get("pan_deg", 0)}°/tilt {cam.get("tilt_deg", 0)}°</span><br>'
            f'最近动作：{_esc(state.get("last_action", "-"))} '
            f'<span class="muted">（{_esc(state.get("last_action_at", ""))}）</span></div>'
        )
        hist = state.get("history") or []
        if hist:
            items = "".join(
                f'<li>{_esc(h.get("at", ""))} → <b>{_esc(h.get("norm", h.get("action", "")))}</b>'
                f'<span class="muted">：{_esc(h.get("summary", ""))}</span></li>'
                for h in hist[-8:][::-1]
            )
            blocks.append(
                f'<div class="loop-item"><b>🔄 动作史（最近 {min(len(hist), 8)}）</b>'
                f'<ul class="loop-hist">{items}</ul></div>'
            )
    if not blocks:
        return '<div class="muted">暂无闭环数据（vision/body 尚未采集）</div>'
    return "".join(blocks)


def _within_days(t: dict, days: int, field: str = "completed_at") -> bool:
    """判断记录时间是否在 N 天内（字段缺失/无法解析返回 False）。"""
    dt = _parse_iso(t.get(field))
    if not dt:
        return False
    return (datetime.now().astimezone() - dt).total_seconds() <= days * 86400


def _now():
    """本地时区的当前时间（统一入口，便于测试 mock）。"""
    return datetime.now().astimezone()


def _is_today(t: dict, field: str = "completed_at") -> bool:
    """按自然日判断是否为今天（PC-C 中1：days=0 语义=0 秒窗口，改用日期边界）。"""
    dt = _parse_iso(t.get(field))
    if not dt:
        return False
    # v3.23.0 修复：dt 可能是 UTC 时间戳（now_iso），必须转本地时区再取日期，
    # 否则本地 00:00-08:00 窗口（UTC 仍是昨天）会把"刚完成"误判为昨天
    # v3.23.1 修复：统一以 _now() 的时区为目标（单一事实源），
    # 避免裸 astimezone() 依赖系统时区导致 CI（UTC）与本机（+08:00）行为不一致
    now = _now()
    return dt.astimezone(now.tzinfo).date() == now.date()


def _local_date(dt) -> "datetime.date":
    """任意 aware/naive 时间 → 本地自然日（以 _now() 的时区为目标，v3.23.1 同源）。"""
    return dt.astimezone(_now().tzinfo).date()


def _day_series(done: list[dict], days: int = 14, field: str = "completed_at") -> list[dict]:
    """近 N 个本地自然日的吞吐/周期序列（v3.24 看板趋势）。

    返回从旧到新的 [{label, date, count, avg_cycle_min}]；无数据日为
    count=0 / avg_cycle_min=None。分桶键=本地日期（_local_date），
    与「今日完成」判定同源——UTC 凌晨完成的任务计入本地当天。
    """
    today = _now().date()
    buckets: dict[datetime.date, dict] = {}
    labels: list[datetime.date] = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        labels.append(d)
        buckets[d] = {"count": 0, "cycles": []}
    for t in done:
        dt = _parse_iso(t.get(field))
        if not dt:
            continue
        d = _local_date(dt)
        b = buckets.get(d)
        if b is None:
            continue
        b["count"] += 1
        created = _parse_iso(t.get("created_at"))
        if created:
            # LOW-1（PC-C v3.24 闸门）：created_at 晚于 completed_at 的脏数据
            # 会产生负周期，SVG 点会绘到绘图区上界之外；钳到 0 兜底
            b["cycles"].append(max(0.0, (dt - created).total_seconds() / 60.0))
    return [
        {
            "label": d.strftime("%m-%d"),
            "date": d.isoformat(),
            "count": buckets[d]["count"],
            "avg_cycle_min": (
                round(sum(buckets[d]["cycles"]) / len(buckets[d]["cycles"]), 1)
                if buckets[d]["cycles"] else None
            ),
        }
        for d in labels
    ]


_TREND_W, _TREND_H = 720, 170
_TREND_PAD_L, _TREND_PAD_R, _TREND_PAD_T, _TREND_PAD_B = 8, 8, 18, 24


def _render_trend_svg(series: list[dict], kind: str = "count") -> str:
    """内联 SVG 趋势图（v3.24）：count=每日完成数柱状 / cycle=每日平均周期折线。

    纯 stdlib、无前端依赖；元素带 data-day/data-value 便于测试与脚本读取；
    全零/空序列安全（不除零、不崩溃）。"""
    n = len(series)
    plot_w = _TREND_W - _TREND_PAD_L - _TREND_PAD_R
    plot_h = _TREND_H - _TREND_PAD_T - _TREND_PAD_B
    slot = plot_w / max(n, 1)
    bar_w = max(slot * 0.62, 2.0)
    if kind == "cycle":
        values = [s.get("avg_cycle_min") for s in series]
        title = "每日平均周期(分)"
    else:
        values = [s.get("count", 0) for s in series]
        title = "每日完成数"
    scale = max((v for v in values if v is not None), default=0.0)
    if scale <= 0:
        scale = 1.0

    def _y(v: float) -> float:
        return _TREND_PAD_T + plot_h - (v / scale) * plot_h

    parts: list[str] = []
    for i, s in enumerate(series):
        x = _TREND_PAD_L + i * slot
        if kind == "cycle":
            v = s.get("avg_cycle_min")
            if v is None:
                continue
            parts.append(
                f'<circle class="trend-dot" cx="{x + bar_w / 2:.1f}" cy="{_y(v):.1f}" r="3" '
                f'fill="#f59e0b" data-day="{_esc(s["date"])}" data-value="{v}">'
                f'<title>{_esc(s["label"])} 平均 {v} 分</title></circle>'
            )
        else:
            v = s.get("count", 0)
            h = (v / scale) * plot_h
            parts.append(
                f'<rect class="trend-bar" x="{x:.1f}" y="{_y(v):.1f}" width="{bar_w:.1f}" '
                f'height="{h:.1f}" rx="2" fill="#3b82f6" data-day="{_esc(s["date"])}" '
                f'data-value="{v}"><title>{_esc(s["label"])} 完成 {v}</title></rect>'
            )
        if i % 2 == 0 or i == n - 1:
            parts.append(
                f'<text x="{x + slot / 2:.1f}" y="{_TREND_H - 6}" text-anchor="middle" '
                f'font-size="9" fill="#6b7280">{_esc(s["label"])}</text>'
            )
    if kind == "cycle":
        pts = [
            f"{_TREND_PAD_L + i * slot + bar_w / 2:.1f},{_y(s['avg_cycle_min']):.1f}"
            for i, s in enumerate(series)
            if s.get("avg_cycle_min") is not None
        ]
        if len(pts) >= 2:
            parts.insert(
                0,
                f'<polyline class="trend-line" points="{" ".join(pts)}" '
                f'fill="none" stroke="#f59e0b" stroke-width="2"/>',
            )
    if not any(v is not None and v > 0 for v in values):
        parts.append(
            f'<text x="{_TREND_W / 2}" y="{_TREND_PAD_T + plot_h / 2}" text-anchor="middle" '
            f'font-size="12" fill="#9ca3af">暂无数据</text>'
        )
    return (
        f'<svg viewBox="0 0 {_TREND_W} {_TREND_H}" width="100%" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{_esc(title)}">'
        + "".join(parts)
        + "</svg>"
    )


def _trend_html(done: list[dict], days: int = 14) -> str:
    """近 N 日趋势区块（v3.24）：完成数柱状 + 平均周期折线。"""
    series = _day_series(done, days)
    total = sum(s["count"] for s in series)
    return (
        '<div class="trend-wrap">'
        f'<div class="trend-chart"><b>每日完成数（近 {days} 日，合计 {total}）</b>'
        f'{_render_trend_svg(series, "count")}</div>'
        f'<div class="trend-chart"><b>每日平均周期（分钟）</b>'
        f'{_render_trend_svg(series, "cycle")}</div>'
        "</div>"
    )


def _metrics_html() -> str:
    """平台指标（v3.20）：今日/近7日吞吐、近7日平均周期、活跃队友、feed 事件分布。"""
    done = []
    for f in DONE_DIR.glob("*.json"):
        t = safe_read_json(f)
        if t:
            done.append(t)
    week = [t for t in done if _within_days(t, 7)]
    cycles = []
    for t in week:
        created = _parse_iso(t.get("created_at"))
        done_at = _parse_iso(t.get("completed_at"))
        if created and done_at:
            cycles.append((done_at - created).total_seconds() / 60.0)
    avg_cycle_week = round(sum(cycles) / len(cycles), 1) if cycles else "-"
    today = len([t for t in week if _is_today(t)])
    mates = [t.get("name") for t in _load_teammates() if _within_days(t, 1, "last_seen")]
    dist: dict = {}
    for e in _tail_feed(50):
        k = str(e.get("type", ""))
        dist[k] = dist.get(k, 0) + 1
    dist_str = "、".join(f"{_esc(k)}×{n}" for k, n in sorted(dist.items())) or "-"
    mate_str = "、".join(_esc(m) for m in mates) or "-"
    return (
        '<div class="cards">'
        f'<div class="card"><b>{today}</b><span>今日完成</span></div>'
        f'<div class="card"><b>{len(week)}</b><span>近7日完成</span></div>'
        f'<div class="card"><b>{avg_cycle_week}</b><span>近7日平均周期(分)</span></div>'
        f'<div class="card"><b>{len(mates)}</b><span>24h活跃队友</span></div>'
        "</div>"
        f'<div class="loop-wrap">事件分布（近50条）：{dist_str}<br>活跃队友：{mate_str}</div>'
    )


def render_dashboard_html() -> str:
    """渲染单文件看板 HTML（纯读，不写任何平台状态）。"""
    pending_tasks = []
    for f in sorted(INBOX_DIR.glob("*.json")):
        t = safe_read_json(f)
        if t:
            pending_tasks.append(t)

    completed = []
    for f in sorted(DONE_DIR.glob("*.json")):
        t = safe_read_json(f)
        if t:
            completed.append(t)

    cycles = []
    for t in completed:
        created = _parse_iso(t.get("created_at"))
        done_at = _parse_iso(t.get("completed_at"))
        if created and done_at:
            cycles.append((done_at - created).total_seconds() / 60.0)
    avg_cycle = round(sum(cycles) / len(cycles), 1) if cycles else None

    chat_count = _count_dir(CHAT_DIR)
    teammates = _load_teammates()
    feed = _tail_feed(20)
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    # v1.9.2：流水线分组（inbox + done 按 pipeline_id 聚合）
    from .tasks import aggregate_pipeline

    pipelines: dict[str, list[dict]] = {}
    for f in list(INBOX_DIR.glob("*.json")) + list(DONE_DIR.glob("*.json")):
        t = safe_read_json(f)
        if t and t.get("pipeline_id"):
            pipelines.setdefault(t["pipeline_id"], []).append(t)
    pipeline_sections = []
    dag_sections = []
    for pid in sorted(pipelines):
        agg = aggregate_pipeline(pid)
        chips = "".join(
            f'<span class="chip chip-{_esc(s["status"])}">{_esc(s["step_name"])} · {_esc(s["status"])}</span> '
            for s in agg["steps"]
        )
        name = agg["steps"][0].get("pipeline_name", "") if agg["steps"] else ""
        pipeline_sections.append(
            f'<div class="pipe-card"><b>{_esc(name)}'
            f' <span class="status-{_esc(agg["status"])}">{_esc(agg["status"])}</span></b>'
            f'<div class="muted">{_esc(pid[:8])} ｜ 完成 {agg["done_steps"]}/{agg["total_steps"]}'
            f'（失败 {agg["failed_steps"]} / 跳过 {agg["skipped_steps"]} / 复核中 {agg["in_review_steps"]}）</div>'
            f"<div>{chips}</div></div>"
        )
        svg = _render_pipeline_dag(pipelines[pid], pid)
        if svg:
            dag_sections.append(f'<div class="dag-wrap">{svg}</div>')

    pending_rows = [
        [
            _esc(t.get("id")),
            _esc(t.get("title")),
            f'<span class="status-{_esc(t.get("status", "pending"))}">{_esc(t.get("status", "pending"))}</span>',
            _esc(t.get("assignee", "any")),
            _esc(t.get("claimed_by", "")),
            _esc(t.get("template", "")),
            _esc(t.get("deadline", "")),
        ]
        for t in pending_tasks
    ]
    done_rows = []
    for t in completed[-10:][::-1]:
        created = _parse_iso(t.get("created_at"))
        done_at = _parse_iso(t.get("completed_at"))
        cycle = (
            round((done_at - created).total_seconds() / 60.0, 1)
            if created and done_at
            else "-"
        )
        done_rows.append([
            _esc(t.get("id")),
            _esc(t.get("title")),
            _esc(t.get("completed_by", "?")),
            _esc(cycle),
            _esc((t.get("evidence") or "")[:60]),
        ])
    teammate_rows = [
        [
            _esc(t.get("name")),
            _esc(", ".join(t.get("skills", []) or [])),
            _esc(t.get("capabilities", "")),
            _esc(t.get("last_seen", "")),
        ]
        for t in teammates
    ]
    feed_rows = [
        [_esc(e.get("ts", "")), _esc(e.get("type", "")), _esc(e.get("title") or e.get("sender") or e.get("task_id", ""))]
        for e in feed
    ]
    if dag_sections:
        dag_html = (
            f'<div class="dag-legend">{_dag_legend()}</div>'
            + "".join(dag_sections)
        )
    else:
        dag_html = '<div class="muted">暂无流水线</div>'

    return (
        "<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"60\">"
        f"<title>Claude 协作看板 v{__version__}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>Claude 协作看板</h1>"
        f'<div class="muted">生成时间：{generated} ｜ 平台版本：v{__version__} ｜ '
        f"协作目录：{_esc(COLLAB_DIR)}</div>"
        '<div class="cards">'
        f'<div class="card"><b>{len(pending_tasks)}</b><span>待办任务</span></div>'
        f'<div class="card"><b>{len(completed)}</b><span>已完成</span></div>'
        f'<div class="card"><b>{chat_count}</b><span>聊天消息</span></div>'
        f'<div class="card"><b>{avg_cycle if avg_cycle is not None else "-"}</b><span>平均周期(分)</span></div>'
        f'<div class="card"><b>{len(teammates)}</b><span>队友</span></div>'
        "</div>"
        + "<h2>平台指标</h2>"
        + _metrics_html()
        + "<h2>近 14 日趋势</h2>"
        + _trend_html(completed)
        + "<h2>闭环状态（眼睛→大脑→身体）</h2>"
        + '<div class="loop-wrap">' + _loop_state_html() + "</div>"
        + "<h2>流水线</h2>"
        + (
            '<div class="pipes">' + "".join(pipeline_sections) + "</div>"
            if pipeline_sections
            else '<div class="muted">暂无流水线</div>'
        )
        + "<h2>流水线 DAG</h2>"
        + dag_html
        + "<h2>待办任务</h2>"
        + (
            _table(
                ["ID", "标题", "状态", "指派", "认领者", "模板", "截止"],
                pending_rows,
            )
            if pending_rows
            else '<div class="muted">暂无待办</div>'
        )
        + "<h2>最近完成</h2>"
        + (
            _table(
                ["ID", "标题", "完成者", "周期(分)", "evidence 摘要"],
                done_rows,
            )
            if done_rows
            else '<div class="muted">暂无已完成任务</div>'
        )
        + "<h2>队友</h2>"
        + (
            _table(
                ["名称", "能力标签", "描述", "最近活跃"],
                teammate_rows,
            )
            if teammate_rows
            else '<div class="muted">暂无注册队友</div>'
        )
        + "<h2>事件流（最近 20 条）</h2>"
        + (
            _table(["时间", "类型", "内容"], feed_rows)
            if feed_rows
            else '<div class="muted">暂无事件</div>'
        )
        + "</body></html>"
    )


async def generate_dashboard(out_path: str = "") -> str:
    """生成静态只读看板 HTML（P2-2）。

    Args:
        out_path: 可选输出路径；留空写 COLLAB_DIR 上一级的 dashboard.html。
            相对路径基于共享文件夹根解析。
    """
    html_text = render_dashboard_html()
    target = Path(out_path) if out_path else COLLAB_DIR.parent / "dashboard.html"
    if not target.is_absolute():
        target = COLLAB_DIR.parent / target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_text, encoding="utf-8")
    except OSError as e:
        return fail(f"写入看板失败: {e}")
    logger.info(f"📊 看板已生成: {target}")
    return ok({
        "path": str(target),
        "bytes": len(html_text.encode("utf-8")),
        "generated_at": now_iso(),
    })


async def dashboard_data() -> str:
    """返回看板核心指标的 JSON 数据（供程序化轮询/监控使用）。

    v3.27.0: 与 generate_dashboard 互补——generate_dashboard 输出 HTML 文件，
    此工具返回结构化 JSON，适合 agent 定期调用或接入监控系统。
    """
    pending_tasks = []
    completed = []
    try:
        for f in sorted(INBOX_DIR.glob("*.json")):
            t = safe_read_json(f)
            if t:
                pending_tasks.append(t)
        for f in sorted(DONE_DIR.glob("*.json")):
            t = safe_read_json(f)
            if t:
                completed.append(t)
    except OSError:
        pass
    chat_count = _count_dir(CHAT_DIR)
    teammates = _load_teammates()

    # 简要周期计算
    cycles = []
    for t in completed:
        created = _parse_iso(t.get("created_at"))
        done_at = _parse_iso(t.get("completed_at"))
        if created and done_at:
            cycles.append((done_at - created).total_seconds() / 60.0)
    avg_cycle = round(sum(cycles) / len(cycles), 1) if cycles else None

    return ok({
        "version": __version__,
        "generated_at": now_iso(),
        "pending_count": len(pending_tasks),
        "completed_count": len(completed),
        "chat_messages": chat_count,
        "teammates": len(teammates),
        "avg_cycle_minutes": avg_cycle,
        "pending_summary": [
            {"id": t.get("id"), "title": t.get("title"), "status": t.get("status"),
             "assignee": t.get("assignee"), "claimed_by": t.get("claimed_by")}
            for t in pending_tasks[:10]
        ],
        "recent_completed": [
            {"id": t.get("id"), "title": t.get("title"),
             "completed_by": t.get("completed_by"), "completed_at": t.get("completed_at")}
            for t in sorted(
                (t for t in completed if t.get("completed_at")),
                key=lambda t: t["completed_at"], reverse=True,
            )[:5]
        ],
    })
