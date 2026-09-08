"""闭环循环 demo：眼睛→大脑→身体 连续闭环（v3.14 Step 3；v3.23.0 打磨）。

设计来源：progress/collab-v3.14-continuous-loop-design.md
每轮：①（可选 --capture）调 vision_capture.py（--demo/--camera）刷新 latest 观察 →
② 读身体状态 collab/body/state.json 得 body_context → ③ 大脑 analyze_observation(body_context=...) →
④ 取第一条 suggested_action → 身体 execute_action → ⑤ 打印轮摘要。Ctrl+C 优雅退出。

纯脚本直调 collab_mcp 模块（与测试同法，不开 MCP stdio）；零新依赖。
身体上下文回传 = 大脑-身体双向反馈：第二轮起大脑能看到身体已执行的朝向/灯光/位置。

v3.23.0 打磨（可观测性，纯软件）：
- 每轮新增 capture_ok（--capture 时采集成败）与 elapsed_ms（轮级耗时）。
- 汇总新增 stats 块：分析成功率 / 动作执行率 / actions_by_name 聚合 / 采集失败数 / 错误清单。
- 新增 --stream-json：每轮一行 JSON + 末尾一行汇总 JSON（自动化/看板可直接消费）。
- 新增预检：无 latest 观察且未 --capture 时启动即提示（不中断）。
- --json / --stream-json 时自动关 verbose，保证 stdout 是纯净 JSON 行。

用法（PowerShell 直跑）：
    python collab/scripts/robot_loop.py --rounds 5 --interval 10 --demo
    python collab/scripts/robot_loop.py --rounds 20 --interval 5 --capture --camera 0 --mode llm
    python collab/scripts/robot_loop.py --rounds 3 --interval 2 --demo --json
    python collab/scripts/robot_loop.py --rounds 3 --interval 2 --demo --stream-json
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 允许测试 patch 睡眠而不真等
_sleep = asyncio.sleep

ROOT = Path(__file__).resolve().parent.parent  # collab/
sys.path.insert(0, str(ROOT))  # 允许直调 collab_mcp 模块

VISION_CAPTURE = ROOT / "scripts" / "vision_capture.py"


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _read_body_context(body_dir: str = "") -> str:
    """读身体状态 JSON 字符串；文件不存在/损坏返回 ''（首次运行尚无状态，降级无上下文）。"""
    d = Path(body_dir or os.environ.get("COLLAB_BODY_DIR") or (ROOT / "body"))
    p = d / "state.json"
    try:
        return p.read_text(encoding="utf-8").strip()
    except (IOError, OSError):
        return ""


def _refresh_observation(demo: bool, camera, vision_dir: str, rounds_so_far: int) -> bool:
    """调 vision_capture.py 刷新 latest 观察（--demo 或 --camera）。失败返回 False（不中断循环）。"""
    cmd = [sys.executable, str(VISION_CAPTURE)]
    if demo:
        cmd += ["--demo"]
    elif camera is not None:
        cmd += ["--camera", str(camera)]
    else:
        cmd += ["--demo"]  # 无明确来源时 demo 兜底（可离线演示）
    cmd += ["--out-dir", vision_dir or str(ROOT / "vision"), "--json"]
    try:
        # Windows 管道下子进程 stdout 默认按 GBK 编码 → 强制 UTF-8，避免解码崩溃（平台约定）
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[round {rounds_so_far}] 采集失败（降级继续）: {e}", file=sys.stderr)
        return False
    if p.returncode != 0:
        print(f"[round {rounds_so_far}] vision_capture 退出码 {p.returncode}: {p.stderr.strip()}",
              file=sys.stderr)
        return False
    return True


async def _run_round(round_no: int, *, capture: bool, demo: bool, camera,
                     mode: str, vision_dir: str, body_dir: str) -> dict:
    """跑一轮：刷新观察（可选）→ 读身体状态 → 大脑分析 → 身体执行。"""
    from collab_mcp import body, vision

    t0 = time.perf_counter()
    capture_ok = None
    if capture:
        capture_ok = _refresh_observation(demo, camera, vision_dir, round_no)

    body_context = _read_body_context(body_dir)
    ana_raw = await vision.analyze_observation(
        source="latest", body_context=body_context, mode=mode, vision_dir=vision_dir)
    try:
        ana = json.loads(ana_raw)
    except (ValueError, TypeError):
        ana = {"success": False, "error": f"大脑分析响应非法: {str(ana_raw)[:120]}"}

    if not ana.get("success"):
        return {
            "round": round_no, "capture_ok": capture_ok,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "analysis_ok": False, "error": ana.get("error", "未知错误"),
            "action": None, "action_ok": None,
        }

    suggestion = (ana.get("suggested_actions") or [None])[0]
    if not suggestion:
        return {
            "round": round_no, "capture_ok": capture_ok,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "analysis_ok": True, "observation_id": ana.get("observation_id"),
            "interpretation": ana.get("interpretation"),
            "suggestion": None, "action_ok": None,
        }

    exe_raw = await body.execute_action(action=suggestion)
    try:
        exe = json.loads(exe_raw)
    except (ValueError, TypeError):
        exe = {"success": False, "error": f"身体执行响应非法: {str(exe_raw)[:120]}"}
    return {
        "round": round_no,
        "capture_ok": capture_ok,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "analysis_ok": True,
        "observation_id": ana.get("observation_id"),
        "interpretation": ana.get("interpretation"),
        "confidence": ana.get("confidence"),
        "suggestion": suggestion,
        "action_ok": bool(exe.get("success")),
        "action_summary": exe.get("summary") or exe.get("error") or "",
    }


async def run_loop(*, rounds: int = 5, interval: int = 10, capture: bool = False,
                   demo: bool = False, camera=None, mode: str = "auto",
                   vision_dir: str = "", body_dir: str = "",
                   verbose: bool = True, round_callback=None) -> dict:
    """连续闭环主循环。返回汇总（rounds_run / actions_executed / rounds）。"""
    rounds = _clamp_int(rounds, 1, 10000, 5)
    interval = _clamp_int(interval, 1, 3600, 10)
    # 目录默认值遵循 env（与 collab_mcp 模块一致），便于测试隔离与共享部署
    vision_dir = vision_dir or os.environ.get("COLLAB_VISION_DIR") or str(ROOT / "vision")
    body_dir = body_dir or os.environ.get("COLLAB_BODY_DIR") or str(ROOT / "body")
    results: list[dict] = []
    executed = 0
    analysis_ok = 0
    capture_failures = 0
    errors: list[str] = []
    actions_by_name: dict[str, int] = {}
    latest = Path(vision_dir) / "latest.json"
    observation_ready = latest.exists()
    if not capture and not observation_ready:
        print(
            f"[robot_loop] 预检：{latest} 不存在且未启用 --capture，"
            "大脑将没有观察可分析；建议加 --capture/--demo 或先跑 vision_capture.py。",
            file=sys.stderr,
        )
    t_total = time.perf_counter()
    try:
        for i in range(1, rounds + 1):
            r = await _run_round(i, capture=capture, demo=demo, camera=camera,
                                 mode=mode, vision_dir=vision_dir, body_dir=body_dir)
            results.append(r)
            if r.get("action_ok"):
                executed += 1
            if r.get("analysis_ok"):
                analysis_ok += 1
            else:
                err = str(r.get("error", ""))[:120]
                if err and err not in errors:
                    errors.append(err)
            if r.get("capture_ok") is False:
                capture_failures += 1
            act = r.get("suggestion")
            if act:
                actions_by_name[act] = actions_by_name.get(act, 0) + 1
            if round_callback is not None:
                round_callback(r, i)
            if verbose:
                print(f"[round {i}] obs={r.get('observation_id')} "
                      f"conf={r.get('confidence')} action={r.get('suggestion')!r} "
                      f"ok={r.get('action_ok')} {r.get('action_summary', '')[:60]}")
            if i < rounds:
                await _sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[robot_loop] 已停止（Ctrl+C），已跑 {len(results)} 轮", file=sys.stderr)
    total_ms = int((time.perf_counter() - t_total) * 1000)
    stats = {
        "total_ms": total_ms,
        "rounds_run": len(results),
        "analysis_ok": analysis_ok,
        "analysis_ok_rate": round(analysis_ok / len(results), 3) if results else 0.0,
        "actions_executed": executed,
        "action_ok_rate": round(executed / len(results), 3) if results else 0.0,
        "actions_by_name": actions_by_name,
        "capture_failures": capture_failures,
        "errors": errors[:5],
    }
    return {
        "rounds_run": len(results),
        "actions_executed": executed,
        "rounds": results,
        "observation_ready_before_start": observation_ready,
        "stats": stats,
    }


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="眼睛→大脑→身体 连续闭环 demo（robot_loop v3.14）")
    ap.add_argument("--rounds", type=int, default=5, help="循环轮数（默认 5）")
    ap.add_argument("--interval", type=int, default=10, help="轮间隔秒（钳制 1..3600，默认 10）")
    ap.add_argument("--capture", action="store_true", help="每轮用 vision_capture.py 刷新观察")
    ap.add_argument("--demo", action="store_true", help="采集用 demo 模式（合成观察，离线可用）")
    ap.add_argument("--camera", type=int, default=None, help="采集用摄像头索引")
    ap.add_argument("--mode", choices=("auto", "heuristic", "llm"), default="auto",
                    help="大脑分析模式（默认 auto）")
    ap.add_argument("--vision-dir", type=str, default="", help="观察目录覆盖（默认 collab/vision）")
    ap.add_argument("--body-dir", type=str, default="", help="身体状态目录覆盖（默认 collab/body）")
    ap.add_argument("--json", action="store_true", help="stdout 输出最终汇总 JSON")
    ap.add_argument("--stream-json", action="store_true",
                    help="stdout 每轮一行 JSON + 末尾一行汇总 JSON（自动关 verbose）")
    args = ap.parse_args(argv)

    def _stream(round_result: dict, _round_no: int) -> None:
        if args.stream_json:
            print(json.dumps(round_result, ensure_ascii=False), flush=True)

    summary = asyncio.run(run_loop(
        rounds=args.rounds, interval=args.interval, capture=args.capture,
        demo=args.demo, camera=args.camera, mode=args.mode,
        vision_dir=args.vision_dir, body_dir=args.body_dir,
        verbose=not (args.json or args.stream_json),
        round_callback=_stream,
    ))
    if args.stream_json:
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    elif args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"[done] rounds_run={summary['rounds_run']} "
              f"actions_executed={summary['actions_executed']} "
              f"analysis_ok={summary['stats']['analysis_ok']} "
              f"capture_failures={summary['stats']['capture_failures']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
